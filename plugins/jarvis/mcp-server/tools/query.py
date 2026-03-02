"""Vault memory querying for semantic search.

Provides query, read, and stats operations against the PostgreSQL jarvis table
with pgvector embeddings. Uses explicit embedding via EmbeddingService.

All document IDs use namespaced format (vault:: prefix) for type-safe identification.
"""

import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

from .schema import execute_query, jsonb_to_metadata, metadata_to_jsonb
from .paths import get_path, SENSITIVE_PATHS
from .namespaces import parse_id, ALL_TYPES, get_tier, TIER_FILE, TIER_CHROMADB
from .expansion import expand_query as _expand_query
from .config import get_expansion_config, get_per_prompt_config, get_reranking_config, get_staleness_config
from .format_support import detect_format
from .staleness import check_staleness, deserialize_mtimes


def _annotate_staleness(raw_entries: list, staleness_config: dict) -> None:
    """In-place annotate raw query entries with staleness information.

    Only processes entries in the obs:: namespace (auto-extracted observations).
    Reads file_mtimes from already-fetched metadata and compares against current
    filesystem state. No additional database operations.

    Args:
        raw_entries: List of entry dicts with 'doc_id', 'metadata', 'relevance' keys.
        staleness_config: Config dict with 'penalty' (float) key.
    """
    penalty = staleness_config.get("penalty", 0.15)

    for entry in raw_entries:
        doc_id = entry.get("doc_id", "")
        if not doc_id.startswith("obs::"):
            continue

        meta = entry.get("metadata", {})
        mtimes_raw = meta.get("file_mtimes")
        if not mtimes_raw:
            continue

        recorded = deserialize_mtimes(mtimes_raw)
        if not recorded:
            continue

        result = check_staleness(recorded)
        if result["is_stale"]:
            entry["is_stale"] = True
            entry["staleness_info"] = result
            entry["relevance"] = max(0.0, entry["relevance"] - penalty)


def _detect_format_from_entry(entry: dict) -> str:
    """Detect format from a query result entry's parent_file path."""
    parent_file = entry.get("parent_file", "")
    return detect_format(parent_file) if parent_file else "markdown"


def _compute_relevance(
    distance: float,
    importance: str = "medium",
    updated_at: Optional[str] = None,
    importance_score: Optional[float] = None,
) -> float:
    """Convert cosine distance to relevance score with boosts.

    pgvector cosine distance (<=> operator) ranges from 0 (identical)
    to 2 (opposite). We convert to a 0-1 relevance score and apply
    importance + recency adjustments.

    When importance_score (float 0-1 from scoring module) is available, it
    provides a more nuanced boost than the string importance field.
    """
    base = 1.0 - (distance / 2.0)

    # Use numeric importance_score when available, fall back to string importance
    if importance_score is not None:
        # Map 0.0-1.0 score to -0.12..+0.12 boost (centered at 0.5)
        boost = (importance_score - 0.5) * 0.24
    else:
        boost = {"high": 0.10, "critical": 0.12, "medium": 0.0, "low": -0.05}.get(
            importance, 0.0
        )

    # Recency boost: recent updates get a small relevance bump
    recency_boost = 0.0
    if updated_at:
        try:
            updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days_ago = (now - updated).total_seconds() / 86400
            if days_ago <= 1:
                recency_boost = 0.08
            elif days_ago <= 7:
                recency_boost = 0.05
        except (ValueError, TypeError):
            pass

    return max(0.0, min(1.0, base + boost + recency_boost))


