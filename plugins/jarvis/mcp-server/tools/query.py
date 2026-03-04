"""Vault memory querying for semantic search.

Provides query, read, and stats operations across local.memories and
obsidian.documents schemas with pgvector embeddings. Uses per-schema CTEs
with UNION ALL for cross-schema search (DAR F5: preserves HNSW index usage).

All document IDs use namespaced format (vault:: prefix) for type-safe identification.
"""

import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

from .schema import execute_query, jsonb_to_metadata, metadata_to_jsonb
from .paths import get_path, SENSITIVE_PATHS
from .namespaces import parse_id, ALL_TYPES, schema_for_id, SCHEMA_LOCAL, SCHEMA_OBSIDIAN
from .expansion import expand_query as _expand_query
from .config import (
    get_context_enrichment_config, get_decay_config, get_expansion_config,
    get_ranking_config, get_reranking_config, get_staleness_config,
)
from .format_support import detect_format
from .staleness import check_staleness, deserialize_mtimes


def _parse_row_datetime(value) -> datetime:
    """Parse a datetime from row data (str, datetime, or None → now)."""
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
    return datetime.now(timezone.utc)


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


def _build_core_filter(
    filter_dict: Optional[dict] = None, user: Optional[str] = None
) -> tuple:
    """Build WHERE conditions for local.memories using columns.

    Returns:
        Tuple of (conditions_list, params_list) for SQL WHERE clause.
    """
    conditions = ["status = 'active'"]
    params = []

    if not filter_dict:
        filter_dict = {}

    # Multi-user filter (opt-in, stored in JSONB)
    if user and user != "anonymous":
        conditions.append("metadata->>'user' = %s")
        params.append(user)

    if "type" in filter_dict and filter_dict["type"]:
        type_val = filter_dict["type"]
        if type_val in ALL_TYPES:
            # Map to category column
            conditions.append("category = %s")
            params.append(type_val)
        # Non-content types (note, journal, etc.) don't exist in core — skip

    if "importance" in filter_dict and filter_dict["importance"]:
        try:
            imp_val = float(filter_dict["importance"])
            conditions.append("importance_score >= %s")
            params.append(imp_val)
        except (ValueError, TypeError):
            pass

    if "tags" in filter_dict and filter_dict["tags"]:
        tag = filter_dict["tags"].split(",")[0].strip()
        conditions.append("metadata->>'tags' LIKE %s")
        params.append(f"%{tag}%")

    return conditions, params


def _build_vault_filter(
    filter_dict: Optional[dict] = None, user: Optional[str] = None
) -> tuple:
    """Build WHERE conditions for obsidian.documents using columns.

    Returns:
        Tuple of (conditions_list, params_list) for SQL WHERE clause.
    """
    conditions = []
    params = []

    if not filter_dict:
        filter_dict = {}

    # Multi-user filter (opt-in, stored in JSONB)
    if user and user != "anonymous":
        conditions.append("metadata->>'user' = %s")
        params.append(user)

    if "directory" in filter_dict and filter_dict["directory"]:
        # directory is a proper column now
        conditions.append("directory = %s")
        params.append(filter_dict["directory"])

    if "type" in filter_dict and filter_dict["type"]:
        type_val = filter_dict["type"]
        if type_val in ALL_TYPES:
            # Content type filter — vault is always 'vault', so if they filter
            # for a non-vault content type, exclude vault entirely
            if type_val != "vault":
                conditions.append("1 = 0")  # no match
        else:
            # Vault-entry type (note, journal, work, etc.)
            conditions.append("vault_type = %s")
            params.append(type_val)

    if "importance" in filter_dict and filter_dict["importance"]:
        try:
            imp_val = float(filter_dict["importance"])
            conditions.append("importance_score >= %s")
            params.append(imp_val)
        except (ValueError, TypeError):
            pass

    if "tags" in filter_dict and filter_dict["tags"]:
        tag = filter_dict["tags"].split(",")[0].strip()
        conditions.append("metadata->>'tags' LIKE %s")
        params.append(f"%{tag}%")

    return conditions, params


def _display_path(doc_id: str) -> str:
    """Strip namespace prefix from ID for display purposes."""
    parsed = parse_id(doc_id)
    return parsed.content_id


