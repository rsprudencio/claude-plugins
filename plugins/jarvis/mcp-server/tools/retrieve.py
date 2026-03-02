"""Unified content retrieval for Jarvis.

Routes reads based on parameters:
- query -> semantic search (query_vault) across core + vault
- id -> ID-based read (content_read or doc_read by prefix)
- name -> memory read by name (memory_crud.memory_read)
- list_type -> list content (content_list or memory_list)
"""

from typing import Optional

from .routing_utils import validate_exactly_one


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
    importance: Optional[float] = None,
    limit: int = 20,
    filter: Optional[dict] = None,
    include_metadata: bool = True,
    include_content: bool = False,
    sort_by: str = "importance_desc",
    session_id: Optional[str] = None,
    user: Optional[str] = None,
) -> dict:
    """Unified read/search entry point.

    Routing priority:
    1. query -> semantic search across all content
    2. id -> read specific document by ID (routes by prefix)
    3. name -> read strategic memory by name
    4. list_type -> list content ("content"/"tier2" or "memory")
    """
    # Count how many routing params are set
    error = validate_exactly_one(
        [query, id, name, list_type],
        "Provide one of: query (search), id (read by ID), "
        "name (memory name), list_type ('content' or 'memory')",
        "Provide only ONE of: query, id, name, list_type",
    )
    if error:
        return error

    # Route 1: Semantic search
    if query:
        from .query import query_vault

        return query_vault(query=query, n_results=n_results, filter=filter, user=user)

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
            list_type=list_type,
            type_filter=type_filter,
            min_importance=min_importance,
            source=source,
            scope=scope,
            project=project,
            tag=tag,
            importance=importance,
            limit=limit,
            sort_by=sort_by,
            session_id=session_id,
            include_content=include_content,
        )

    return {"success": False, "error": "No valid routing parameter provided"}


def _read_by_id(doc_id: str, include_metadata: bool):
    """Route ID-based reads by schema prefix and normalize the response format."""
    from .namespaces import schema_for_id, SCHEMA_CORE, SCHEMA_VAULT

    schema = schema_for_id(doc_id)
    if schema == SCHEMA_CORE:
        # Core memory: use content_read (increments retrieval_count)
        from .content import content_read

        return content_read(doc_id)
    else:
        # Vault document: use doc_read for indexed content
        from .query import doc_read

        result = doc_read(ids=[doc_id], include_metadata=include_metadata)

        # Normalize to same format as content_read for single-ID lookup
        if result.get("success") and result.get("documents"):
            doc = result["documents"][0]
            return {
                "success": True,
                "found": True,
                "id": doc.get("id"),
                "path": doc.get("path"),
                "content": doc.get("document"),
                "metadata": doc.get("metadata"),
            }
        else:
            return {
                "success": result.get("success", False),
                "found": False,
                "id": doc_id,
                "error": result.get("error") if not result.get("documents") else None,
            }


def _list_content(
    list_type,
    type_filter,
    min_importance,
    source,
    scope,
    project,
    tag,
    importance,
    limit,
    sort_by="importance_desc",
    session_id=None,
    include_content=False,
):
    """Route list operations."""
    # "tier2" kept as deprecated alias for "content" (removed in v3.1)
    if list_type in ("content", "tier2"):
        from .content import content_list

        return content_list(
            content_type=type_filter,
            min_importance=min_importance,
            source=source,
            limit=limit,
            sort_by=sort_by,
            session_id=session_id,
            include_content=include_content,
        )
    elif list_type == "memory":
        from .memory_crud import memory_list

        return memory_list(
            scope=scope,
            project=project,
            tag=tag,
            importance=importance,
            include_content=include_content,
        )
    else:
        return {
            "success": False,
            "error": f"Invalid list_type '{list_type}'. Use: 'content' or 'memory'",
        }