def _extract_preview(content: str, max_len: int = 150, fmt: str = "markdown") -> str:
    """Extract a clean preview from document content.

    Strips frontmatter/properties and leading headings, format-aware.
    """
    from .format_support import strip_frontmatter

    stripped = strip_frontmatter(content, fmt)
    # Strip leading headings (both # and * styles)
    if fmt == "org":
        stripped = re.sub(
            r"^\*+\s+.*$", "", stripped, count=1, flags=re.MULTILINE
        ).strip()
        # Strip #+TITLE lines
        stripped = re.sub(
            r"^#\+TITLE:.*$", "", stripped, count=1, flags=re.MULTILINE
        ).strip()
    else:
        stripped = re.sub(
            r"^#+\s+.*$", "", stripped, count=1, flags=re.MULTILINE
        ).strip()
    # Collapse whitespace
    stripped = re.sub(r"\s+", " ", stripped).strip()

    if len(stripped) <= max_len:
        return stripped

    # Truncate at word boundary
    truncated = stripped[:max_len]
    last_space = truncated.rfind(" ")
    if last_space > max_len * 0.5:
        truncated = truncated[:last_space]
    return truncated + "..."


def _build_filter_sql(
    filter_dict: Optional[dict] = None, user: Optional[str] = None
) -> tuple:
    """Build SQL WHERE conditions from a filter dict.

    Translates the metadata schema where:
    - 'type' is the universal content type (vault, memory, observation, etc.)
    - 'vault_type' is the vault-entry type (note, journal, work, etc.)

    When users filter by type with a vault-entry value (note, journal, etc.),
    we transparently map to vault_type. When they use a content type value
    (vault, memory, etc.), we use the universal type field.

    The optional ``user`` parameter adds a metadata filter for multi-user
    isolation — only documents attributed to that user are returned.

    Returns:
        Tuple of (conditions_list, params_list) for SQL WHERE clause.
    """
    if not filter_dict:
        filter_dict = {}

    conditions = []
    params = []

    # Multi-user filter (opt-in)
    if user and user != "anonymous":
        conditions.append("metadata->>'user' = %s")
        params.append(user)

    if "directory" in filter_dict and filter_dict["directory"]:
        conditions.append("metadata->>'directory' = %s")
        params.append(filter_dict["directory"])

    if "type" in filter_dict and filter_dict["type"]:
        type_val = filter_dict["type"]
        if type_val in ALL_TYPES:
            # Universal content type (vault, memory, observation, etc.)
            conditions.append("metadata->>'type' = %s")
            params.append(type_val)
        else:
            # Vault-entry type (note, journal, work, etc.)
            conditions.append("metadata->>'vault_type' = %s")
            params.append(type_val)

    if "importance" in filter_dict and filter_dict["importance"]:
        conditions.append("metadata->>'importance' = %s")
        params.append(filter_dict["importance"])

    if "tags" in filter_dict and filter_dict["tags"]:
        # Tags stored as comma-separated string; use LIKE for substring match
        tag = filter_dict["tags"].split(",")[0].strip()
        conditions.append("metadata->>'tags' LIKE %s")
        params.append(f"%{tag}%")

    return conditions, params


def _display_path(doc_id: str) -> str:
    """Strip namespace prefix from ID for display purposes."""
    parsed = parse_id(doc_id)
    return parsed.content_id


def _increment_retrieval_counts(doc_ids: list, increment: float = 1.0) -> None:
    """Batch increment retrieval counts for Tier 2 documents.

    Best-effort operation: errors are logged but don't block query response.
    Only updates Tier 2 documents (Tier 1 doesn't track retrieval counts).

    Uses a single SQL UPDATE for all IDs (much more efficient than ChromaDB's
    individual get+upsert pattern).

    Args:
        doc_ids: List of document IDs to increment
        increment: Amount to add to retrieval_count (default 1.0).
                   Use fractional values (e.g. 0.01) for passive surfacing.
    """
    if not doc_ids:
        return

    try:
        from .schema import _get_pool

        # Filter to only Tier 2 IDs
        tier2_ids = [doc_id for doc_id in doc_ids if get_tier(doc_id) == TIER_CHROMADB]
        if not tier2_ids:
            return

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # Batch update retrieval counts using ANY() for efficiency
                cur.execute(
                    """UPDATE jarvis
                       SET metadata = jsonb_set(
                           jsonb_set(metadata, '{retrieval_count}',
                               to_jsonb((COALESCE((metadata->>'retrieval_count')::float, 0) + %s)::text)),
                           '{updated_at}', to_jsonb(%s::text)),
                           updated_at = now()
                       WHERE id = ANY(%s)""",
                    (increment, now_iso, tier2_ids),
                )
                conn.commit()

    except Exception as e:
        import logging

        logger = logging.getLogger("jarvis-core")
        logger.warning(f"Failed to increment retrieval counts: {e}")


