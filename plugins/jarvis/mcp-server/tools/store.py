"""Unified content store for Jarvis.

Routes writes based on parameters (priority: id -> relative_path -> type):
- id provided -> parse namespace prefix -> update existing content
  - vault::* -> extract path, vault file write + reindex
  - memory::* -> extract name, memory upsert
  - obs::/pattern::/etc -> tier2 upsert by ID
- relative_path provided -> vault file create (new, no prior ID)
- type provided -> create new memory or tier2 content (auto-generate ID)

All .md file writes are auto-indexed to ChromaDB.
"""
import logging
from typing import Optional

from .namespaces import TIER2_TYPES, parse_id, get_tier, TIER_CHROMADB
from .format_support import is_indexable

logger = logging.getLogger("jarvis-tools")


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
    - type: Create new memory or tier2 content
    """
    # Validate: exactly ONE routing param must be set
    routing_params = sum(1 for p in [id, relative_path, type] if p)
    if routing_params == 0:
        return {
            "success": False,
            "error": "Provide one of: id (update existing), "
                     "relative_path (new vault file), type (new memory/tier2)",
        }
    if routing_params > 1:
        return {
            "success": False,
            "error": "Provide only ONE of: id, relative_path, type",
        }

    # Route 1: ID-based update (from retrieve results)
    if id:
        return _store_by_id(
            doc_id=id, content=content,
            mode=mode, old_string=old_string, new_string=new_string,
            separator=separator, replace_all=replace_all,
            importance=importance, tags=tags, source=source,
            extra_metadata=extra_metadata, auto_index=auto_index,
            skip_secret_scan=skip_secret_scan,
        )

    # Route 2: New vault file (no prior ID)
    if relative_path:
        return _store_vault_file(
            relative_path=relative_path, content=content,
            mode=mode, old_string=old_string, new_string=new_string,
            separator=separator, replace_all=replace_all,
            auto_index=auto_index,
        )

    # Route 3: New memory
    if type == "memory":
        return _store_memory(
            name=name, content=content, scope=scope,
            project=project, tags=tags,
            importance=importance, overwrite=overwrite,
            skip_secret_scan=skip_secret_scan,
        )

    # Route 4: New tier2 content (auto-generate ID)
    if type in TIER2_TYPES:
        return _store_tier2(
            content=content, content_type=type,
            name=name, importance_score=importance,
            source=source, tags=tags,
            session_id=session_id,
            extra_metadata=extra_metadata,
            skip_secret_scan=skip_secret_scan,
        )

    return {
        "success": False,
        "error": f"Unknown type '{type}'. Valid: memory, {', '.join(TIER2_TYPES)}",
    }


def _store_by_id(doc_id, content, mode, old_string, new_string,
                 separator, replace_all, importance, tags, source,
                 extra_metadata, auto_index, skip_secret_scan):
    """Route updates by parsing the namespaced ID prefix."""
    parsed = parse_id(doc_id)

    # Vault document (vault::notes/bla.md or vault::notes/bla.md#chunk-0)
    if parsed.namespace == "vault":
        return _store_vault_file(
            relative_path=parsed.content_id, content=content,
            mode=mode, old_string=old_string, new_string=new_string,
            separator=separator, replace_all=replace_all,
            auto_index=auto_index,
        )

    # Memory document (memory::global::name or memory::project::name)
    if parsed.namespace == "memory":
        scope = "global" if "global" in parsed.full_prefix else "project"
        return _store_memory(
            name=parsed.content_id, content=content,
            scope=scope, project=None,
            tags=tags, importance=importance,
            overwrite=True,  # ID-based = update existing
            skip_secret_scan=skip_secret_scan,
        )

    # Tier 2 content (obs::, pattern::, etc.)
    tier = get_tier(doc_id)
    if tier == TIER_CHROMADB:
        return _update_tier2(
            doc_id=doc_id, content=content,
            importance=importance, tags=tags,
            source=source, extra_metadata=extra_metadata,
        )

    return {
        "success": False,
        "error": f"Cannot route ID '{doc_id}' — unknown namespace '{parsed.namespace}'",
    }


def _store_vault_file(relative_path, content, mode, old_string, new_string,
                      separator, replace_all, auto_index):
    """Route to vault file operations with auto-reindex."""
    from .file_ops import write_vault_file, append_vault_file, edit_vault_file

    if mode == "write":
        result = write_vault_file(relative_path, content)
    elif mode == "append":
        result = append_vault_file(relative_path, content, separator)
    elif mode == "edit":
        result = edit_vault_file(relative_path, old_string, new_string, replace_all)
    else:
        return {"success": False, "error": f"Invalid mode: '{mode}'. Use: write, append, edit"}

    # Auto-index supported files (.md, .org) to ChromaDB
    if result.get("success") and auto_index and is_indexable(relative_path):
        try:
            from .memory import index_file
            index_result = index_file(relative_path)
            result["indexed"] = index_result.get("success", False)
        except Exception as e:
            logger.debug(f"Auto-index failed for {relative_path}: {e}")
            result["indexed"] = False

    return result


def _store_memory(name, content, scope, project, tags, importance, overwrite,
                  skip_secret_scan):
    """Route to memory_crud.memory_write with importance conversion."""
    from .memory_crud import memory_write

    # Convert float importance to categorical if provided
    importance_str = "medium"
    if importance is not None:
        if importance >= 0.9:
            importance_str = "critical"
        elif importance >= 0.7:
            importance_str = "high"
        elif importance >= 0.4:
            importance_str = "medium"
        else:
            importance_str = "low"

    return memory_write(
        name=name or "", content=content, scope=scope,
        project=project, tags=tags,
        importance=importance_str, overwrite=overwrite,
        skip_secret_scan=skip_secret_scan,
    )


def _store_tier2(content, content_type, name, importance_score, source, tags,
                 session_id, extra_metadata, skip_secret_scan):
    """Route to tier2.tier2_write (creates new with auto-generated ID)."""
    from .tier2 import tier2_write

    return tier2_write(
        content=content, content_type=content_type,
        name=name, importance_score=importance_score or 0.5,
        source=source or "manual",
        tags=tags,
        session_id=session_id,
        extra_metadata=extra_metadata,
        skip_secret_scan=skip_secret_scan,
    )


def _update_tier2(doc_id, content, importance, tags, source, extra_metadata):
    """Update existing tier2 content by ID (ChromaDB upsert).

    Reads existing doc, merges updates, upserts back.
    """
    from .tier2 import tier2_read, tier2_upsert

    # Read existing to get current metadata
    existing = tier2_read(doc_id)
    if not existing.get("found", False):
        return {"success": False, "error": f"Tier 2 document '{doc_id}' not found"}

    # Build updated metadata (merge, don't replace)
    metadata = existing.get("metadata", {})
    if importance is not None:
        metadata["importance_score"] = str(importance)
    if tags is not None:
        metadata["tags"] = ",".join(tags) if tags else ""
    if source is not None:
        metadata["source"] = source
    if extra_metadata:
        metadata.update(extra_metadata)

    updated_content = content if content else existing.get("content", "")

    return tier2_upsert(
        doc_id=doc_id,
        content=updated_content,
        metadata=metadata,
    )
