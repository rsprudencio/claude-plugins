"""Vault memory querying for semantic search.

Provides query, read, and stats operations across local.memories and
obsidian.documents schemas with pgvector embeddings. Uses per-schema CTEs
with UNION ALL for cross-schema search (DAR F5: preserves HNSW index usage).

All document IDs use namespaced format (vault:: prefix) for type-safe identification.
"""

import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

import numpy as np

from .schema import execute_query, jsonb_to_metadata, metadata_to_jsonb
from .paths import get_path, SENSITIVE_PATHS
from .namespaces import parse_id, ALL_TYPES, schema_for_id, SCHEMA_LOCAL, SCHEMA_OBSIDIAN
from .expansion import expand_query as _expand_query
from .config import (
    get_context_enrichment_config, get_decay_config, get_expansion_config,
    get_ranking_config, get_reranking_config, get_staleness_config,
)
from .format_support import detect_format
from .ranking import DEFAULT_IMPORTANCE_WEIGHT, compute_unified_score, score_memory
from .staleness import check_staleness, deserialize_mtimes


logger = logging.getLogger(__name__)

_INJECTION_DEDUP_TYPES = {"memory", "observation", "worklog"}
_INJECTION_TYPE_PRIORITY = {"memory": 3, "observation": 2, "worklog": 1}


def _semantic_deduplicate_context(
    entries: list[dict],
    embedding_service,
    similarity_threshold: float,
) -> tuple[list[dict], int]:
    """Collapse redundant local memory/observation/worklog candidates.

    Only records from the same project are compared. Durable strategic memories
    win over observations and worklogs even when a transient record has a small
    ranking advantage. Any embedding failure preserves the original candidates.
    """
    eligible = []
    for index, entry in enumerate(entries):
        meta = entry.get("metadata") or {}
        doc_type = str(meta.get("type") or "").lower()
        if entry.get("_schema") != SCHEMA_LOCAL or doc_type not in _INJECTION_DEDUP_TYPES:
            continue
        document = entry.get("document") or ""
        if document.strip():
            eligible.append((index, entry, doc_type, document))

    if len(eligible) < 2:
        return entries, 0

    try:
        vectors = np.asarray(
            embedding_service.encode_batch([item[3] for item in eligible]),
            dtype=np.float32,
        )
        norms = np.linalg.norm(vectors, axis=1, keepdims=True)
        vectors = vectors / np.clip(norms, a_min=1e-12, a_max=None)
    except Exception as exc:
        logger.warning("Semantic injection dedup skipped: %s", exc)
        return entries, 0

    threshold = min(max(float(similarity_threshold), -1.0), 1.0)
    ordered = sorted(
        range(len(eligible)),
        key=lambda pos: (
            -_INJECTION_TYPE_PRIORITY[eligible[pos][2]],
            -float(eligible[pos][1].get("relevance", 0.0)),
        ),
    )
    kept_positions: list[int] = []
    suppressed_indexes: set[int] = set()

    for pos in ordered:
        index, entry, _, _ = eligible[pos]
        project = str((entry.get("metadata") or {}).get("project") or "")
        duplicate = False
        for kept_pos in kept_positions:
            kept_entry = eligible[kept_pos][1]
            kept_project = str(
                (kept_entry.get("metadata") or {}).get("project") or ""
            )
            if project != kept_project:
                continue
            if float(np.dot(vectors[pos], vectors[kept_pos])) >= threshold:
                duplicate = True
                break
        if duplicate:
            suppressed_indexes.add(index)
        else:
            kept_positions.append(pos)

    if not suppressed_indexes:
        return entries, 0
    return (
        [entry for index, entry in enumerate(entries) if index not in suppressed_indexes],
        len(suppressed_indexes),
    )