def query_vault(
    query: str,
    n_results: int = 5,
    filter: Optional[dict] = None,
    user: Optional[str] = None,
) -> dict:
    """Semantic search across vault memory.

    Args:
        query: Natural language search query
        n_results: Max results (capped at 20)
        filter: Optional metadata filters (directory, type, importance, tags)
        user: Optional user filter for multi-user isolation

    Returns:
        Formatted results dict with titles, paths, excerpts, relevance scores
    """
    from .embedding import get_embedding_service
    from .schema import _get_pool

    try:
        count_result = execute_query(
            "SELECT count(*) AS cnt FROM jarvis", fetch="one"
        )
        total = count_result["cnt"] if count_result else 0
    except Exception as e:
        return {"success": False, "error": f"Database unavailable: {e}"}

    if total == 0:
        return {
            "success": True,
            "query": query,
            "results": [],
            "total_in_collection": 0,
            "message": "No documents indexed. Ask Jarvis to 'index my vault' or use jarvis_index_vault tool.",
        }

    n_results = min(max(1, n_results), 20)

    # Build filter conditions
    filter_conditions, filter_params = _build_filter_sql(filter, user=user)

    # Query expansion
    expansion_config = get_expansion_config()
    expansion = _expand_query(query, expansion_config)
    search_text = expansion["expanded"]

    # Embed the query
    try:
        service = get_embedding_service()
        query_embedding = service.encode(search_text)
    except Exception as e:
        return {"success": False, "error": f"Embedding failed: {e}"}

    # Over-fetch to account for chunk deduplication (and reranking if enabled)
    reranking_config = get_reranking_config()
    if reranking_config.get("enabled", True):
        fetch_count = min(reranking_config.get("candidate_count", 100), total)
    else:
        fetch_count = min(n_results * 3, 60, total)

    try:
        # Build the similarity search query
        where_clause = ""
        params = [query_embedding]
        if filter_conditions:
            where_clause = "WHERE " + " AND ".join(filter_conditions)
            params.extend(filter_params)
        params.append(fetch_count)

        sql = f"""SELECT id, document, metadata,
                         embedding <=> %s::halfvec AS distance
                  FROM jarvis
                  {where_clause}
                  ORDER BY distance ASC
                  LIMIT %s"""

        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, tuple(params))
                columns = [desc.name for desc in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    except Exception as e:
        return {"success": False, "error": f"Query failed: {e}"}

    # Build raw result entries with relevance scores
    raw_entries = []
    for row in rows:
        meta = jsonb_to_metadata(row["metadata"])
        distance = float(row["distance"])
        importance = meta.get("importance", "medium")
        updated_at = meta.get("updated_at")

        # Use numeric importance_score if available
        imp_score = None
        if "importance_score" in meta:
            try:
                imp_score = float(meta["importance_score"])
            except (ValueError, TypeError):
                pass

        relevance = _compute_relevance(distance, importance, updated_at, imp_score)

        # Determine parent file for chunk dedup
        parent_file = meta.get("parent_file")
        if not parent_file:
            # Legacy: strip #chunk-N from ID
            parsed = parse_id(row["id"])
            parent_file = parsed.content_id

        raw_entries.append(
            {
                "doc_id": row["id"],
                "parent_file": parent_file,
                "relevance": relevance,
                "document": row["document"],
                "metadata": meta,
            }
        )

    # Staleness annotation: penalize observations whose tracked files changed
    staleness_config = get_staleness_config()
    if staleness_config.get("enabled", True):
        _annotate_staleness(raw_entries, staleness_config)

    # Chunk deduplication: keep best-relevance chunk per parent_file
    best_per_file = {}
    for entry in raw_entries:
        pf = entry["parent_file"]
        if (
            pf not in best_per_file
            or entry["relevance"] > best_per_file[pf]["relevance"]
        ):
            best_per_file[pf] = entry

    # Cross-encoder reranking (applied only when enabled and >1 candidate)
    reranking_applied = False
    if reranking_config.get("enabled") and len(best_per_file) > 1:
        from .reranking import rerank

        deduped_list = sorted(
            best_per_file.values(), key=lambda e: e["relevance"], reverse=True
        )
        docs = [e["document"] or "" for e in deduped_list]
        vscores = [e["relevance"] for e in deduped_list]
        blended = rerank(query, docs, vscores, reranking_config)
        if blended is not vscores:
            for entry, score in zip(deduped_list, blended):
                entry["relevance"] = score
            reranking_applied = True

    # Sort by relevance descending and trim to final count
    final_count = reranking_config.get("top_k", 10) if reranking_applied else n_results
    deduped = sorted(
        best_per_file.values(), key=lambda e: e["relevance"], reverse=True
    )[:final_count]

    results = []
    all_ids = []
    for rank, entry in enumerate(deduped, start=1):
        meta = entry["metadata"]
        doc_id = entry["doc_id"]
        entry_fmt = _detect_format_from_entry(entry)
        preview = (
            _extract_preview(entry["document"], fmt=entry_fmt)
            if entry["document"]
            else ""
        )
        title = meta.get("title", doc_id)
        doc_type = meta.get("vault_type") or meta.get("type", "unknown")
        doc_importance = meta.get("importance", "medium")
        tags = meta.get("tags", "")
        chunk_heading = meta.get("chunk_heading", "")

        tier = get_tier(doc_id)
        if tier == TIER_FILE:
            source = "file"
        else:
            source = meta.get("type", "chromadb")

        result_entry = {
            "rank": rank,
            "id": doc_id,
            "path": entry["parent_file"],
            "title": title,
            "type": doc_type,
            "importance": doc_importance,
            "relevance": round(entry["relevance"], 3),
            "preview": preview,
            "tags": tags,
            "tier": tier,
            "source": source,
        }
        if chunk_heading:
            result_entry["chunk_heading"] = chunk_heading
        if entry.get("is_stale"):
            result_entry["stale"] = True
            stale_info = entry.get("staleness_info", {})
            if stale_info.get("stale_files"):
                result_entry["stale_files"] = stale_info["stale_files"]
        results.append(result_entry)
        all_ids.append(doc_id)

    # Increment retrieval counts for Tier 2 results (best-effort, non-blocking)
    _increment_retrieval_counts(all_ids)

    response = {
        "success": True,
        "query": query,
        "results": results,
        "total_in_collection": total,
    }

    # Include expansion metadata when terms were added
    if expansion["terms_added"]:
        response["expansion"] = {
            "terms_added": expansion["terms_added"],
            "intent": expansion["intent"],
        }

    # Include reranking metadata when applied
    if reranking_applied:
        response["reranking"] = {
            "applied": True,
            "alpha": reranking_config.get("alpha", 0.7),
            "candidates": len(best_per_file),
            "top_k": final_count,
        }

    return response


def semantic_context(
    query: str,
    threshold: float = 0.5,
    budget: int = 8000,
    skip_retrieval_increment: bool = False,
) -> dict:
    """Search vault memories for per-prompt context injection.

    Optimized for automatic, per-message use. Differs from query_vault():
    - Applies minimum relevance threshold (filters low-scoring results)
    - Budget-based allocation: tier2 content shown in full, vault as references
    - Fractionally increments retrieval counts (configurable, default 0.01)
    - Filters out sensitive directories (documents/, people/)
    - Returns query_ms timing for performance monitoring
    - Silently returns empty on any error (graceful degradation)

    Budget strategy:
    - Total budget is split 50/50 between tier2 and vault
    - Tier 2 items (observations, learnings, etc.) are shown with full content
    - Vault items are shown as references (file path + heading, ~120 chars)
    - Each half gets a guaranteed budget; unused budget overflows to the other
    - Results are processed in relevance order, highest first

    Args:
        query: User's raw prompt text
        threshold: Minimum relevance score 0.0-1.0 (default 0.5)
        budget: Total character budget for injection (default 8000, split 50/50)
        skip_retrieval_increment: If True, skip the retrieval count write-back.
            Surfaced IDs are returned in the result dict so the caller can
            bump them externally (e.g., via HTTP POST to the MCP server).

    Returns:
        Dict with matches list and metadata. When skip_retrieval_increment=True,
        includes 'surfaced_ids' list for deferred bumping.
    """
    start = time.time()

    try:
        from .embedding import get_embedding_service
        from .schema import _get_pool

        count_result = execute_query(
            "SELECT count(*) AS cnt FROM jarvis", fetch="one"
        )
        total = count_result["cnt"] if count_result else 0
    except Exception:
        return {"matches": [], "query_ms": 0, "total_searched": 0}

    if total == 0:
        return {"matches": [], "query_ms": 0, "total_searched": 0}

    # Query expansion (reuse existing infrastructure)
    expansion_config = get_expansion_config()
    expansion = _expand_query(query, expansion_config)
    search_text = expansion["expanded"]

    # Embed the query
    try:
        service = get_embedding_service()
        query_embedding = service.encode(search_text)
    except Exception:
        return {"matches": [], "query_ms": 0, "total_searched": total}

    # Over-fetch to account for chunk dedup + threshold filtering
    fetch_count = min(100, total)

    try:
        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """SELECT id, document, metadata,
                              embedding <=> %s::halfvec AS distance
                       FROM jarvis
                       ORDER BY distance ASC
                       LIMIT %s""",
                    (query_embedding, fetch_count),
                )
                columns = [desc.name for desc in cur.description]
                rows = [dict(zip(columns, row)) for row in cur.fetchall()]
    except Exception:
        return {"matches": [], "query_ms": 0, "total_searched": total}

    # Build raw entries with relevance scores
    raw_entries = []
    skipped_sensitive = 0

    for row in rows:
        meta = jsonb_to_metadata(row["metadata"])
        distance = float(row["distance"])

        # Filter sensitive directories
        directory = meta.get("directory", "")
        if directory in SENSITIVE_PATHS:
            skipped_sensitive += 1
            continue

        # Filter superseded Tier 2 entries
        if meta.get("status") == "superseded":
            continue

        importance = meta.get("importance", "medium")
        updated_at = meta.get("updated_at")

        imp_score = None
        if "importance_score" in meta:
            try:
                imp_score = float(meta["importance_score"])
            except (ValueError, TypeError):
                pass

        relevance = _compute_relevance(distance, importance, updated_at, imp_score)

        # Apply threshold
        if relevance < threshold:
            continue

        # Determine parent file for chunk dedup
        parent_file = meta.get("parent_file")
        if not parent_file:
            parsed = parse_id(row["id"])
            parent_file = parsed.content_id

        raw_entries.append(
            {
                "doc_id": row["id"],
                "parent_file": parent_file,
                "relevance": relevance,
                "document": row["document"],
                "metadata": meta,
            }
        )

    # Staleness annotation: penalize observations whose tracked files changed
    staleness_config = get_staleness_config()
    if staleness_config.get("enabled", True):
        _annotate_staleness(raw_entries, staleness_config)

    # Chunk deduplication: keep best-relevance chunk per parent_file
    best_per_file = {}
    for entry in raw_entries:
        pf = entry["parent_file"]
        if (
            pf not in best_per_file
            or entry["relevance"] > best_per_file[pf]["relevance"]
        ):
            best_per_file[pf] = entry

    # Sort by relevance descending
    deduped = sorted(best_per_file.values(), key=lambda e: e["relevance"], reverse=True)

    # Budget-based selection: process in relevance order
    # Each item tries its own half first, overflows to the other
    VAULT_REF_COST = 120  # estimated chars for "See: path + heading"
    half = budget // 2
    tier2_remaining = half
    vault_remaining = half
    selected = []

    for entry in deduped:
        ns = entry["metadata"].get("namespace", "")
        is_vault = ns == "vault::"

        if is_vault:
            cost = VAULT_REF_COST
            if vault_remaining >= cost:
                vault_remaining -= cost
                entry["display_mode"] = "reference"
                selected.append(entry)
            elif tier2_remaining >= cost:
                tier2_remaining -= cost
                entry["display_mode"] = "reference"
                selected.append(entry)
        else:
            content_len = len(entry["document"] or "")
            cost = max(content_len, 50)  # minimum 50 chars cost
            if tier2_remaining >= cost:
                tier2_remaining -= cost
                entry["display_mode"] = "full"
                selected.append(entry)
            elif vault_remaining >= cost:
                vault_remaining -= cost
                entry["display_mode"] = "full"
                selected.append(entry)

    # Fractional retrieval bump for passively surfaced results
    surfaced_ids = [entry["doc_id"] for entry in selected] if selected else []
    if selected and not skip_retrieval_increment:
        per_prompt_config = get_per_prompt_config()
        passive_increment = per_prompt_config.get("passive_retrieval_increment", 0.01)
        if passive_increment > 0:
            _increment_retrieval_counts(surfaced_ids, increment=passive_increment)

    matches = []
    for entry in selected:
        meta = entry["metadata"]
        doc_type = meta.get("vault_type") or meta.get("type", "unknown")
        chunk_heading = meta.get("chunk_heading", "")

        if entry["display_mode"] == "reference":
            # Vault reference: path + heading only, no content
            ref_text = entry["parent_file"]
            if chunk_heading:
                ref_text += f" (section: {chunk_heading})"
            content = ref_text
        else:
            # Tier 2: full content, no truncation
            entry_fmt = _detect_format_from_entry(entry)
            content = (
                _extract_preview(entry["document"], max_len=10000, fmt=entry_fmt)
                if entry["document"]
                else ""
            )

        match = {
            "source": entry["parent_file"],
            "title": meta.get("title", entry["parent_file"]),
            "relevance": round(entry["relevance"], 3),
            "type": doc_type,
            "content": content,
            "display_mode": entry["display_mode"],
        }
        if chunk_heading:
            match["heading"] = chunk_heading
        if entry.get("is_stale"):
            match["stale"] = True
        matches.append(match)

    query_ms = round((time.time() - start) * 1000, 1)

    result = {
        "matches": matches,
        "query_ms": query_ms,
        "total_searched": total,
        "skipped_sensitive": skipped_sensitive,
        "budget_used": {
            "tier2": half - tier2_remaining,
            "vault": half - vault_remaining,
            "total": budget,
        },
    }

    # Include surfaced IDs when caller needs to bump externally
    if skip_retrieval_increment and surfaced_ids:
        result["surfaced_ids"] = surfaced_ids

    return result


