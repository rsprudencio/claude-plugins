"""LLM-driven memory consolidation with ANN-based clustering.

Provides memory garbage collection through:
1. ANN-based candidate selection (O(n*K), not O(n^2) pairwise)
2. LLM summarization with provenance tracking
3. Confidence gating (graduated rollout: manual → shadow → auto)
4. Transactional supersession (INSERT consolidated + UPDATE originals, atomic)
5. Reversible operations via consolidation_run_id

All consolidation operations require explicit opt-in (disabled by default).
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger("jarvis-core")

# Module-level imports for test patchability (deferred imports cause patch failures)
from .schema import _get_pool, execute_query, jsonb_to_metadata, metadata_to_jsonb
from .embedding import get_embedding_service


# ── Data structures ──────────────────────────────────────────────────


@dataclass
class MemoryCluster:
    """A group of similar memories that could be consolidated."""
    memory_ids: list[str]
    avg_similarity: float
    total_importance: float
    contents: list[dict] = field(default_factory=list)

    @property
    def size(self) -> int:
        return len(self.memory_ids)

    @property
    def idempotency_key(self) -> str:
        """Hash of sorted source IDs — same cluster always gets same key."""
        sorted_ids = sorted(self.memory_ids)
        return hashlib.sha256("|".join(sorted_ids).encode()).hexdigest()[:16]


@dataclass
class ConsolidationResult:
    """Result of consolidating a single cluster."""
    content: str
    importance: float
    supersedes: list[str]
    contradictions: list[dict] = field(default_factory=list)
    confidence: float = 0.0
    cluster: Optional[MemoryCluster] = None


# ── ANN-based candidate selection ────────────────────────────────────


def find_consolidation_candidates(
    *,
    min_cluster_size: int = 3,
    similarity_threshold: float = 0.85,
    max_clusters: int = 20,
    budget_seconds: int = 60,
    min_importance: float = 0.0,
) -> list[MemoryCluster]:
    """Find groups of similar active memories for consolidation.

    Algorithm (ANN-based, NOT O(n^2)):
    1. Fetch all active core memories with embeddings
    2. For each memory, query pgvector for top-K nearest neighbors
    3. Filter neighbors by similarity >= threshold
    4. Build adjacency graph from neighbor relationships
    5. Connected components with >= min_cluster_size = candidate clusters
    6. Rank clusters by total importance (highest = consolidate first)
    7. Stop if time budget exceeded (backpressure)

    Returns:
        List of MemoryCluster objects, sorted by total_importance descending.
    """
    start = time.time()
    pool = _get_pool()

    # Step 1: Get all active memory IDs + importance
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, importance_score, embedding
                   FROM core.memories
                   WHERE status = 'active'
                     AND importance_score >= %s
                   ORDER BY importance_score DESC""",
                (min_importance,),
            )
            columns = [d.name for d in cur.description]
            memories = [dict(zip(columns, row)) for row in cur.fetchall()]

    if len(memories) < min_cluster_size:
        return []

    # Step 2-3: For each memory, find ANN neighbors above threshold
    # Use pgvector cosine distance: distance <=> embedding
    # similarity = 1 - (distance / 2)
    K = 10  # neighbors per memory
    adjacency: dict[str, set[str]] = {m["id"]: set() for m in memories}
    importance_map = {m["id"]: float(m["importance_score"]) for m in memories}

    with pool.connection() as conn:
        with conn.cursor() as cur:
            for mem in memories:
                if time.time() - start > budget_seconds:
                    logger.info("Consolidation candidate search hit time budget (%ds)", budget_seconds)
                    break

                cur.execute(
                    """SELECT id, embedding <=> %s::halfvec AS distance
                       FROM core.memories
                       WHERE status = 'active' AND id != %s
                       ORDER BY embedding <=> %s::halfvec ASC
                       LIMIT %s""",
                    (mem["embedding"], mem["id"], mem["embedding"], K),
                )
                for row in cur.fetchall():
                    neighbor_id = row[0]
                    distance = float(row[1])
                    similarity = 1.0 - (distance / 2.0)
                    if similarity >= similarity_threshold:
                        adjacency[mem["id"]].add(neighbor_id)
                        if neighbor_id in adjacency:
                            adjacency[neighbor_id].add(mem["id"])

    # Step 4: Connected components via BFS
    visited = set()
    clusters = []

    for mem_id in adjacency:
        if mem_id in visited:
            continue
        if not adjacency[mem_id]:
            visited.add(mem_id)
            continue

        # BFS from this node
        component = set()
        queue = [mem_id]
        while queue:
            node = queue.pop(0)
            if node in visited:
                continue
            visited.add(node)
            component.add(node)
            for neighbor in adjacency.get(node, set()):
                if neighbor not in visited:
                    queue.append(neighbor)

        if len(component) >= min_cluster_size:
            ids = list(component)
            total_imp = sum(importance_map.get(i, 0.5) for i in ids)

            # Compute average pairwise similarity within cluster
            pair_count = 0
            sim_sum = 0.0
            for i, id_a in enumerate(ids):
                for id_b in ids[i + 1:]:
                    if id_b in adjacency.get(id_a, set()):
                        sim_sum += similarity_threshold  # lower bound
                        pair_count += 1

            avg_sim = sim_sum / pair_count if pair_count > 0 else similarity_threshold

            clusters.append(MemoryCluster(
                memory_ids=ids,
                avg_similarity=avg_sim,
                total_importance=total_imp,
            ))

    # Step 5: Sort by total importance descending, cap at max_clusters
    clusters.sort(key=lambda c: c.total_importance, reverse=True)
    return clusters[:max_clusters]