def _parse_optional_row_datetime(value) -> Optional[datetime]:
    """Parse an optional datetime from row data without inventing a timestamp."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            pass
    return None


def _parse_row_datetime(value) -> datetime:
    """Parse a required datetime from row data (missing/invalid → now)."""
    return _parse_optional_row_datetime(value) or datetime.now(timezone.utc)


def _annotate_staleness(raw_entries: list, staleness_config: dict) -> None:
    """In-place annotate raw query entries with staleness information.

    Only processes entries in the obs:: namespace (auto-extracted observations).
    Reads file_mtimes from already-fetched metadata and compares against current
    filesystem state. No additional database operations.

    Skips remote schema entries: their file_mtimes reference paths on the
    remote machine that don't exist locally, so os.stat() always fails,
    producing a permanent false-positive staleness penalty.

    Args:
        raw_entries: List of entry dicts with 'doc_id', 'metadata', 'relevance' keys.
        staleness_config: Config dict with 'penalty' (float) key.
    """
    penalty = staleness_config.get("penalty", 0.15)

    for entry in raw_entries:
        doc_id = entry.get("doc_id", "")
        if not doc_id.startswith("obs::"):
            continue

        # Skip remote entries — file paths are from the remote machine
        schema = entry.get("_schema", "")
        if schema.startswith("remote_"):
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
            # No floor: scores are unclamped (Layer 4), and flooring at 0.0
            # would let a stale entry outrank fresher negative-scored ones.
            entry["relevance"] -= penalty


def _detect_format_from_entry(entry: dict) -> str:
    """Detect format from a query result entry's parent_file path."""
    parent_file = entry.get("parent_file", "")
    return detect_format(parent_file) if parent_file else "markdown"


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


# Keys that map to dedicated columns — skip in generic JSONB loop
_KNOWN_CORE_KEYS = {"type", "importance", "tags", "scope", "project"}
_KNOWN_VAULT_KEYS = {"type", "importance", "tags", "directory"}


def _build_core_filter(
    filter_dict: Optional[dict] = None, user: Optional[str] = None
) -> tuple:
    """Build WHERE conditions for local.memories using columns.

    Known keys (type, importance, tags) map to dedicated columns.
    Any other key falls back to a JSONB equality check on the metadata column.

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

    if "scope" in filter_dict and filter_dict["scope"]:
        conditions.append("scope = %s")
        params.append(filter_dict["scope"])

    if "project" in filter_dict and filter_dict["project"]:
        conditions.append("project = %s")
        params.append(filter_dict["project"])

    # Generic JSONB fallback: any unknown key → metadata->>'key' = value
    for key, val in filter_dict.items():
        if key in _KNOWN_CORE_KEYS or not val:
            continue
        conditions.append("metadata->>%s = %s")
        params.extend([key, str(val)])

    return conditions, params


def _build_vault_filter(
    filter_dict: Optional[dict] = None, user: Optional[str] = None
) -> tuple:
    """Build WHERE conditions for obsidian.documents using columns.

    Known keys (type, importance, tags, directory) map to dedicated columns.
    Any other key falls back to a JSONB equality check on the metadata column.

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

    # Generic JSONB fallback: any unknown key → metadata->>'key' = value
    for key, val in filter_dict.items():
        if key in _KNOWN_VAULT_KEYS or not val:
            continue
        conditions.append("metadata->>%s = %s")
        params.extend([key, str(val)])

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


def _parse_schemas(schemas_str: Optional[str]) -> Optional[list[str]]:
    """Parse a schemas string into a list of schema names.

    Args:
        schemas_str: 'all', None, or comma-separated schema names
                     (e.g. 'local,remote_personio').

    Returns:
        None (= search all registered schemas) or a list of schema names.
    """
    if schemas_str is None or schemas_str.strip().lower() == "all":
        return None
    return [s.strip() for s in schemas_str.split(",") if s.strip()]


