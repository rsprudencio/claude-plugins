"""Unified content removal for Jarvis.

Routes deletes based on parameters:
- id -> delete by document ID (routes by prefix)
- name -> delete strategic memory by name
"""
from typing import Optional

from .namespaces import get_tier, TIER_CHROMADB


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
        else:
            return {
                "success": False,
                "error": "Only Tier 2 content can be deleted by ID. "
                         "For vault files, delete manually.",
            }

    if name:
        from .memory_crud import memory_delete
        return memory_delete(
            name=name, scope=scope,
            project=project, confirm=confirm,
        )

    return {"success": False, "error": "No valid parameter provided"}
