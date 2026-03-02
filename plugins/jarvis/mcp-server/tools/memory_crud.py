"""Memory CRUD tool handlers.

Orchestrates file I/O (tools.memory_files) + pgvector indexing (tools.schema)
for Tier 1 file-backed memories. Each handler is called from server.py.

Storage locations:
- Global:  <vault>/.jarvis/strategic/<name>.md
- Project: <vault>/.jarvis/memories/<project>/<name>.md
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from .schema import execute_query, execute_write, metadata_to_jsonb, jsonb_to_metadata
from .memory_files import (
    resolve_memory_path,
    write_memory_file,
    read_memory_file,
    list_memory_files,
    delete_memory_file,
    validate_name,
)
from .namespaces import (
    ContentType,
    global_memory_id,
    project_memory_id,
    memory_namespace,
    parse_id,
)
from .config import get_memory_config
from .secret_scan import scan_for_secrets

logger = logging.getLogger("jarvis-core")

CATEGORICAL_TO_NUMERIC = {
    "critical": 0.95,
    "high": 0.8,
    "medium": 0.5,
    "low": 0.3,
}

VALID_SCOPES = ("global", "project")


def _build_doc_id(name: str, scope: str, project: Optional[str] = None) -> str:
    """Build the document ID for a memory."""
    if scope == "project" and project:
        return project_memory_id(project, name)
    return global_memory_id(name)


def _normalize_importance(value) -> float:
    """Normalize importance to a float 0.0-1.0.

    Accepts:
      - float/int: passed through (clamped to 0.0-1.0)
      - str numeric: "0.8" -> 0.8
      - str categorical: "high" -> 0.8  (backward compat)

    Returns float or raises ValueError.
    """
    if isinstance(value, (int, float)):
        return max(0.0, min(1.0, float(value)))
    if isinstance(value, str):
        # Try categorical mapping first
        if value.lower() in CATEGORICAL_TO_NUMERIC:
            return CATEGORICAL_TO_NUMERIC[value.lower()]
        # Try numeric string
        try:
            return max(0.0, min(1.0, float(value)))
        except ValueError:
            pass
    raise ValueError(
        f"Invalid importance: '{value}'. "
        f"Use 0.0-1.0 or: {', '.join(CATEGORICAL_TO_NUMERIC.keys())}"
    )


def _build_memory_metadata(
    name: str,
    scope: str,
    importance: float,
    tags: list,
    project: Optional[str] = None,
    created: Optional[str] = None,
    modified: Optional[str] = None,
) -> dict:
    """Build metadata for a memory document."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    namespace = memory_namespace(project)

    meta = {
        "type": ContentType.MEMORY,
        "namespace": namespace,
        "tier": "file",
        "scope": scope,
        "name": name,
        "importance": str(importance),
        "importance_score": str(importance),
        "source": "memory-write",
        "created_at": created or now_iso,
        "updated_at": modified or now_iso,
    }
    if tags:
        meta["tags"] = ",".join(tags)
    if scope == "project" and project:
        meta["project"] = project

    return meta


