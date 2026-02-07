"""Memory CRUD tool handlers.

Orchestrates file I/O (tools.memory_files) + ChromaDB indexing (tools.memory)
for Tier 1 file-backed memories. Each handler is called from server.py.

Storage locations:
- Global:  <vault>/.jarvis/strategic/<name>.md
- Project: <vault>/.jarvis/memories/<project>/<name>.md
"""
import logging
from datetime import datetime, timezone
from typing import Optional

from .memory import _get_collection
from .memory_files import (
    resolve_memory_path, write_memory_file, read_memory_file,
    list_memory_files, delete_memory_file, validate_name,
)
from .namespaces import (
    global_memory_id, project_memory_id, memory_namespace,
    TYPE_MEMORY, parse_id,
)
from .secret_scan import scan_for_secrets

logger = logging.getLogger("jarvis-tools")

VALID_IMPORTANCE = ("low", "medium", "high", "critical")
VALID_SCOPES = ("global", "project")


def _build_chromadb_id(name: str, scope: str,
                       project: Optional[str] = None) -> str:
    """Build the ChromaDB document ID for a memory."""
    if scope == "project" and project:
        return project_memory_id(project, name)
    return global_memory_id(name)


def _build_memory_metadata(name: str, scope: str, importance: str,
                           tags: list, project: Optional[str] = None,
                           created: Optional[str] = None,
                           modified: Optional[str] = None) -> dict:
    """Build ChromaDB metadata for a memory document."""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    namespace = memory_namespace(project)

    meta = {
        "type": TYPE_MEMORY,
        "namespace": namespace,
        "scope": scope,
        "name": name,
        "importance": importance,
        "source": "memory-write",
        "created_at": created or now_iso,
        "updated_at": modified or now_iso,
    }
    if tags:
        meta["tags"] = ",".join(tags)
    if scope == "project" and project:
        meta["project"] = project

    return meta


def memory_write(name: str, content: str, scope: str = "global",
                 project: Optional[str] = None, tags: Optional[list] = None,
                 importance: str = "medium", overwrite: bool = False,
                 skip_secret_scan: bool = False) -> dict:
    """Write a memory file and index in ChromaDB.

    Args:
        name: Memory name slug (lowercase, hyphens)
        content: Markdown content (body only, frontmatter is auto-generated)
        scope: "global" or "project"
        project: Required when scope="project"
        tags: Optional list of tags
        importance: "low", "medium", "high", "critical"
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
        return {"success": False, "error": f"Invalid scope: '{scope}'. Use: {VALID_SCOPES}"}

    # Validate importance
    if importance not in VALID_IMPORTANCE:
        return {"success": False, "error": f"Invalid importance: '{importance}'. Use: {VALID_IMPORTANCE}"}

    # Validate project requirement
    if scope == "project" and not project:
        return {"success": False, "error": "Project name required for scope='project'"}

    # Secret scan
    secret_scan_result = "skipped"
    if not skip_secret_scan:
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
        path=path, name=name, content=content, scope=scope,
        project=project, importance=importance, tags=tags,
        overwrite=overwrite,
    )
    if not write_result.get("success"):
        return write_result

    # Index in ChromaDB
    indexed = False
    doc_id = _build_chromadb_id(name, scope, project)
    try:
        collection = _get_collection()
        metadata = _build_memory_metadata(
            name=name, scope=scope, importance=importance,
            tags=tags, project=project,
        )
        # Store full content (with frontmatter) for search
        file_result = read_memory_file(path)
        full_content = file_result.get("content", content) if file_result.get("success") else content

        collection.upsert(
            ids=[doc_id],
            documents=[full_content],
            metadatas=[metadata],
        )
        indexed = True
    except Exception as e:
        logger.warning(f"ChromaDB indexing failed for memory '{name}': {e}")

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


def memory_read(name: str, scope: str = "global",
                project: Optional[str] = None) -> dict:
    """Read a memory by name.

    Tries ChromaDB first (fast path), falls back to file read.

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

    doc_id = _build_chromadb_id(name, scope, project)

    # Try ChromaDB first (fast path)
    try:
        collection = _get_collection()
        result = collection.get(
            ids=[doc_id],
            include=["documents", "metadatas"],
        )
        if result["ids"]:
            return {
                "success": True,
                "found": True,
                "name": name,
                "scope": scope,
                "content": result["documents"][0],
                "metadata": result["metadatas"][0],
                "source": "chromadb",
            }
    except Exception as e:
        logger.debug(f"ChromaDB read failed for '{name}': {e}")

    # Fall back to file read
    path, error = resolve_memory_path(name, scope, project)
    if error:
        return {"success": False, "error": error}

    file_result = read_memory_file(path)
    if file_result.get("success"):
        return {
            "success": True,
            "found": True,
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


def memory_list(scope: str = "all", project: Optional[str] = None,
                tag: Optional[str] = None,
                importance: Optional[str] = None) -> dict:
    """List memory files with optional filters.

    Args:
        scope: "global", "project", or "all"
        project: Filter by project (for scope="project")
        tag: Filter by tag
        importance: Filter by importance level

    Returns:
        Result dict with memories list and total count
    """
    memories = list_memory_files(
        scope=scope, project=project,
        tag=tag, importance=importance,
    )

    # Cross-reference with ChromaDB to detect stale indexes
    try:
        collection = _get_collection()
        for mem in memories:
            doc_id = _build_chromadb_id(
                mem["name"], mem["scope"], mem.get("project"),
            )
            try:
                result = collection.get(ids=[doc_id])
                mem["indexed"] = bool(result["ids"])
            except Exception:
                mem["indexed"] = False
            # Remove full path from output (internal detail)
            mem.pop("path", None)
    except Exception:
        # ChromaDB unavailable — mark all as unknown
        for mem in memories:
            mem["indexed"] = None
            mem.pop("path", None)

    return {
        "success": True,
        "memories": memories,
        "total": len(memories),
    }


def memory_delete(name: str, scope: str = "global",
                  project: Optional[str] = None,
                  confirm: bool = False) -> dict:
    """Delete a memory file and its ChromaDB entry.

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

    # Delete ChromaDB entry
    index_deleted = False
    doc_id = _build_chromadb_id(name, scope, project)
    try:
        collection = _get_collection()
        collection.delete(ids=[doc_id])
        index_deleted = True
    except Exception as e:
        logger.warning(f"ChromaDB delete failed for '{name}': {e}")

    # ChromaDB delete is a no-op for missing IDs, so only check file_deleted
    # to determine if the memory actually existed

    return {
        "success": True,
        "name": name,
        "scope": scope,
        "file_deleted": file_deleted,
        "index_deleted": index_deleted,
    }