def _query_local_schema(pool, query_embedding, fetch_count: int,
                        filter_dict: Optional[dict], user: Optional[str]) -> list:
    """Query local.memories with HNSW ordering."""
    core_conditions, core_params = _build_core_filter(filter_dict, user)
    core_where = " AND ".join(core_conditions)
    with pool.connection() as conn:
        with conn.cursor() as cur:
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
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def _query_obsidian_schema(pool, query_embedding, fetch_count: int,
                           filter_dict: Optional[dict], user: Optional[str]) -> list:
    """Query obsidian.documents with HNSW ordering."""
    vault_conditions, vault_params = _build_vault_filter(filter_dict, user)
    vault_where = " AND ".join(vault_conditions) if vault_conditions else None
    vault_where_clause = f"WHERE {vault_where}" if vault_where else ""
    with pool.connection() as conn:
        with conn.cursor() as cur:
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
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def _query_remote_schema(pool, query_embedding, fetch_count: int,
                         filter_dict: Optional[dict], user: Optional[str],
                         entry) -> list:
    """Query a remote mirror schema (same column structure as local.memories).

    Remote mirror retrieval_count stays frozen after this query — mirrors are
    read-only. This is intentional: remote retrieval patterns should not
    influence local ranking (D12).

    Args:
        entry: SchemaEntry from the schema registry (provides name + table).
    """
    from psycopg import sql as psql
    from .schema_registry import is_valid_pg_identifier

    # Defence-in-depth: validate both identifiers before SQL composition
    if not is_valid_pg_identifier(entry.name):
        import logging as _logging
        _logging.getLogger("jarvis-core").error(
            "Invalid schema name in remote query (skipping): %r", entry.name)
        return []
    if not is_valid_pg_identifier(entry.table):
        import logging as _logging
        _logging.getLogger("jarvis-core").error(
            "Invalid table name in remote query (skipping): %r", entry.table)
        return []

    core_conditions, core_params = _build_core_filter(filter_dict, user)
    core_where = " AND ".join(core_conditions)

    query = psql.SQL(
        "SELECT id, document, metadata, "
        "category, scope, source, importance_score, "
        "retrieval_count, created_at, "
        "embedding <=> %s::halfvec AS distance, "
        "%s AS _schema "
        "FROM {schema}.{table} "
        "WHERE {where} "
        "ORDER BY embedding <=> %s::halfvec ASC "
        "LIMIT %s"
    ).format(
        schema=psql.Identifier(entry.name),
        table=psql.Identifier(entry.table),
        where=psql.SQL(core_where),
    )

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                query,
                tuple([query_embedding] + core_params + [entry.name, query_embedding, fetch_count]),
            )
            columns = [desc.name for desc in cur.description]
            return [dict(zip(columns, row)) for row in cur.fetchall()]


def _remote_count(entry) -> int:
    """Count active rows in a remote schema using safe identifier composition.

    Args:
        entry: SchemaEntry with validated name and table fields.

    Returns:
        Row count, or 0 on error.
    """
    from psycopg import sql as psql
    from .schema import _get_pool

    query = psql.SQL(
        "SELECT count(*) AS cnt FROM {schema}.{table} WHERE status = 'active'"
    ).format(
        schema=psql.Identifier(entry.name),
        table=psql.Identifier(entry.table),
    )

    pool = _get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query)
            row = cur.fetchone()
            return row[0] if row else 0