def _format_core_result(row: dict) -> dict:
    """Format a local.memories row into a result dict.

    Promotes columns (category, scope, source, importance_score) to
    top-level keys. Remaining metadata stays in 'metadata'.
    """
    meta = jsonb_to_metadata(row.get("metadata", {}))
    # Add promoted column values to metadata for downstream compat
    meta["category"] = row.get("category", "observation")
    meta["scope"] = row.get("scope", "global")
    meta["source"] = row.get("source", "auto-extract")
    meta["importance_score"] = str(row.get("importance_score", 0.5))
    meta["type"] = row.get("category", "observation")
    return meta


def _format_vault_result(row: dict) -> dict:
    """Format a obsidian.documents row into a result dict.

    Promotes columns (parent_file, directory, vault_type, etc.) to
    metadata dict for downstream compat.
    """
    meta = jsonb_to_metadata(row.get("metadata", {}))
    meta["parent_file"] = row.get("parent_file", "")
    meta["directory"] = row.get("directory", "")
    meta["vault_type"] = row.get("vault_type", "document")
    meta["title"] = row.get("title", "")
    meta["chunk_index"] = str(row.get("chunk_index", 0))
    meta["chunk_total"] = str(row.get("chunk_total", 1))
    meta["chunk_heading"] = row.get("chunk_heading", "")
    meta["importance_score"] = str(row.get("importance_score", 0.5))
    meta["type"] = "vault"
    return meta


def _increment_retrieval_counts(doc_ids: list, increment: float = 1.0) -> None:
    """Batch increment retrieval counts for local.memories documents.

    Best-effort operation: errors are logged but don't block query response.
    Only updates local.memories (vault documents don't track retrieval counts).

    Uses a single SQL UPDATE with column-level increment (much more efficient
    than the old JSONB string extraction pattern).

    Args:
        doc_ids: List of document IDs to increment
        increment: Amount to add to retrieval_count (default 1.0).
                   Use fractional values (e.g. 0.01) for passive surfacing.
    """
    if not doc_ids:
        return

    try:
        from .schema import _get_pool

        # Filter to only core (non-vault) IDs
        core_ids = [doc_id for doc_id in doc_ids
                    if schema_for_id(doc_id) == SCHEMA_LOCAL]
        if not core_ids:
            return

        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """UPDATE local.memories
                       SET retrieval_count = retrieval_count + %s,
                           updated_at = now()
                       WHERE id = ANY(%s) AND status = 'active'""",
                    (increment, core_ids),
                )
                conn.commit()

    except Exception as e:
        import logging

        logger = logging.getLogger("jarvis-core")
        logger.warning(f"Failed to increment retrieval counts: {e}")


def _cross_schema_search(query_embedding, fetch_count: int,
                         filter_dict: Optional[dict] = None,
                         user: Optional[str] = None) -> list:
    """Execute per-schema search across local + obsidian.

    DAR F5: Each query uses its own HNSW index via ORDER BY ... LIMIT.
    Results are merged in Python by distance.

    Args:
        query_embedding: The query embedding vector
        fetch_count: Max results per schema
        filter_dict: Optional metadata filter dict
        user: Optional user filter for multi-user isolation

    Returns list of row dicts with _schema column ('local' or 'obsidian').
    """
    from .schema import _get_pool

    # Build schema-specific filters
    core_conditions, core_params = _build_core_filter(filter_dict, user)
    vault_conditions, vault_params = _build_vault_filter(filter_dict, user)

    pool = _get_pool()
    rows = []

    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Core query (includes created_at + retrieval_count for decay ranking)
            core_where = " AND ".join(core_conditions)
            cur.execute(
                f"""SELECT id, document, metadata,
                           category, scope, source, importance_score,
                           retrieval_count, created_at,
                           embedding <=> %s::halfvec AS distance,
                           'local' AS _schema
                    FROM local.memories
                    WHERE {core_where}
                    ORDER BY embedding <=> %s::halfvec ASC
                    LIMIT %s""",
                tuple([query_embedding] + core_params + [query_embedding, fetch_count]),
            )
            columns = [desc.name for desc in cur.description]
            for row_data in cur.fetchall():
                rows.append(dict(zip(columns, row_data)))

            # Vault query
            vault_where = " AND ".join(vault_conditions) if vault_conditions else None
            vault_where_clause = f"WHERE {vault_where}" if vault_where else ""
            cur.execute(
                f"""SELECT id, document, metadata,
                           parent_file, directory, vault_type, title,
                           chunk_index, chunk_total, chunk_heading,
                           importance_score,
                           embedding <=> %s::halfvec AS distance,
                           'obsidian' AS _schema
                    FROM obsidian.documents
                    {vault_where_clause}
                    ORDER BY embedding <=> %s::halfvec ASC
                    LIMIT %s""",
                tuple([query_embedding] + vault_params + [query_embedding, fetch_count]),
            )
            columns = [desc.name for desc in cur.description]
            for row_data in cur.fetchall():
                rows.append(dict(zip(columns, row_data)))

    # Sort merged results by distance
    rows.sort(key=lambda r: float(r.get("distance", 999)))
    return rows