def doc_read(ids: list, include_metadata: bool = True) -> dict:
    """Read specific documents from PostgreSQL by ID.

    Accepts both namespaced IDs (vault::notes/my-note.md) and bare paths
    (notes/my-note.md). Bare paths are automatically prefixed with vault::.

    Args:
        ids: Document IDs (vault-relative paths or namespaced IDs)
        include_metadata: Whether to include metadata in response

    Returns:
        Documents with optional metadata, plus not_found list
    """
    if not ids:
        return {"success": False, "error": "No IDs provided"}

    # Normalize IDs: bare paths get vault:: prefix
    lookup_ids = []
    id_map = {}  # lookup_id -> original_id (for display)
    for doc_id in ids:
        if "::" in doc_id:
            lookup_ids.append(doc_id)
            id_map[doc_id] = doc_id
        else:
            namespaced = f"vault::{doc_id}"
            lookup_ids.append(namespaced)
            id_map[namespaced] = doc_id

    try:
        select_cols = "id, document" + (", metadata" if include_metadata else "")
        sql = f"SELECT {select_cols} FROM jarvis WHERE id = ANY(%s)"
        rows = execute_query(sql, (lookup_ids,))
    except Exception as e:
        return {"success": False, "error": f"Read failed: {e}"}

    found = []
    found_ids = {row["id"] for row in rows}

    not_found = []
    for lid in lookup_ids:
        if lid not in found_ids:
            not_found.append(id_map.get(lid, lid))

    for row in rows:
        entry = {
            "id": row["id"],
            "path": _display_path(row["id"]),
            "document": row["document"],
        }
        if include_metadata and "metadata" in row:
            entry["metadata"] = jsonb_to_metadata(row["metadata"])
        found.append(entry)

    return {
        "success": True,
        "documents": found,
        "not_found": not_found,
    }