def memory_write(
    name: str,
    content: str,
    scope: str = "global",
    project: Optional[str] = None,
    tags: Optional[list] = None,
    importance: float = 0.5,
    overwrite: bool = False,
    skip_secret_scan: bool = False,
) -> dict:
    """Write a memory file and index in PostgreSQL.

    Args:
        name: Memory name slug (lowercase, hyphens)
        content: Markdown content (body only, frontmatter is auto-generated)
        scope: "global" or "project"
        project: Required when scope="project"
        tags: Optional list of tags
        importance: Numeric 0.0-1.0 (also accepts categorical strings for backward compat)
        overwrite: Allow overwriting existing memory
        skip_secret_scan: Bypass secret detection (use with caution)

    Returns:
        Result dict with success status, path, indexing info
    """
    tags = tags or []

    # Validate name
    name_error = validate_name(name)
    if name_error:
        return {"success": False, "error": name_error}

    # Validate scope
    if scope not in VALID_SCOPES:
        return {
            "success": False,
            "error": f"Invalid scope: '{scope}'. Use: {VALID_SCOPES}",
        }

    # Normalize importance (accepts float, int, categorical string)
    try:
        importance = _normalize_importance(importance)
    except ValueError as e:
        return {"success": False, "error": str(e)}

    # Validate project requirement
    if scope == "project" and not project:
        return {"success": False, "error": "Project name required for scope='project'"}

    # Secret scan (respects both per-call skip and global config toggle)
    secret_scan_result = "skipped"
    if not skip_secret_scan and get_memory_config().get("secret_detection", True):
        detections = scan_for_secrets(content)
        if detections:
            return {
                "success": False,
                "error": "SECRET_DETECTED",
                "message": "Content contains potential secrets. Fix the content or use skip_secret_scan=true.",
                "detections": detections,
            }
        secret_scan_result = "clean"

    # Resolve file path
    path, error = resolve_memory_path(name, scope, project)
    if error:
        return {"success": False, "error": error}

    # Write file
    write_result = write_memory_file(
        path=path,
        name=name,
        content=content,
        scope=scope,
        project=project,
        importance=importance,
        tags=tags,
        overwrite=overwrite,
    )
    if not write_result.get("success"):
        return write_result

    # Index in PostgreSQL
    indexed = False
    doc_id = _build_doc_id(name, scope, project)
    try:
        from .embedding import get_embedding_service
        from .schema import _get_pool

        metadata = _build_memory_metadata(
            name=name,
            scope=scope,
            importance=importance,
            tags=tags,
            project=project,
        )
        # Store full content (with frontmatter) for search
        file_result = read_memory_file(path)
        full_content = (
            file_result.get("content", content)
            if file_result.get("success")
            else content
        )

        # Generate embedding
        service = get_embedding_service()
        embedding = service.encode(full_content)

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
                        full_content,
                        embedding,
                        metadata_to_jsonb(metadata),
                        metadata.get("created_at"),
                        metadata.get("updated_at"),
                    ),
                )
                conn.commit()
        indexed = True
    except Exception as e:
        logger.warning(f"PostgreSQL indexing failed for memory '{name}': {e}")

    return {
        "success": True,
        "name": name,
        "scope": scope,
        "id": doc_id,
        "path": path,
        "version": write_result.get("version", 1),
        "secret_scan": secret_scan_result,
        "indexed": indexed,
    }


def memory_read(
    name: str, scope: str = "global", project: Optional[str] = None
) -> dict:
    """Read a memory by name.

    Tries PostgreSQL first (fast path), falls back to file read.

    Args:
        name: Memory name slug
        scope: "global" or "project"
        project: Required when scope="project"

    Returns:
        Result dict with content, metadata, source info
    """
    # Validate name
    name_error = validate_name(name)
    if name_error:
        return {"success": False, "error": name_error}

    doc_id = _build_doc_id(name, scope, project)

    # Try PostgreSQL first (fast path)
    try:
        row = execute_query(
            "SELECT id, document, metadata FROM jarvis WHERE id = %s",
            (doc_id,),
            fetch="one",
        )
        if row:
            return {
                "success": True,
                "found": True,
                "id": doc_id,
                "name": name,
                "scope": scope,
                "content": row["document"],
                "metadata": jsonb_to_metadata(row["metadata"]),
                "source": "database",
            }
    except Exception as e:
        logger.debug(f"PostgreSQL read failed for '{name}': {e}")

    # Fall back to file read
    path, error = resolve_memory_path(name, scope, project)
    if error:
        return {"success": False, "error": error}

    file_result = read_memory_file(path)
    if file_result.get("success"):
        return {
            "success": True,
            "found": True,
            "id": doc_id,
            "name": name,
            "scope": scope,
            "content": file_result["content"],
            "body": file_result["body"],
            "metadata": file_result["metadata"],
            "source": "file",
            "index_stale": True,
        }

    # Neither found — return available memories
    available = list_memory_files(scope=scope, project=project)
    available_names = [m["name"] for m in available]

    return {
        "success": True,
        "found": False,
        "name": name,
        "scope": scope,
        "message": f"Memory '{name}' not found.",
        "available": available_names,
    }


