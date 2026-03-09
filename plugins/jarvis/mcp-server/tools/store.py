"""Unified content store for Jarvis.

Routes writes based on parameters (priority: id -> relative_path -> type):
- id provided -> parse namespace prefix -> update existing content
  - vault::* -> extract path, vault file write + reindex
  - memory::* -> extract name, memory upsert
  - obs::/pattern::/etc -> content upsert by ID
- relative_path provided -> vault file create (new, no prior ID)
- type provided -> create new memory or content (auto-generate ID)

All .md file writes are auto-indexed for semantic search.
"""

import logging
from typing import Optional

from .namespaces import CONTENT_TYPES, parse_id, schema_for_id, SCHEMA_LOCAL
from .format_support import is_indexable
from .routing_utils import validate_exactly_one, parse_memory_scope

logger = logging.getLogger("jarvis-core")


def store(
    content: str = "",
    id: Optional[str] = None,
    relative_path: Optional[str] = None,
    type: Optional[str] = None,
    name: Optional[str] = None,
    mode: str = "write",
    old_string: str = "",
    new_string: str = "",
    separator: str = "\n",
    replace_all: bool = False,
    importance: Optional[float] = None,
    tags: Optional[list] = None,
    scope: str = "global",
    project: Optional[str] = None,
    source: Optional[str] = None,
    session_id: Optional[str] = None,
    extra_metadata: Optional[dict] = None,
    overwrite: bool = False,
    auto_index: bool = True,
    skip_secret_scan: bool = False,
) -> dict:
    """Unified write entry point.

    Routing priority: id -> relative_path -> type
    - id: Update existing content (from a previous jarvis_retrieve result)
    - relative_path: Create new vault file (no prior ID)
    - type: Create new memory or content
    """
    # Validate: exactly ONE routing param must be set
    error = validate_exactly_one(
        [id, relative_path, type],
        "Provide one of: id (update existing), "
        "relative_path (new vault file), type (new memory/content)",
        "Provide only ONE of: id, relative_path, type",
    )
    if error:
        return error

    # Route 1: ID-based update (from retrieve results)
    if id:
        return _store_by_id(
            doc_id=id,
            content=content,
            mode=mode,
            old_string=old_string,
            new_string=new_string,
            separator=separator,
            replace_all=replace_all,
            importance=importance,
            tags=tags,
            source=source,
            extra_metadata=extra_metadata,
            auto_index=auto_index,
            skip_secret_scan=skip_secret_scan,
            scope=scope,
            project=project,
        )

    # Route 2: New vault file (no prior ID)
    if relative_path:
        return _store_vault_file(
            relative_path=relative_path,
            content=content,
            mode=mode,
            old_string=old_string,
            new_string=new_string,
            separator=separator,
            replace_all=replace_all,
            auto_index=auto_index,
        )

    # Route 3: New memory
    if type == "memory":
        return _store_memory(
            name=name,
            content=content,
            scope=scope,
            project=project,
            tags=tags,
            importance=importance,
            overwrite=overwrite,
            skip_secret_scan=skip_secret_scan,
        )

    # Route 4: New content (auto-generate ID)
    if type in CONTENT_TYPES:
        # Forward scope/project into extra_metadata so content_write can extract them
        merged_metadata = dict(extra_metadata) if extra_metadata else {}
        if scope and scope != "global":
            merged_metadata.setdefault("scope", scope)
        if project:
            merged_metadata.setdefault("project", project)

        return _store_content(
            content=content,
            content_type=type,
            name=name,
            importance_score=importance,
            source=source,
            tags=tags,
            session_id=session_id,
            extra_metadata=merged_metadata or None,
            skip_secret_scan=skip_secret_scan,
        )

    return {
        "success": False,
        "error": f"Unknown type '{type}'. Valid: memory, {', '.join(CONTENT_TYPES)}",
    }


