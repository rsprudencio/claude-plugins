"""Unified content removal for Jarvis.

Routes deletes based on parameters:
- id -> delete by document ID (routes by prefix)
- name -> delete strategic memory by name
"""
import os
from typing import Optional

from .namespaces import get_tier, TIER_CHROMADB


def _remove_vault_file(id: str, confirm: bool = False) -> dict:
    """Delete a vault file from disk and clean its ChromaDB index entries."""
    from .file_ops import validate_vault_path
    from .memory import _get_collection, _delete_existing_chunks

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

    # Clean up ChromaDB index
    try:
        collection = _get_collection()
        deleted_chunks = _delete_existing_chunks(collection, file_path)
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
    if not id and not name:
        return {
            "success": False,
            "error": "Provide id (document ID) or name (memory name)",
        }
    if id and name:
        return {
            "success": False,
            "error": "Provide only ONE of: id, name",
        }

    if id:
        tier = get_tier(id)
        if tier == TIER_CHROMADB:
            from .tier2 import tier2_delete
            return tier2_delete(id)
        elif id.startswith("vault::"):
            return _remove_vault_file(id, confirm=confirm)
        elif id.startswith("memory::"):
            mem_name = id[8:]
            return {
                "success": False,
                "error": f"This is a strategic memory. "
                         f"Use jarvis_remove(name=\"{mem_name}\", confirm=True) instead.",
            }
        else:
            return {
                "success": False,
                "error": f"Unrecognized ID prefix in '{id}'. "
                         f"Use id= for Tier 2 content (obs::, pattern::, etc.) "
                         f"or vault content (vault::), or name= for strategic memories.",
            }

    if name:
        from .memory_crud import memory_delete
        return memory_delete(
            name=name, scope=scope,
            project=project, confirm=confirm,
        )

    return {"success": False, "error": "No valid parameter provided"}