def query_vault(
    query: str,
    n_results: int = 5,
    filter: Optional[dict] = None,
    user: Optional[str] = None,
) -> dict:
    """Semantic search across vault memory.

    Searches both local.memories and obsidian.documents using per-schema CTEs
    for optimal HNSW index usage.

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
        # Check total across both schemas
        core_count = execute_query(
            "SELECT count(*) AS cnt FROM local.memories WHERE status = 'active'",
            fetch="one",
        )
        vault_count = execute_query(
            "SELECT count(*) AS cnt FROM obsidian.documents", fetch="one"
        )
        total = (core_count["cnt"] if core_count else 0) + (
            vault_count["cnt"] if vault_count else 0
        )
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
        rows = _cross_schema_search(
            query_embedding, fetch_count, filter_dict=filter, user=user
        )
    except Exception as e:
        return {"success": False, "error": f"Query failed: {e}"}

    # Build raw result entries with relevance scores
    decay_config = get_decay_config()
    ranking_cfg = get_ranking_config()
    use_decay = decay_config.get("enabled", True)

    raw_entries = []
    for row in rows:
        schema = row.get("_schema", "obsidian")

        if schema == "local":
            meta = _format_core_result(row)
        else:
            meta = _format_vault_result(row)

        distance = float(row["distance"])
        similarity = 1.0 - (distance / 2.0)

        # Use numeric importance_score
        imp_score = 0.5
        imp_str = meta.get("importance_score")
        if imp_str:
            try:
                imp_score = float(imp_str)
            except (ValueError, TypeError):
                pass

        # Two-phase ranking: blended score with decay (core only)
        if use_decay and schema == "local":
            from .ranking import compute_blended_score
            blended, eff_imp = compute_blended_score(
                similarity=similarity,
                base_importance=imp_score,
                created_at=_parse_row_datetime(row.get("created_at") or meta.get("created_at")),
                last_retrieved_at=_parse_row_datetime(meta.get("last_retrieved_at")),
                retrieval_count=int(float(meta.get("retrieval_count", 0))),
                similarity_weight=ranking_cfg.get("similarity_weight", 0.7),
                importance_weight=ranking_cfg.get("importance_weight", 0.3),
                decay_config=decay_config,
            )
            relevance = blended
        else:
            # Vault docs + fallback: original relevance formula
            importance = meta.get("importance", "medium")
            updated_at = meta.get("updated_at")
            relevance = _compute_relevance(distance, importance, updated_at, imp_score)

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
                "_schema": schema,
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

        schema = entry.get("_schema", "obsidian")
        if schema == "obsidian":
            source = "vault"
        else:
            source = meta.get("category", meta.get("type", "memory"))

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
            "schema": schema,
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

    # Increment retrieval counts for core results (best-effort, non-blocking)
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

    Searches both local.memories and obsidian.documents. Differs from query_vault():
    - Applies minimum relevance threshold (filters low-scoring results)
    - Budget-based allocation: core content shown in full, vault as references
    - Fractionally increments retrieval counts (configurable, default 0.01)
    - Filters out sensitive directories (documents/, people/)
    - Returns query_ms timing for performance monitoring
    - Silently returns empty on any error (graceful degradation)

    Budget strategy:
    - Total budget is split 50/50 between core and vault
    - Core items (observations, learnings, etc.) are shown with full content
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

        core_count = execute_query(
            "SELECT count(*) AS cnt FROM local.memories WHERE status = 'active'",
            fetch="one",
        )
        vault_count = execute_query(
            "SELECT count(*) AS cnt FROM obsidian.documents", fetch="one"
        )
        total = (core_count["cnt"] if core_count else 0) + (
            vault_count["cnt"] if vault_count else 0
        )
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
        rows = _cross_schema_search(query_embedding, fetch_count)
    except Exception:
        return {"matches": [], "query_ms": 0, "total_searched": total}

    # Build raw entries with relevance scores
    decay_config = get_decay_config()
    ranking_cfg = get_ranking_config()
    use_decay = decay_config.get("enabled", True)

    raw_entries = []
    skipped_sensitive = 0

    for row in rows:
        schema = row.get("_schema", "obsidian")

        if schema == "local":
            meta = _format_core_result(row)
        else:
            meta = _format_vault_result(row)

        distance = float(row["distance"])
        similarity = 1.0 - (distance / 2.0)

        # Filter sensitive directories
        directory = meta.get("directory", "")
        if directory in SENSITIVE_PATHS:
            skipped_sensitive += 1
            continue

        # Filter superseded core entries (enforced by active_memories view,
        # but also check metadata for safety)
        if meta.get("status") == "superseded":
            continue

        # Use numeric importance_score
        imp_score = 0.5
        imp_str = meta.get("importance_score")
        if imp_str:
            try:
                imp_score = float(imp_str)
            except (ValueError, TypeError):
                pass

        # Two-phase ranking with decay (core only)
        if use_decay and schema == "local":
            from .ranking import compute_blended_score
            blended, eff_imp = compute_blended_score(
                similarity=similarity,
                base_importance=imp_score,
                created_at=_parse_row_datetime(row.get("created_at") or meta.get("created_at")),
                last_retrieved_at=_parse_row_datetime(meta.get("last_retrieved_at")),
                retrieval_count=int(float(meta.get("retrieval_count", 0))),
                similarity_weight=ranking_cfg.get("similarity_weight", 0.7),
                importance_weight=ranking_cfg.get("importance_weight", 0.3),
                decay_config=decay_config,
            )
            relevance = blended
        else:
            importance = meta.get("importance", "medium")
            updated_at = meta.get("updated_at")
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
                "_schema": schema,
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
    core_remaining = half
    vault_remaining = half
    selected = []

    for entry in deduped:
        schema = entry.get("_schema", "obsidian")
        is_vault = schema == "obsidian"

        if is_vault:
            cost = VAULT_REF_COST
            if vault_remaining >= cost:
                vault_remaining -= cost
                entry["display_mode"] = "reference"
                selected.append(entry)
            elif core_remaining >= cost:
                core_remaining -= cost
                entry["display_mode"] = "reference"
                selected.append(entry)
        else:
            content_len = len(entry["document"] or "")
            cost = max(content_len, 50)  # minimum 50 chars cost
            if core_remaining >= cost:
                core_remaining -= cost
                entry["display_mode"] = "full"
                selected.append(entry)
            elif vault_remaining >= cost:
                vault_remaining -= cost
                entry["display_mode"] = "full"
                selected.append(entry)

    # Fractional retrieval bump for passively surfaced results
    surfaced_ids = [entry["doc_id"] for entry in selected] if selected else []
    if selected and not skip_retrieval_increment:
        enrichment_config = get_context_enrichment_config()
        passive_increment = enrichment_config.get("passive_retrieval_increment", 0.01)
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
            # Core memory: full content, no truncation
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
            "core": half - core_remaining,
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

    Routes by ID prefix: vault:: → obsidian.documents, else → local.memories.

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
    vault_ids = []
    core_ids = []
    id_map = {}  # lookup_id -> original_id (for display)

    for doc_id in ids:
        if "::" not in doc_id:
            namespaced = f"vault::{doc_id}"
            id_map[namespaced] = doc_id
            vault_ids.append(namespaced)
        elif doc_id.startswith("vault::"):
            id_map[doc_id] = doc_id
            vault_ids.append(doc_id)
        else:
            id_map[doc_id] = doc_id
            core_ids.append(doc_id)

    found = []
    found_ids = set()

    try:
        # Read vault documents
        if vault_ids:
            meta_cols = ", metadata, parent_file, directory, vault_type, title, chunk_index, chunk_total, chunk_heading, importance_score" if include_metadata else ""
            sql = f"SELECT id, document{meta_cols} FROM obsidian.documents WHERE id = ANY(%s)"
            rows = execute_query(sql, (vault_ids,))
            for row in rows:
                entry = {
                    "id": row["id"],
                    "path": _display_path(row["id"]),
                    "document": row["document"],
                }
                if include_metadata:
                    entry["metadata"] = _format_vault_result(row)
                found.append(entry)
                found_ids.add(row["id"])

        # Read core memories
        if core_ids:
            meta_cols = ", metadata, category, scope, source, importance_score" if include_metadata else ""
            sql = f"SELECT id, document{meta_cols} FROM local.memories WHERE id = ANY(%s)"
            rows = execute_query(sql, (core_ids,))
            for row in rows:
                entry = {
                    "id": row["id"],
                    "path": _display_path(row["id"]),
                    "document": row["document"],
                }
                if include_metadata:
                    entry["metadata"] = _format_core_result(row)
                found.append(entry)
                found_ids.add(row["id"])

    except Exception as e:
        return {"success": False, "error": f"Read failed: {e}"}

    all_lookup_ids = vault_ids + core_ids
    not_found = [
        id_map.get(lid, lid) for lid in all_lookup_ids if lid not in found_ids
    ]

    return {
        "success": True,
        "documents": found,
        "not_found": not_found,
    }


def collection_stats(sample_size: int = 5, detailed: bool = False) -> dict:
    """Get memory system health and statistics.

    Queries both local.memories and obsidian.documents.

    Args:
        sample_size: Number of sample entries to peek
        detailed: Include per-type/namespace breakdowns and storage size

    Returns:
        Stats dict with count, samples, type distribution
    """
    try:
        core_count = execute_query(
            "SELECT count(*) AS cnt FROM local.memories WHERE status = 'active'",
            fetch="one",
        )
        vault_count = execute_query(
            "SELECT count(*) AS cnt FROM obsidian.documents", fetch="one"
        )
        core_total = core_count["cnt"] if core_count else 0
        vault_total = vault_count["cnt"] if vault_count else 0
        total = core_total + vault_total
    except Exception as e:
        return {"success": False, "error": f"Database unavailable: {e}"}

    if total == 0:
        return {
            "success": True,
            "total_documents": 0,
            "samples": [],
            "message": "No documents indexed. Ask Jarvis to 'index my vault' or use jarvis_index_vault tool.",
        }

    # Peek at samples from both schemas
    samples = []
    half_sample = max(1, sample_size // 2)

    try:
        # Core samples
        core_rows = execute_query(
            "SELECT id, metadata, category FROM local.memories WHERE status = 'active' LIMIT %s",
            (half_sample,),
        )
        for row in core_rows:
            meta = jsonb_to_metadata(row.get("metadata", {}))
            samples.append(
                {
                    "id": row["id"],
                    "path": _display_path(row["id"]),
                    "title": meta.get("title", row["id"]),
                    "type": row.get("category", meta.get("type", "unknown")),
                    "schema": "local",
                }
            )

        # Vault samples
        vault_rows = execute_query(
            "SELECT id, metadata, vault_type, title FROM obsidian.documents LIMIT %s",
            (half_sample,),
        )
        for row in vault_rows:
            samples.append(
                {
                    "id": row["id"],
                    "path": _display_path(row["id"]),
                    "title": row.get("title", row["id"]),
                    "type": row.get("vault_type", "document"),
                    "schema": "obsidian",
                }
            )
    except Exception as e:
        return {"success": False, "error": f"Stats failed: {e}"}

    result = {
        "success": True,
        "total_documents": total,
        "core_documents": core_total,
        "vault_documents": vault_total,
        "samples": samples,
    }

    # Detailed breakdown
    if detailed:
        try:
            # Core breakdown by category
            cat_rows = execute_query(
                """SELECT category, count(*) AS cnt
                   FROM local.memories
                   WHERE status = 'active'
                   GROUP BY category"""
            )
            category_counts = {row["category"]: row["cnt"] for row in cat_rows}

            # Vault breakdown by vault_type
            vt_rows = execute_query(
                """SELECT vault_type, count(*) AS cnt
                   FROM obsidian.documents
                   GROUP BY vault_type"""
            )
            vault_type_counts = {row["vault_type"]: row["cnt"] for row in vt_rows}

            result["category_breakdown"] = category_counts
            result["vault_type_breakdown"] = vault_type_counts

            # Storage size (both tables)
            for table, label in [
                ("local.memories", "core_storage"),
                ("obsidian.documents", "vault_storage"),
            ]:
                try:
                    size_result = execute_query(
                        f"SELECT pg_total_relation_size('{table}') AS total_bytes",
                        fetch="one",
                    )
                    if size_result:
                        result[f"{label}_bytes"] = size_result["total_bytes"]
                        result[f"{label}_mb"] = round(
                            size_result["total_bytes"] / (1024 * 1024), 2
                        )
                except Exception:
                    pass

        except Exception as e:
            result["detailed_error"] = str(e)

    return result


# Backward-compatible aliases (will be removed in future version)
memory_read = doc_read
memory_stats = collection_stats