def _store_by_id(
    doc_id,
    content,
    mode,
    old_string,
    new_string,
    separator,
    replace_all,
    importance,
    tags,
    source,
    extra_metadata,
    auto_index,
    skip_secret_scan,
    scope=None,
    project=None,
):
    """Route updates by parsing the namespaced ID prefix."""
    parsed = parse_id(doc_id)

    # Vault document (vault::notes/bla.md or vault::notes/bla.md#chunk-0)
    if parsed.namespace == "vault":
        return _store_vault_file(
            relative_path=parsed.content_id,
            content=content,
            mode=mode,
            old_string=old_string,
            new_string=new_string,
            separator=separator,
            replace_all=replace_all,
            auto_index=auto_index,
        )

    # Memory document (memory::global::name or memory::project::name)
    if parsed.namespace == "memory":
        mem_scope, mem_project = parse_memory_scope(parsed)
        return _store_memory(
            name=parsed.content_id,
            content=content,
            scope=mem_scope,
            project=mem_project,
            tags=tags,
            importance=importance,
            overwrite=True,  # ID-based = update existing
            skip_secret_scan=skip_secret_scan,
        )

    # Local content (obs::, pattern::, etc.)
    schema = schema_for_id(doc_id)
    if schema == SCHEMA_LOCAL:
        return _update_content(
            doc_id=doc_id,
            content=content,
            importance=importance,
            tags=tags,
            source=source,
            extra_metadata=extra_metadata,
            scope=scope,
            project=project,
        )

    return {
        "success": False,
        "error": f"Cannot route ID '{doc_id}' — unknown namespace '{parsed.namespace}'",
    }


def _store_vault_file(
    relative_path,
    content,
    mode,
    old_string,
    new_string,
    separator,
    replace_all,
    auto_index,
):
    """Route to vault file operations with auto-reindex."""
    from .file_ops import write_vault_file, append_vault_file, edit_vault_file

    if mode == "write":
        result = write_vault_file(relative_path, content)
    elif mode == "append":
        result = append_vault_file(relative_path, content, separator)
    elif mode == "edit":
        result = edit_vault_file(relative_path, old_string, new_string, replace_all)
    else:
        return {
            "success": False,
            "error": f"Invalid mode: '{mode}'. Use: write, append, edit",
        }

    # Auto-index supported files (.md, .org) for semantic search
    if result.get("success") and auto_index and is_indexable(relative_path):
        try:
            from .memory import index_file

            index_result = index_file(relative_path)
            result["indexed"] = index_result.get("success", False)
        except Exception as e:
            logger.debug(f"Auto-index failed for {relative_path}: {e}")
            result["indexed"] = False

    return result


def _store_memory(
    name, content, scope, project, tags, importance, overwrite, skip_secret_scan
):
    """Route to memory_crud.memory_write (numeric importance passthrough)."""
    from .memory_crud import memory_write

    return memory_write(
        name=name or "",
        content=content,
        scope=scope,
        project=project,
        tags=tags,
        importance=importance if importance is not None else 0.5,
        overwrite=overwrite,
        skip_secret_scan=skip_secret_scan,
    )


def _store_content(
    content,
    content_type,
    name,
    importance_score,
    source,
    tags,
    session_id,
    extra_metadata,
    skip_secret_scan,
):
    """Route to content.content_write (creates new with auto-generated ID)."""
    from .content import content_write

    return content_write(
        content=content,
        content_type=content_type,
        name=name,
        importance_score=importance_score or 0.5,
        source=source or "manual",
        tags=tags,
        session_id=session_id,
        extra_metadata=extra_metadata,
        skip_secret_scan=skip_secret_scan,
    )


def _update_content(doc_id, content, importance, tags, source, extra_metadata,
                    scope=None, project=None):
    """Update existing content by ID.

    Reads existing doc, merges updates, upserts back.
    """
    from .content import content_read, content_upsert

    # Read existing to get current metadata
    existing = content_read(doc_id)
    if not existing.get("found", False):
        return {"success": False, "error": f"Document '{doc_id}' not found"}

    # Build updated metadata (merge, don't replace)
    metadata = existing.get("metadata", {})
    if importance is not None:
        metadata["importance_score"] = str(importance)
    if tags is not None:
        metadata["tags"] = ",".join(tags) if tags else ""
    if source is not None:
        metadata["source"] = source
    if scope is not None:
        metadata["scope"] = scope
    if project is not None:
        metadata["project"] = project
    if extra_metadata:
        metadata.update(extra_metadata)

    updated_content = content if content else existing.get("content", "")

    return content_upsert(
        doc_id=doc_id,
        content=updated_content,
        metadata=metadata,
    )