def _cross_schema_search(query_embedding, fetch_count: int,
                         filter_dict: Optional[dict] = None,
                         user: Optional[str] = None,
                         schemas: Optional[list[str]] = None) -> list:
    """Execute per-schema search across all registered searchable schemas.

    Registry-driven (N schemas): iterates get_searchable_schemas() instead of
    hardcoding local + obsidian. Each schema query uses its own pool connection
    for per-schema error isolation (D7).

    Args:
        query_embedding: The query embedding vector
        fetch_count: Max results per schema
        filter_dict: Optional metadata filter dict
        user: Optional user filter for multi-user isolation
        schemas: Optional list of schema names to restrict search.
                 None = all registered searchable schemas.

    Returns:
        List of row dicts with _schema column, sorted by distance ascending.

    Raises:
        ValueError: If schemas filter contains unknown schema names (D8).
    """
    from .schema import _get_pool
    from .schema_registry import (
        get_searchable_schemas, SchemaKind, SchemaEntry,
        is_valid_schema_name,
    )
    from .config import get_embedding_config

    # Get all searchable schemas from registry
    all_searchable = get_searchable_schemas()
    if not all_searchable:
        # Defensive fallback: registry not yet initialized
        all_searchable = [
            SchemaEntry(name=SCHEMA_LOCAL, kind=SchemaKind.LOCAL, table="memories"),
            SchemaEntry(name=SCHEMA_OBSIDIAN, kind=SchemaKind.OBSIDIAN, table="documents"),
        ]

    # D8: Validate and filter by caller-specified schemas
    if schemas is not None:
        available_names = {e.name for e in all_searchable}
        unknown = set(schemas) - available_names
        if unknown:
            raise ValueError(
                f"Unknown schemas: {sorted(unknown)}. "
                f"Available: {sorted(available_names)}"
            )
        searchable = [e for e in all_searchable if e.name in schemas]
    else:
        searchable = all_searchable

    # D2: Skip schemas whose embedding_model doesn't match the active model
    try:
        active_model = get_embedding_config().get("model")
    except Exception:
        active_model = None

    compatible = []
    for entry in searchable:
        model = entry.metadata.get("embedding_model")
        if model and active_model and model != active_model:
            import logging as _logging
            _logging.getLogger("jarvis-core").warning(
                "Skipping schema %s: embedding model mismatch (%s != %s)",
                entry.name, model, active_model,
            )
            continue
        compatible.append(entry)

    pool = _get_pool()
    rows: list = []

    # D7: Per-schema try/except — one schema failing does not abort others
    for entry in compatible:
        try:
            if entry.kind == SchemaKind.LOCAL:
                schema_rows = _query_local_schema(pool, query_embedding, fetch_count, filter_dict, user)
            elif entry.kind == SchemaKind.OBSIDIAN:
                schema_rows = _query_obsidian_schema(pool, query_embedding, fetch_count, filter_dict, user)
            elif entry.kind == SchemaKind.REMOTE:
                schema_rows = _query_remote_schema(pool, query_embedding, fetch_count, filter_dict, user, entry)
            else:
                continue
            rows.extend(schema_rows)
        except Exception as e:
            import logging as _logging
            _logging.getLogger("jarvis-core").error(
                "Schema %s query failed (skipping): %s", entry.name, e
            )
            continue

    # Sort merged results by distance
    rows.sort(key=lambda r: float(r.get("distance", 999)))
    return rows


