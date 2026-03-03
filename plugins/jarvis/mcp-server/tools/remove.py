"""Unified content removal for Jarvis.

Routes deletes based on parameters:
- id -> delete by document ID (routes by prefix)
- name -> delete strategic memory by name
"""

import os
from typing import Optional

from .namespaces import schema_for_id, SCHEMA_LOCAL, SCHEMA_OBSIDIAN
from .routing_utils import validate_exactly_one, parse_memory_scope


def _remove_vault_file(id: str, confirm: bool = False) -> dict:
    """Delete a vault file from disk and clean its database index entries."""
    from .file_ops import validate_vault_path
    from .memory import _delete_existing_chunks

    file_path = id[7:].split("#chunk-")[0]  # strip vault:: and #chunk-N

    valid, full_path, error = validate_vault_path(file_path)
    if not valid:
        return {"success": False, "error": error}

    if not os.path.exists(full_path):
        return {
            "success": False,
            "error": f"File not found: '{file_path}'",
        }

    if not confirm:
        return {
            "success": True,
            "confirmation_required": True,
            "file_path": file_path,
            "message": f"Delete vault file '{file_path}'? "
            f"Pass confirm=True to proceed.",
        }

    # Delete the file
    os.remove(full_path)

    # Clean up database index entries
    try:
        deleted_chunks = _delete_existing_chunks(file_path)
    except Exception:
        deleted_chunks = 0

    return {
        "success": True,
        "file_path": file_path,
        "chunks_removed": deleted_chunks,
    }


def remove(
    id: Optional[str] = None,
    name: Optional[str] = None,
    scope: str = "global",
    project: Optional[str] = None,
    confirm: bool = False,
) -> dict:
    """Unified delete entry point."""
    error = validate_exactly_one(
        [id, name],
        "Provide id (document ID) or name (memory name)",
        "Provide only ONE of: id, name",
    )
    if error:
        return error

    if id:
        schema = schema_for_id(id)

        if schema == SCHEMA_OBSIDIAN:
            return _remove_vault_file(id, confirm=confirm)

        if id.startswith("memory::"):
            from .namespaces import parse_id
            from .memory_crud import memory_delete

            parsed = parse_id(id)
            scope, project = parse_memory_scope(parsed)
            return memory_delete(
                name=parsed.content_id,
                scope=scope,
                project=project,
                confirm=confirm,
            )

        # Local content (obs::, pattern::, learning::, etc.)
        if schema == SCHEMA_LOCAL:
            # Ownership check for multi-user deployments
            from jarvis_common.auth import get_current_user

            user = get_current_user()
            if user != "anonymous":
                from .content import content_read

                existing = content_read(id)
                if existing.get("found"):
                    owner = existing.get("metadata", {}).get("user")
                    if owner and owner != user:
                        return {
                            "success": False,
                            "error": "Cannot delete another user's content",
                        }

            from .content import content_delete

            return content_delete(id)

        return {
            "success": False,
            "error": f"Unrecognized ID prefix in '{id}'. "
            f"Use id= for content (obs::, pattern::, etc.) "
            f"or vault content (vault::), or name= for strategic memories.",
        }

    if name:
        from .memory_crud import memory_delete

        return memory_delete(
            name=name,
            scope=scope,
            project=project,
            confirm=confirm,
        )

    return {"success": False, "error": "No valid parameter provided"}