def memory_list(
    scope: str = "all",
    project: Optional[str] = None,
    tag: Optional[str] = None,
    importance: Optional[float] = None,
    include_content: bool = False,
) -> dict:
    """List memory files with optional filters.

    Args:
        scope: "global", "project", or "all"
        project: Filter by project (for scope="project")
        tag: Filter by tag
        importance: Minimum importance threshold (0.0-1.0). Also accepts
                    categorical strings for backward compat.
        include_content: Include body text in each memory entry

    Returns:
        Result dict with memories list and total count
    """
    # Normalize importance filter (accepts float or categorical string)
    if importance is not None:
        try:
            importance = _normalize_importance(importance)
        except ValueError:
            importance = None
    memories = list_memory_files(
        scope=scope,
        project=project,
        tag=tag,
        importance=importance,
        include_content=include_content,
    )

    # Cross-reference with PostgreSQL to detect stale indexes
    try:
        for mem in memories:
            doc_id = _build_doc_id(
                mem["name"],
                mem["scope"],
                mem.get("project"),
            )
            mem["id"] = doc_id
            try:
                row = execute_query(
                    "SELECT id FROM jarvis WHERE id = %s",
                    (doc_id,),
                    fetch="one",
                )
                mem["indexed"] = row is not None
            except Exception:
                mem["indexed"] = False
            # Remove full path from output (internal detail)
            mem.pop("path", None)
    except Exception:
        # Database unavailable — mark all as unknown
        for mem in memories:
            mem["indexed"] = None
            mem.pop("path", None)

    return {
        "success": True,
        "memories": memories,
        "total": len(memories),
    }


def memory_delete(
    name: str,
    scope: str = "global",
    project: Optional[str] = None,
    confirm: bool = False,
) -> dict:
    """Delete a memory file and its database entry.

    Args:
        name: Memory name slug
        scope: "global" or "project"
        project: Required when scope="project"
        confirm: Must be True for global memories (safety gate)

    Returns:
        Result dict with deletion status
    """
    # Validate name
    name_error = validate_name(name)
    if name_error:
        return {"success": False, "error": name_error}

    # Safety gate for global memories
    if scope == "global" and not confirm:
        # Preview what would be deleted
        path, error = resolve_memory_path(name, scope, project)
        if error:
            return {"success": False, "error": error}

        file_result = read_memory_file(path)
        preview = ""
        if file_result.get("success"):
            body = file_result.get("body", "")
            preview = body[:200] + ("..." if len(body) > 200 else "")

        return {
            "success": True,
            "confirmation_required": True,
            "name": name,
            "scope": scope,
            "preview": preview,
            "message": f"Delete global memory '{name}'? Pass confirm=true to proceed.",
        }

    # Resolve file path
    path, error = resolve_memory_path(name, scope, project)
    if error:
        return {"success": False, "error": error}

    # Delete file
    file_result = delete_memory_file(path)
    file_deleted = file_result.get("success", False)

    # Delete database entry
    index_deleted = False
    doc_id = _build_doc_id(name, scope, project)
    try:
        from .schema import _get_pool

        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM jarvis WHERE id = %s", (doc_id,))
                index_deleted = cur.rowcount > 0
                conn.commit()
    except Exception as e:
        logger.warning(f"PostgreSQL delete failed for '{name}': {e}")

    return {
        "success": True,
        "name": name,
        "scope": scope,
        "file_deleted": file_deleted,
        "index_deleted": index_deleted,
    }