def collection_stats(sample_size: int = 5, detailed: bool = False) -> dict:
    """Get memory system health and statistics.

    Args:
        sample_size: Number of sample entries to peek
        detailed: Include per-type/namespace breakdowns and storage size

    Returns:
        Stats dict with count, samples, type distribution
    """
    try:
        count_result = execute_query(
            "SELECT count(*) AS cnt FROM jarvis", fetch="one"
        )
        total = count_result["cnt"] if count_result else 0
    except Exception as e:
        return {"success": False, "error": f"Database unavailable: {e}"}

    if total == 0:
        return {
            "success": True,
            "total_documents": 0,
            "samples": [],
            "message": "No documents indexed. Ask Jarvis to 'index my vault' or use jarvis_index_vault tool.",
        }

    sample_size = min(max(1, sample_size), total)

    try:
        peek_rows = execute_query(
            "SELECT id, metadata FROM jarvis LIMIT %s", (sample_size,)
        )
    except Exception as e:
        return {"success": False, "error": f"Stats failed: {e}"}

    samples = []
    for row in peek_rows:
        meta = jsonb_to_metadata(row["metadata"])
        # Use vault_type for vault entries, fall back to type
        entry_type = meta.get("vault_type") or meta.get("type", "unknown")
        samples.append(
            {
                "id": row["id"],
                "path": _display_path(row["id"]),
                "title": meta.get("title", row["id"]),
                "type": entry_type,
            }
        )

    result = {
        "success": True,
        "total_documents": total,
        "samples": samples,
    }

    # Detailed breakdown
    if detailed:
        try:
            type_rows = execute_query(
                """SELECT metadata->>'type' AS content_type,
                          count(*) AS cnt
                   FROM jarvis
                   GROUP BY content_type"""
            )
            type_counts = {row["content_type"]: row["cnt"] for row in type_rows}

            ns_rows = execute_query(
                """SELECT metadata->>'namespace' AS ns,
                          count(*) AS cnt
                   FROM jarvis
                   GROUP BY ns"""
            )
            namespace_counts = {row["ns"]: row["cnt"] for row in ns_rows}

            result["type_breakdown"] = type_counts
            result["namespace_breakdown"] = namespace_counts

            # Storage size (PostgreSQL table + indexes)
            size_result = execute_query(
                """SELECT pg_total_relation_size('jarvis') AS total_bytes""",
                fetch="one",
            )
            if size_result:
                storage_bytes = size_result["total_bytes"]
                result["storage_bytes"] = storage_bytes
                result["storage_mb"] = round(storage_bytes / (1024 * 1024), 2)

        except Exception as e:
            result["detailed_error"] = str(e)

    return result


# Backward-compatible aliases (will be removed in future version)
memory_read = doc_read
memory_stats = collection_stats