def query_vault(
    query: str,
    n_results: int = 5,
    filter: Optional[dict] = None,
    user: Optional[str] = None,
    schemas: Optional[list[str]] = None,
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
        # Check total across local + obsidian (required schemas)
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

    # D4: Add remote schema counts (best-effort — failures don't abort the query)
    from .schema_registry import get_searchable_schemas, SchemaKind, is_valid_pg_identifier
    for _re in get_searchable_schemas(kind=SchemaKind.REMOTE):
        if not is_valid_pg_identifier(_re.name) or not is_valid_pg_identifier(_re.table):
            continue
        try:
            _rc = _remote_count(_re)
            total += _rc
        except Exception:
            pass

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

    # Over-fetch to account for chunk deduplication (and reranking if enabled).
    # The window fills by raw vector distance BEFORE per-file chunk dedup, so
    # a multi-chunk document can crowd out others — overfetch_factor (default 5)
    # sizes the window to survive dedup.
    reranking_config = get_reranking_config()
    if reranking_config.get("enabled", False):
        fetch_count = min(reranking_config.get("candidate_count", 100), total)
    else:
        overfetch = get_ranking_config().get("overfetch_factor", 5)
        fetch_count = min(n_results * max(int(overfetch), 1), 60, total)

    try:
        rows = _cross_schema_search(
            query_embedding, fetch_count, filter_dict=filter, user=user,
            schemas=schemas,
        )
    except ValueError as e:
        # D8: Unknown schema names in the filter
        return {"success": False, "error": str(e)}
    except Exception as e:
        return {"success": False, "error": f"Query failed: {e}"}

    # Build raw result entries with relevance scores
    decay_config = get_decay_config()
    ranking_cfg = get_ranking_config()
    use_decay = decay_config.get("enabled", True)

    # D12: Core-like schemas (LOCAL + REMOTE) support blended decay scoring —
    # they share the local.memories column structure.
    from .schema_registry import _core_like_schemas as _get_core_like
    core_like = _get_core_like()

    raw_entries = []
    for row in rows:
        schema = row.get("_schema", "obsidian")

        # Remote mirrors share the local.memories structure; obsidian has its own
        if schema in core_like:
            meta = _format_core_result(row)
        else:
            meta = _format_vault_result(row)

        distance = float(row["distance"])
        # pgvector cosine distance is ``1 - cosine_similarity``.
        similarity = 1.0 - distance

        # Use numeric importance_score
        imp_score = 0.5
        imp_str = meta.get("importance_score")
        if imp_str:
            try:
                imp_score = float(imp_str)
            except (ValueError, TypeError):
                pass

        # Unified scoring (Layer 4): one formula for every schema. Memories get
        # decay-adjusted importance; vault chunks use raw importance_score. No
        # recency term — updated_at is the reindex timestamp, not an authoring
        # date, so it would add a constant cross-schema offset after reindexes.
        # Note: remote retrieval_count stays frozen (mirrors are read-only) — this
        # is intentional; remote retrieval patterns should not influence local ranking.
        importance_weight = ranking_cfg.get(
            "importance_weight", DEFAULT_IMPORTANCE_WEIGHT
        )
        if use_decay and schema in core_like:
            relevance, _ = score_memory(
                similarity=similarity,
                base_importance=imp_score,
                created_at=_parse_row_datetime(row.get("created_at") or meta.get("created_at")),
                last_retrieved_at=_parse_optional_row_datetime(
                    meta.get("last_retrieved_at")
                ),
                retrieval_count=int(float(meta.get("retrieval_count", 0))),
                importance_weight=importance_weight,
                decay_config=decay_config,
            )
        else:
            relevance = compute_unified_score(
                similarity, imp_score, importance_weight=importance_weight
            )

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
                "similarity": similarity,
                "document": row["document"],
                "metadata": meta,
                "_schema": schema,
            }
        )

    # Staleness annotation: penalize observations whose tracked files changed
    staleness_config = get_staleness_config()
    if staleness_config.get("enabled", True):
        _annotate_staleness(raw_entries, staleness_config)

    # D9: Compound dedup key (parent_file, schema) — avoids collapsing entries
    # from different schemas that happen to share a parent_file name.
    best_per_file: dict = {}
    for entry in raw_entries:
        key = (entry["parent_file"], entry["_schema"])
        if key not in best_per_file or entry["relevance"] > best_per_file[key]["relevance"]:
            best_per_file[key] = entry

    # D9: Cross-schema dedup — if same doc_id appears in both local and a remote
    # mirror (echo dedup miss), prefer the local copy.
    id_to_key: dict = {}
    keys_to_remove: set = set()
    for key, entry in list(best_per_file.items()):
        doc_id = entry["doc_id"]
        if doc_id in id_to_key:
            existing_key = id_to_key[doc_id]
            if entry["_schema"] == SCHEMA_LOCAL:
                keys_to_remove.add(existing_key)
                id_to_key[doc_id] = key
            else:
                keys_to_remove.add(key)
        else:
            id_to_key[doc_id] = key
    for k in keys_to_remove:
        best_per_file.pop(k, None)

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

    # Sort by relevance descending and trim to the caller's n_results —
    # reranking rescores candidates but never expands the requested count.
    final_count = n_results
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
            "similarity": round(entry.get("similarity", 0.0), 3),
            "preview": preview,
            "tags": tags,
            "schema": schema,
            "source": source,
        }
        # Provenance: tag results from remote mirror schemas
        if schema.startswith("remote_"):
            result_entry["source_remote"] = schema
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
    threshold: float = 0.85,
    budget: int = 8000,
    skip_retrieval_increment: bool = False,
    schemas: Optional[list[str]] = None,
    max_results: int = 20,
) -> dict:
    """Search vault memories for per-prompt context injection.

    Searches both local.memories and obsidian.documents. Differs from query_vault():
    - Applies a minimum raw-cosine threshold (filters low-scoring results)
    - Caps the injected result count independently of the character budget
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
        threshold: Minimum raw cosine similarity (default 0.85)
        budget: Total character budget for injection (default 8000, split 50/50)
        max_results: Maximum matches to inject after ranking/deduplication
            (default 20, clamped to 1-100). The budget may reduce this further.
        skip_retrieval_increment: If True, skip the retrieval count write-back.
            Surfaced IDs are returned in the result dict so the caller can
            bump them externally (e.g., via HTTP POST to the MCP server).

    Returns:
        Dict with matches list and metadata. When skip_retrieval_increment=True,
        includes 'surfaced_ids' list for deferred bumping.
    """
    start = time.time()
    try:
        max_results = int(max_results)
    except (TypeError, ValueError):
        max_results = 20
    max_results = min(max(max_results, 1), 100)

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

    # D4: Add remote schema counts (best-effort)
    from .schema_registry import get_searchable_schemas as _gss, SchemaKind as _SK, is_valid_pg_identifier as _vpi
    for _re in _gss(kind=_SK.REMOTE):
        if not _vpi(_re.name) or not _vpi(_re.table):
            continue
        try:
            total += _remote_count(_re)
        except Exception:
            pass

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
        rows = _cross_schema_search(query_embedding, fetch_count, schemas=schemas)
    except Exception:
        return {"matches": [], "query_ms": 0, "total_searched": total}

    # Build raw entries with relevance scores
    decay_config = get_decay_config()
    ranking_cfg = get_ranking_config()
    use_decay = decay_config.get("enabled", True)

    # D12: Pre-compute core-like set for scoring gate (LOCAL + REMOTE schemas)
    from .schema_registry import _core_like_schemas as _get_core_like_sc
    core_like_sc = _get_core_like_sc()

    raw_entries = []
    skipped_sensitive = 0

    for row in rows:
        schema = row.get("_schema", "obsidian")

        # Remote mirrors share the local.memories structure; obsidian has its own
        if schema in core_like_sc:
            meta = _format_core_result(row)
        else:
            meta = _format_vault_result(row)

        distance = float(row["distance"])
        # pgvector cosine distance is ``1 - cosine_similarity``.
        similarity = 1.0 - distance

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

        # Threshold gates on RAW similarity, before any importance boost —
        # relevance (gate) and ranking (boost) stay decoupled, so importance
        # can never rescue an off-topic match past the gate.
        if similarity < threshold:
            continue

        # Unified scoring (Layer 4): one formula for every schema. Memories get
        # decay-adjusted importance; vault chunks use raw importance_score. No
        # recency term (updated_at is the reindex timestamp, not authoring date).
        # Remote retrieval_count stays frozen — mirrors are read-only.
        importance_weight = ranking_cfg.get(
            "importance_weight", DEFAULT_IMPORTANCE_WEIGHT
        )
        if use_decay and schema in core_like_sc:
            relevance, _ = score_memory(
                similarity=similarity,
                base_importance=imp_score,
                created_at=_parse_row_datetime(row.get("created_at") or meta.get("created_at")),
                last_retrieved_at=_parse_optional_row_datetime(
                    meta.get("last_retrieved_at")
                ),
                retrieval_count=int(float(meta.get("retrieval_count", 0))),
                importance_weight=importance_weight,
                decay_config=decay_config,
            )
        else:
            relevance = compute_unified_score(
                similarity, imp_score, importance_weight=importance_weight
            )

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
                "similarity": similarity,
                "document": row["document"],
                "metadata": meta,
                "_schema": schema,
            }
        )

    # Staleness annotation: penalize observations whose tracked files changed
    staleness_config = get_staleness_config()
    if staleness_config.get("enabled", True):
        _annotate_staleness(raw_entries, staleness_config)

    # D9: Compound dedup key (parent_file, schema)
    best_per_file: dict = {}
    for entry in raw_entries:
        key = (entry["parent_file"], entry["_schema"])
        if key not in best_per_file or entry["relevance"] > best_per_file[key]["relevance"]:
            best_per_file[key] = entry

    # D9: Cross-schema dedup — local wins over remote for same doc_id
    id_to_key_sc: dict = {}
    keys_to_remove_sc: set = set()
    for key, entry in list(best_per_file.items()):
        doc_id = entry["doc_id"]
        if doc_id in id_to_key_sc:
            existing_key = id_to_key_sc[doc_id]
            if entry["_schema"] == SCHEMA_LOCAL:
                keys_to_remove_sc.add(existing_key)
                id_to_key_sc[doc_id] = key
            else:
                keys_to_remove_sc.add(key)
        else:
            id_to_key_sc[doc_id] = key
    for k in keys_to_remove_sc:
        best_per_file.pop(k, None)

    # Cross-encoder reranking: rescore all candidates regardless of schema.
    # Runs after dedup (fewer candidates = faster inference).
    reranking_config = get_reranking_config()
    if reranking_config.get("enabled") and len(best_per_file) > 1:
        from .reranking import rerank

        candidates = sorted(
            best_per_file.values(), key=lambda e: e["relevance"], reverse=True
        )
        docs = [e["document"] or "" for e in candidates]
        vscores = [e["relevance"] for e in candidates]
        blended = rerank(query, docs, vscores, reranking_config)
        if blended is not vscores:
            for entry, score in zip(candidates, blended):
                entry["relevance"] = score

    # Suppress semantically redundant local records before the final cap. This
    # targets auto-extraction overlap (memory + observation + worklog) without
    # collapsing distinct vault references or records from different projects.
    enrichment_config = get_context_enrichment_config()
    semantic_duplicates_suppressed = 0
    candidates = list(best_per_file.values())
    if enrichment_config.get("semantic_dedup_enabled", True):
        candidates, semantic_duplicates_suppressed = _semantic_deduplicate_context(
            candidates,
            service,
            enrichment_config.get("semantic_dedup_threshold", 0.86),
        )

    # Sort by relevance descending
    deduped = sorted(
        candidates, key=lambda e: e["relevance"], reverse=True
    )[:max_results]

    # Budget-based selection: 3-way split (local / obsidian / remote).
    # Each bucket gets a guaranteed share; unused budget overflows to others.
    VAULT_REF_COST = 120  # estimated chars for "See: path + heading"

    # Check if any remote schemas are in play
    has_remote = any(e.get("_schema", "").startswith("remote_") for e in deduped)
    if has_remote:
        third = budget // 3
        local_remaining = third
        vault_remaining = third
        remote_remaining = budget - 2 * third  # absorb rounding remainder
    else:
        half = budget // 2
        local_remaining = half
        vault_remaining = half
        remote_remaining = 0

    selected = []

    def _try_spend(cost: int, primary: str) -> bool:
        """Try to spend from primary bucket, overflow to others, or split across buckets.

        Phase 1: Try each bucket individually (prefer primary, then others).
        Phase 2: If no single bucket suffices, split the cost across all buckets
        that have remaining capacity (prevents large entries from being silently
        dropped when total remaining budget is sufficient but fragmented).
        """
        nonlocal local_remaining, vault_remaining, remote_remaining
        buckets = {"local": local_remaining, "vault": vault_remaining, "remote": remote_remaining}
        order = [primary] + [b for b in ("local", "vault", "remote") if b != primary]

        # Phase 1: single-bucket fit
        for bucket in order:
            if buckets[bucket] >= cost:
                if bucket == "local":
                    local_remaining -= cost
                elif bucket == "vault":
                    vault_remaining -= cost
                else:
                    remote_remaining -= cost
                return True

        # Phase 2: split across buckets when total remaining >= cost
        total_available = local_remaining + vault_remaining + remote_remaining
        if total_available >= cost:
            remaining_cost = cost
            for bucket in order:
                take = min(remaining_cost, buckets[bucket])
                if take > 0:
                    if bucket == "local":
                        local_remaining -= take
                    elif bucket == "vault":
                        vault_remaining -= take
                    else:
                        remote_remaining -= take
                    remaining_cost -= take
                if remaining_cost <= 0:
                    break
            return True

        return False

    for entry in deduped:
        schema = entry.get("_schema", "obsidian")
        is_vault = schema == "obsidian"
        is_remote = schema.startswith("remote_")

        if is_vault:
            cost = VAULT_REF_COST
            if _try_spend(cost, "vault"):
                entry["display_mode"] = "reference"
                selected.append(entry)
        else:
            content_len = len(entry["document"] or "")
            cost = max(content_len, 50)  # minimum 50 chars cost
            bucket = "remote" if is_remote else "local"
            if _try_spend(cost, bucket):
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
            "id": entry["parent_file"],
            "relevance": round(entry["relevance"], 3),
            "similarity": round(entry.get("similarity", 0.0), 3),
            "type": doc_type,
            "content": content,
            "display_mode": entry["display_mode"],
            "schema": entry.get("_schema", "local"),
        }
        if chunk_heading:
            match["heading"] = chunk_heading
        if entry.get("is_stale"):
            match["stale"] = True

        # Attribution for remote memories: surface origin so the LLM
        # can distinguish "my memory" from "synced from another user".
        schema = entry.get("_schema", "")
        if schema.startswith("remote_"):
            match["source_remote"] = schema
            # Try to extract author hint from project_path metadata
            project_path = meta.get("project_path", "")
            if "/Users/" in project_path:
                # /Users/<username>/... → extract username
                parts = project_path.split("/Users/", 1)
                if len(parts) > 1:
                    author = parts[1].split("/", 1)[0]
                    if author:
                        match["origin_user"] = author

        matches.append(match)

    query_ms = round((time.time() - start) * 1000, 1)

    result = {
        "matches": matches,
        "query_ms": query_ms,
        "total_searched": total,
        "skipped_sensitive": skipped_sensitive,
        "semantic_duplicates_suppressed": semantic_duplicates_suppressed,
        "budget_used": {
            "local": (third if has_remote else half) - local_remaining,
            "vault": (third if has_remote else half) - vault_remaining,
            "remote": (budget - 2 * third if has_remote else 0) - remote_remaining if has_remote else 0,
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

    # Remote schema stats (Step 8: provenance + freshness)
    from .schema_registry import get_searchable_schemas as _gss_cs, SchemaKind as _SK_cs, is_valid_pg_identifier as _vpi_cs
    from .schema import get_meta as _get_meta_cs
    remote_entries = _gss_cs(kind=_SK_cs.REMOTE)
    if remote_entries:
        remote_stats = []
        for entry in remote_entries:
            if not _vpi_cs(entry.name) or not _vpi_cs(entry.table):
                continue
            try:
                count = _remote_count(entry)
                pull_meta = _get_meta_cs(f"pull_sync_ts:{entry.remote_name}")
                last_pull = pull_meta.get("timestamp") if pull_meta else None
                remote_stats.append({
                    "schema": entry.name,
                    "remote_name": entry.remote_name,
                    "count": count,
                    "last_pull_ts": last_pull,
                })
            except Exception as e:
                remote_stats.append({
                    "schema": entry.name,
                    "remote_name": entry.remote_name,
                    "error": str(e),
                })
        result["remote_schemas"] = remote_stats

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