def _load_cluster_contents(cluster: MemoryCluster) -> MemoryCluster:
    """Load full document content for a cluster's memories.

    Populates cluster.contents with dicts containing id, document,
    importance_score, category, and metadata.
    """
    pool = _get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, document, importance_score, category, metadata
                   FROM core.memories
                   WHERE id = ANY(%s) AND status = 'active'""",
                (cluster.memory_ids,),
            )
            columns = [d.name for d in cur.description]
            cluster.contents = [dict(zip(columns, row)) for row in cur.fetchall()]

    return cluster


# ── LLM summarization ────────────────────────────────────────────────


CONSOLIDATION_PROMPT = """You are consolidating {count} related memories into a single, authoritative summary.

Memories (with IDs for provenance):
{numbered_memories}

Rules:
1. Preserve all unique information — do not drop facts
2. For contradictions: DO NOT auto-resolve. Flag them explicitly:
   "Note: Memory #{a_idx} says X but Memory #{b_idx} says Y — unresolved."
3. Merge overlapping observations into crisp statements
4. Maintain the original importance level (highest in cluster)
5. Include source memory IDs for claim-level provenance

Return valid JSON only:
{{"content": "...", "importance": 0.8, "supersedes": ["id1", "id2", ...], "contradictions": [{{"claim": "...", "sources": ["id1", "id2"]}}]}}"""


def build_consolidation_prompt(cluster: MemoryCluster) -> str:
    """Build the LLM prompt for a loaded cluster."""
    numbered = []
    for i, mem in enumerate(cluster.contents, 1):
        numbered.append(f"#{i} [ID: {mem['id']}] (importance: {mem['importance_score']}):\n{mem['document']}")

    return CONSOLIDATION_PROMPT.format(
        count=len(cluster.contents),
        numbered_memories="\n\n".join(numbered),
        a_idx=1,
        b_idx=2,
    )


def parse_consolidation_response(text: str) -> dict:
    """Parse LLM JSON response, extracting from markdown code blocks if needed."""
    # Strip markdown code fences if present
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # Remove first line (```json or ```) and last line (```)
        lines = [l for l in lines if not l.strip().startswith("```")]
        cleaned = "\n".join(lines).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        return {"error": f"Failed to parse LLM response: {text[:200]}"}


# ── Confidence assessment ────────────────────────────────────────────


def assess_confidence(cluster: MemoryCluster, contradictions: list[dict]) -> float:
    """Compute confidence score for a consolidation candidate.

    Metrics:
    - cluster_cohesion: avg pairwise similarity within cluster
    - contradiction_penalty: 0.1 per flagged contradiction

    Returns:
        Confidence score (0.0 - 1.0).
    """
    cohesion = cluster.avg_similarity
    penalty = 0.1 * len(contradictions)
    return max(0.0, cohesion - penalty)


# ── Transactional apply/undo ─────────────────────────────────────────


def apply_consolidation(
    result: ConsolidationResult,
    *,
    run_id: Optional[str] = None,
) -> dict:
    """Apply a consolidation result transactionally.

    In a single PG transaction:
    1. INSERT consolidated memory
    2. UPDATE originals: status='superseded', superseded_by=<new_id>
    3. Set consolidation_run_id on all affected rows

    Args:
        result: ConsolidationResult from LLM summarization.
        run_id: Consolidation run identifier. Auto-generated if None.

    Returns:
        Dict with new_id, run_id, superseded_count.
    """
    if run_id is None:
        run_id = f"run-{uuid.uuid4().hex[:12]}"

    cluster = result.cluster
    if cluster is None:
        return {"error": "No cluster attached to result"}

    # Generate embedding for consolidated content
    service = get_embedding_service()
    embedding = service.encode(result.content)

    # Idempotency key from sorted source IDs
    idem_key = cluster.idempotency_key
    new_id = f"consolidated::{idem_key}"

    pool = _get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Check idempotency: skip if already consolidated
            cur.execute(
                "SELECT id FROM core.memories WHERE id = %s",
                (new_id,),
            )
            if cur.fetchone():
                conn.rollback()
                return {
                    "skipped": True,
                    "reason": "Already consolidated (idempotent)",
                    "existing_id": new_id,
                }

            # 1. INSERT consolidated memory
            meta = {
                "consolidation_run_id": run_id,
                "source_ids": result.supersedes,
                "contradictions": result.contradictions,
            }
            cur.execute(
                """INSERT INTO core.memories
                   (id, document, embedding, category, scope, importance_score,
                    source, status, consolidation_run_id, metadata)
                   VALUES (%s, %s, %s::halfvec, 'summary', 'global', %s,
                           'consolidation', 'active', %s, %s::jsonb)""",
                (new_id, result.content, embedding, result.importance,
                 run_id, metadata_to_jsonb(meta)),
            )

            # 2. UPDATE originals: supersede them
            cur.execute(
                """UPDATE core.memories
                   SET status = 'superseded',
                       superseded_by = %s,
                       consolidation_run_id = %s,
                       updated_at = now()
                   WHERE id = ANY(%s) AND status = 'active'""",
                (new_id, run_id, result.supersedes),
            )
            superseded_count = cur.rowcount

            conn.commit()

    logger.info(
        "Consolidation applied: %s superseded %d memories (run=%s)",
        new_id, superseded_count, run_id,
    )

    return {
        "new_id": new_id,
        "run_id": run_id,
        "superseded_count": superseded_count,
        "contradictions": len(result.contradictions),
    }


def undo_consolidation(run_id: str) -> dict:
    """Undo all consolidations from a specific run.

    1. Clear superseded_by on all originals in the run
    2. Soft-delete the consolidated memories
    3. Originals return to active_memories view automatically

    Args:
        run_id: The consolidation_run_id to undo.

    Returns:
        Dict with restored_count and deleted_count.
    """
    pool = _get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # 1. Restore originals: clear supersession
            cur.execute(
                """UPDATE core.memories
                   SET status = 'active',
                       superseded_by = NULL,
                       consolidation_run_id = NULL,
                       updated_at = now()
                   WHERE consolidation_run_id = %s
                     AND status = 'superseded'""",
                (run_id,),
            )
            restored = cur.rowcount

            # 2. Soft-delete consolidated summaries
            cur.execute(
                """UPDATE core.memories
                   SET status = 'deleted',
                       deleted_at = now(),
                       updated_at = now()
                   WHERE consolidation_run_id = %s
                     AND source = 'consolidation'
                     AND status = 'active'""",
                (run_id,),
            )
            deleted = cur.rowcount

            conn.commit()

    logger.info(
        "Consolidation undone: run=%s restored=%d deleted=%d",
        run_id, restored, deleted,
    )

    return {
        "run_id": run_id,
        "restored_count": restored,
        "deleted_count": deleted,
    }


# ── Dry-run support ──────────────────────────────────────────────────


def dry_run_consolidation(
    *,
    min_cluster_size: int = 3,
    similarity_threshold: float = 0.85,
    max_clusters: int = 20,
) -> list[dict]:
    """Show what would be consolidated without doing it.

    Returns list of cluster summaries with memory IDs, sizes, and importance.
    """
    clusters = find_consolidation_candidates(
        min_cluster_size=min_cluster_size,
        similarity_threshold=similarity_threshold,
        max_clusters=max_clusters,
    )

    results = []
    for i, cluster in enumerate(clusters, 1):
        _load_cluster_contents(cluster)
        previews = []
        for mem in cluster.contents[:3]:  # Preview first 3
            doc = mem.get("document", "")
            previews.append(doc[:100] + "..." if len(doc) > 100 else doc)

        results.append({
            "cluster": i,
            "size": cluster.size,
            "memory_ids": cluster.memory_ids,
            "avg_similarity": round(cluster.avg_similarity, 3),
            "total_importance": round(cluster.total_importance, 2),
            "idempotency_key": cluster.idempotency_key,
            "previews": previews,
        })

    return results
