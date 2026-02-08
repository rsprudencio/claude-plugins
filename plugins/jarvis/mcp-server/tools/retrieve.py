"""Unified content retrieval for Jarvis.

Routes reads based on parameters:
- query -> semantic search (query_vault)
- id -> ID-based read (tier2_read or doc_read by prefix)
- name -> memory read by name (memory_crud.memory_read)
- list_type -> list content (tier2_list or memory_list)
"""
from typing import Optional


def retrieve(
    query: Optional[str] = None,
    id: Optional[str] = None,
    name: Optional[str] = None,
    list_type: Optional[str] = None,
    n_results: int = 5,
    type_filter: Optional[str] = None,
    min_importance: Optional[float] = None,
    source: Optional[str] = None,
    scope: str = "global",
    project: Optional[str] = None,
    tag: Optional[str] = None,
    importance: Optional[str] = None,
    limit: int = 20,
    filter: Optional[dict] = None,
    include_metadata: bool = True,
) -> dict:
    """Unified read/search entry point.

    Routing priority:
    1. query -> semantic search across all content
    2. id -> read specific document by ID (routes by prefix)
    3. name -> read strategic memory by name
    4. list_type -> list content ("tier2" or "memory")
    """
    # Count how many routing params are set
    routing_params = sum(1 for p in [query, id, name, list_type] if p)
    if routing_params == 0:
        return {
            "success": False,
            "error": "Provide one of: query (search), id (read by ID), "
                     "name (memory name), list_type ('tier2' or 'memory')",
        }
    if routing_params > 1:
        return {
            "success": False,
            "error": "Provide only ONE of: query, id, name, list_type",
        }

    # Route 1: Semantic search
    if query:
        from .query import query_vault
        return query_vault(query=query, n_results=n_results, filter=filter)

    # Route 2: ID-based read
    if id:
        return _read_by_id(id, include_metadata)

    # Route 3: Memory read by name
    if name:
        from .memory_crud import memory_read
        return memory_read(name=name, scope=scope, project=project)

    # Route 4: List content
    if list_type:
        return _list_content(
            list_type=list_type, type_filter=type_filter,
            min_importance=min_importance, source=source,
            scope=scope, project=project,
            tag=tag, importance=importance, limit=limit,
        )

    return {"success": False, "error": "No valid routing parameter provided"}


def _read_by_id(doc_id: str, include_metadata: bool):
    """Route ID-based reads by prefix."""
    from .namespaces import get_tier, TIER_CHROMADB

    tier = get_tier(doc_id)
    if tier == TIER_CHROMADB:
        # Tier 2: use tier2_read (increments retrieval_count)
        from .tier2 import tier2_read
        return tier2_read(doc_id)
    else:
        # Tier 1: use doc_read for ChromaDB-indexed content
        from .query import doc_read
        return doc_read(ids=[doc_id], include_metadata=include_metadata)


def _list_content(list_type, type_filter, min_importance, source,
                  scope, project, tag, importance, limit):
    """Route list operations."""
    if list_type == "tier2":
        from .tier2 import tier2_list
        return tier2_list(
            content_type=type_filter,
            min_importance=min_importance,
            source=source,
            limit=limit,
        )
    elif list_type == "memory":
        from .memory_crud import memory_list
        return memory_list(
            scope=scope, project=project,
            tag=tag, importance=importance,
        )
    else:
        return {
            "success": False,
            "error": f"Invalid list_type '{list_type}'. Use: 'tier2' or 'memory'",
        }
