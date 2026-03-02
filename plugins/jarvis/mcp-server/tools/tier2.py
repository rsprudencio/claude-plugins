"""Tier 2 content CRUD operations backed by PostgreSQL + pgvector.

Tier 2 stores auto-generated, ephemeral content in the unified jarvis table
without file backing. Content types: observation, pattern, summary,
relationship, hint, plan, learning, decision, worklog.

Tier 2 content can be promoted to Tier 1 (file-backed) via the promotion module.
"""

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from .schema import execute_query, execute_write, metadata_to_jsonb
from .namespaces import (
    ContentType,
    observation_id,
    pattern_id,
    summary_id,
    code_id,
    relationship_id,
    hint_id,
    plan_id,
    learning_id,
    decision_id,
    worklog_id,
    NAMESPACE_OBS,
    NAMESPACE_PATTERN,
    NAMESPACE_SUMMARY,
    NAMESPACE_CODE,
    NAMESPACE_REL,
    NAMESPACE_HINT,
    NAMESPACE_PLAN,
    NAMESPACE_LEARNING,
    NAMESPACE_DECISION,
    NAMESPACE_WORKLOG,
)
from .config import get_memory_config
from .secret_scan import scan_for_secrets

logger = logging.getLogger("jarvis-core")

VALID_CONTENT_TYPES = (
    "observation",
    "pattern",
    "summary",
    "code",
    "relationship",
    "hint",
    "plan",
    "learning",
    "decision",
    "worklog",
)

# Map content_type string to (ContentType enum, NAMESPACE constant, ID generator)
_TYPE_MAP = {
    "observation": (ContentType.OBSERVATION, NAMESPACE_OBS, observation_id),
    "pattern": (ContentType.PATTERN, NAMESPACE_PATTERN, pattern_id),
    "summary": (ContentType.SUMMARY, NAMESPACE_SUMMARY, summary_id),
    "code": (ContentType.CODE, NAMESPACE_CODE, code_id),
    "relationship": (ContentType.RELATIONSHIP, NAMESPACE_REL, relationship_id),
    "hint": (ContentType.HINT, NAMESPACE_HINT, hint_id),
    "plan": (ContentType.PLAN, NAMESPACE_PLAN, plan_id),
    "learning": (ContentType.LEARNING, NAMESPACE_LEARNING, learning_id),
    "decision": (ContentType.DECISION, NAMESPACE_DECISION, decision_id),
    "worklog": (ContentType.WORKLOG, NAMESPACE_WORKLOG, worklog_id),
}


def tier2_write(
    content: str,
    content_type: str,
    name: Optional[str] = None,
    importance_score: float = 0.5,
    source: str = "auto-extract",
    tags: Optional[list] = None,
    session_id: Optional[str] = None,
    extra_metadata: Optional[dict] = None,
    skip_secret_scan: bool = False,
) -> dict:
    """Write Tier 2 content to PostgreSQL with embedding.

    Args:
        content: Document content (markdown)
        content_type: Type of content (observation, pattern, summary, etc.)
        name: Required for pattern/plan/decision, optional for others
        importance_score: Importance score 0.0-1.0 (default 0.5)
        source: Source of content (default "auto-extract")
        tags: Optional list of tags for categorization
        session_id: Optional session identifier
        extra_metadata: Optional dict of additional metadata key-value pairs
        skip_secret_scan: Skip secret detection (default False)

    Returns:
        Result dict with success, id, content_type, importance_score
    """
    # Validate content type
    if content_type not in VALID_CONTENT_TYPES:
        return {
            "success": False,
            "error": f"Invalid content_type '{content_type}'. "
            f"Valid types: {', '.join(VALID_CONTENT_TYPES)}",
        }

    # Validate name requirement
    if content_type in ("pattern", "plan", "decision") and not name:
        return {
            "success": False,
            "error": f"content_type '{content_type}' requires a name parameter",
        }

    # Validate importance score
    if not 0.0 <= importance_score <= 1.0:
        return {
            "success": False,
            "error": f"importance_score must be between 0.0 and 1.0, got {importance_score}",
        }

    # Secret scan (respects both per-call skip and global config toggle)
    if not skip_secret_scan and get_memory_config().get("secret_detection", True):
        detections = scan_for_secrets(content)
        if detections:
            return {
                "success": False,
                "error": "Secret detected in content",
                "detections": detections,
            }

    ingest_event_id = None
    if extra_metadata and isinstance(extra_metadata, dict):
        raw_event_id = str(extra_metadata.get("ingest_event_id", "")).strip()
        if raw_event_id:
            ingest_event_id = raw_event_id

    # Generate ID
    type_const, namespace, id_gen = _TYPE_MAP[content_type]

    if content_type == "observation":
        doc_id = id_gen()  # Auto-generates timestamp
    elif content_type == "pattern":
        doc_id = id_gen(name)
    elif content_type == "summary":
        doc_id = id_gen(session_id)  # Uses session_id if provided
    elif content_type == "code":
        # For code, name should be "file_path::symbol"
        if name and "::" in name:
            file_path, symbol = name.split("::", 1)
            doc_id = id_gen(file_path, symbol)
        else:
            doc_id = id_gen(name or "unknown", "__module__")
    elif content_type == "relationship":
        # For relationship, name should be "entity_a::entity_b"
        if name and "::" in name:
            entity_a, entity_b = name.split("::", 1)
            doc_id = id_gen(entity_a, entity_b)
        else:
            return {
                "success": False,
                "error": "relationship type requires name in format 'entity_a::entity_b'",
            }
    elif content_type == "hint":
        # For hint, name should be "topic::seq"
        if name and "::" in name:
            topic, seq_str = name.split("::", 1)
            doc_id = id_gen(topic, int(seq_str))
        else:
            doc_id = id_gen(name or "general", 0)
    elif content_type == "plan":
        doc_id = id_gen(name)
    elif content_type == "learning":
        doc_id = id_gen()  # Auto-generates timestamp
    elif content_type == "decision":
        doc_id = id_gen(name)
    elif content_type == "worklog":
        doc_id = id_gen()  # Auto-generates timestamp
    else:
        return {"success": False, "error": f"Unknown content_type: {content_type}"}

    # Build metadata
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    metadata = {
        "type": type_const,
        "namespace": namespace,
        "tier": "chromadb",
        "promoted": "false",
        "retrieval_count": "0",
        "importance_score": str(importance_score),
        "source": source,
        "created_at": now_iso,
        "updated_at": now_iso,
    }

    if tags:
        metadata["tags"] = ",".join(tags)
    if session_id:
        metadata["session_id"] = session_id
    if name:
        metadata["name"] = name
    if extra_metadata:
        metadata.update(extra_metadata)

    # Multi-user attribution
    from jarvis_common.auth import get_current_user

    user = get_current_user()
    if user != "anonymous":
        metadata["user"] = user

    # Write to PostgreSQL
    try:
        from .embedding import get_embedding_service
        from .schema import _get_pool

        # Idempotency for retry/replay pipelines. If the same ingest_event_id
        # was already written, return existing ID without creating duplicates.
        if ingest_event_id:
            existing = execute_query(
                "SELECT id FROM jarvis WHERE metadata->>'ingest_event_id' = %s LIMIT 1",
                (ingest_event_id,),
                fetch="one",
            )
            if existing:
                return {
                    "success": True,
                    "id": existing["id"],
                    "content_type": content_type,
                    "importance_score": importance_score,
                    "deduplicated": True,
                }

        # Generate embedding
        service = get_embedding_service()
        embedding = service.encode(content)

        # Upsert into jarvis table
        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO jarvis (id, document, embedding, metadata, created_at, updated_at)
                       VALUES (%s, %s, %s::halfvec, %s::jsonb,
                               COALESCE(%s::timestamptz, now()),
                               COALESCE(%s::timestamptz, now()))
                       ON CONFLICT (id) DO UPDATE SET
                           document = EXCLUDED.document,
                           embedding = EXCLUDED.embedding,
                           metadata = EXCLUDED.metadata,
                           updated_at = EXCLUDED.updated_at""",
                    (
                        doc_id,
                        content,
                        embedding,
                        metadata_to_jsonb(metadata),
                        metadata.get("created_at"),
                        metadata.get("updated_at"),
                    ),
                )
                conn.commit()

        result = {
            "success": True,
            "id": doc_id,
            "content_type": content_type,
            "importance_score": importance_score,
        }

        # Post-write conflict detection (all Tier 2 types)
        try:
            from .conflict import detect_conflicts

            superseded = detect_conflicts(doc_id, content)
            if superseded:
                result["conflicts_resolved"] = len(superseded)
                result["superseded_ids"] = superseded
        except Exception as e:
            logger.debug(f"Conflict detection skipped: {e}")

        return result
    except Exception as e:
        logger.error(f"tier2_write failed: {e}")
        return {"success": False, "error": str(e)}


def tier2_read(doc_id: str) -> dict:
    """Read Tier 2 content from PostgreSQL and increment retrieval count.

    Args:
        doc_id: Document ID to read

    Returns:
        Result dict with success, found, id, content, metadata
    """
    try:
        from .schema import _get_pool, jsonb_to_metadata

        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                # Atomic read + increment retrieval count
                cur.execute(
                    """UPDATE jarvis
                       SET metadata = jsonb_set(
                           jsonb_set(metadata, '{retrieval_count}',
                               to_jsonb((COALESCE((metadata->>'retrieval_count')::float, 0) + 1)::text)),
                           '{updated_at}', to_jsonb(%s::text)),
                           updated_at = now()
                       WHERE id = %s
                       RETURNING id, document, metadata""",
                    (
                        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                        doc_id,
                    ),
                )
                row = cur.fetchone()
                conn.commit()

        if not row:
            return {
                "success": True,
                "found": False,
                "id": doc_id,
            }

        metadata = jsonb_to_metadata(row[2])
        return {
            "success": True,
            "found": True,
            "id": row[0],
            "content": row[1],
            "metadata": metadata,
        }
    except Exception as e:
        logger.error(f"tier2_read failed: {e}")
        return {"success": False, "error": str(e)}


VALID_SORT_OPTIONS = (
    "importance_desc",
    "importance_asc",
    "created_at_desc",
    "created_at_asc",
    "none",
)

# Map sort_by values to SQL ORDER BY clauses
_SORT_SQL = {
    "importance_desc": "ORDER BY (metadata->>'importance_score')::float DESC NULLS LAST",
    "importance_asc": "ORDER BY (metadata->>'importance_score')::float ASC NULLS LAST",
    "created_at_desc": "ORDER BY created_at DESC",
    "created_at_asc": "ORDER BY created_at ASC",
    "none": "",
}


def tier2_list(
    content_type: Optional[str] = None,
    min_importance: Optional[float] = None,
    source: Optional[str] = None,
    limit: int = 20,
    sort_by: str = "importance_desc",
    session_id: Optional[str] = None,
    include_content: bool = True,
) -> dict:
    """List Tier 2 documents with optional filtering and sorting.

    Args:
        content_type: Filter by content type (observation, pattern, etc.)
        min_importance: Minimum importance score (0.0-1.0)
        source: Filter by source (e.g., "auto-extract")
        limit: Maximum number of results (default 20)
        sort_by: Sort order. One of: importance_desc (default),
                 importance_asc, created_at_desc, created_at_asc, none
        session_id: Filter by session_id (useful for dedup within a session)
        include_content: Include document text in results (default True)

    Returns:
        Result dict with success, documents, total
    """
    try:
        # Validate sort_by
        if sort_by not in VALID_SORT_OPTIONS:
            return {
                "success": False,
                "error": f"Invalid sort_by '{sort_by}'. "
                f"Valid options: {', '.join(VALID_SORT_OPTIONS)}",
            }

        # Build SQL WHERE clause
        conditions = ["metadata->>'tier' = 'chromadb'"]
        params = []

        if content_type:
            if content_type not in VALID_CONTENT_TYPES:
                return {
                    "success": False,
                    "error": f"Invalid content_type '{content_type}'. "
                    f"Valid types: {', '.join(VALID_CONTENT_TYPES)}",
                }
            type_const, _, _ = _TYPE_MAP[content_type]
            conditions.append("metadata->>'type' = %s")
            params.append(type_const)

        if source:
            conditions.append("metadata->>'source' = %s")
            params.append(source)

        if session_id:
            conditions.append("metadata->>'session_id' = %s")
            params.append(session_id)

        if min_importance is not None:
            conditions.append("(metadata->>'importance_score')::float >= %s")
            params.append(min_importance)

        where_clause = " AND ".join(conditions)
        order_clause = _SORT_SQL.get(sort_by, "")

        # First get total count (without limit)
        count_sql = f"SELECT count(*) AS cnt FROM jarvis WHERE {where_clause}"
        count_result = execute_query(count_sql, tuple(params), fetch="one")
        total = count_result["cnt"] if count_result else 0

        # Then fetch the actual results with limit
        select_cols = "id, metadata" + (", document" if include_content else "")
        fetch_sql = f"SELECT {select_cols} FROM jarvis WHERE {where_clause} {order_clause} LIMIT %s"
        rows = execute_query(fetch_sql, tuple(params) + (limit,))

        docs = []
        for row in rows:
            from .schema import jsonb_to_metadata

            entry = {
                "id": row["id"],
                "metadata": jsonb_to_metadata(row["metadata"]),
            }
            if include_content:
                entry["content"] = row["document"]
            docs.append(entry)

        return {
            "success": True,
            "documents": docs,
            "total": total,
            "returned": len(docs),
        }
    except Exception as e:
        logger.error(f"tier2_list failed: {e}")
        return {"success": False, "error": str(e)}


def tier2_delete(doc_id: str) -> dict:
    """Delete Tier 2 content from PostgreSQL.

    Args:
        doc_id: Document ID to delete

    Returns:
        Result dict with success, id, deleted
    """
    try:
        from .schema import _get_pool

        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM jarvis WHERE id = %s", (doc_id,))
                deleted = cur.rowcount > 0
                conn.commit()

        if not deleted:
            return {
                "success": True,
                "id": doc_id,
                "deleted": False,
                "reason": "not found",
            }

        return {
            "success": True,
            "id": doc_id,
            "deleted": True,
        }
    except Exception as e:
        logger.error(f"tier2_delete failed: {e}")
        return {"success": False, "error": str(e)}


def tier2_upsert(doc_id: str, content: str, metadata: dict) -> dict:
    """Update existing tier2 document by ID with re-embedding.

    Unlike tier2_write which generates new IDs, this updates in-place.

    Args:
        doc_id: Existing document ID
        content: Updated content
        metadata: Updated metadata dict

    Returns:
        Result dict with success, doc_id, updated
    """
    try:
        from .embedding import get_embedding_service
        from .schema import _get_pool

        metadata["updated_at"] = datetime.now(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )

        # Re-embed the updated content
        service = get_embedding_service()
        embedding = service.encode(content)

        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO jarvis (id, document, embedding, metadata, created_at, updated_at)
                       VALUES (%s, %s, %s::halfvec, %s::jsonb, now(), now())
                       ON CONFLICT (id) DO UPDATE SET
                           document = EXCLUDED.document,
                           embedding = EXCLUDED.embedding,
                           metadata = EXCLUDED.metadata,
                           updated_at = EXCLUDED.updated_at""",
                    (
                        doc_id,
                        content,
                        embedding,
                        metadata_to_jsonb(metadata),
                    ),
                )
                conn.commit()

        return {"success": True, "doc_id": doc_id, "updated": True}
    except Exception as e:
        logger.error(f"tier2_upsert failed: {e}")
        return {"success": False, "error": str(e)}
