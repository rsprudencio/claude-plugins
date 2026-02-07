"""Vault memory querying for semantic search.

Provides query, read, and stats operations against the ChromaDB jarvis collection.
Uses the shared ChromaDB client from tools.memory.

All document IDs use namespaced format (vault:: prefix) for type-safe identification.
"""
import re
from typing import Optional

from .memory import _get_collection
from .namespaces import parse_id, ALL_TYPES


def _compute_relevance(distance: float, importance: str = "medium") -> float:
    """Convert ChromaDB cosine distance to relevance score with importance boost.

    ChromaDB cosine distance ranges from 0 (identical) to 2 (opposite).
    We convert to a 0-1 relevance score and apply importance adjustments.
    """
    base = 1.0 - (distance / 2.0)
    boost = {"high": 0.10, "medium": 0.0, "low": -0.05}.get(importance, 0.0)
    return max(0.0, min(1.0, base + boost))


def _extract_preview(content: str, max_len: int = 150) -> str:
    """Extract a clean preview from document content.

    Strips YAML frontmatter and truncates at a word boundary.
    """
    # Strip frontmatter
    stripped = re.sub(r'^---\s*\n.*?\n---\s*\n', '', content, count=1, flags=re.DOTALL)
    # Strip leading headings and whitespace
    stripped = re.sub(r'^#+\s+.*$', '', stripped, count=1, flags=re.MULTILINE).strip()
    # Collapse whitespace
    stripped = re.sub(r'\s+', ' ', stripped).strip()

    if len(stripped) <= max_len:
        return stripped

    # Truncate at word boundary
    truncated = stripped[:max_len]
    last_space = truncated.rfind(' ')
    if last_space > max_len * 0.5:
        truncated = truncated[:last_space]
    return truncated + "..."


def _translate_filter(filter_dict: Optional[dict]) -> Optional[dict]:
    """Translate clean filter dict to ChromaDB where syntax.

    Handles the metadata schema where:
    - 'type' is the universal content type (vault, memory, observation, etc.)
    - 'vault_type' is the vault-entry type (note, journal, work, etc.)

    When users filter by type with a vault-entry value (note, journal, etc.),
    we transparently map to vault_type. When they use a content type value
    (vault, memory, etc.), we use the universal type field.

    Input: {"directory": "journal", "type": "note", "importance": "high", "tags": "work"}
    Output: {"$and": [{"directory": "journal"}, {"vault_type": "note"}, ...]}
    """
    if not filter_dict:
        return None

    conditions = []

    if "directory" in filter_dict and filter_dict["directory"]:
        conditions.append({"directory": filter_dict["directory"]})

    if "type" in filter_dict and filter_dict["type"]:
        type_val = filter_dict["type"]
        if type_val in ALL_TYPES:
            # Universal content type (vault, memory, observation, etc.)
            conditions.append({"type": type_val})
        else:
            # Vault-entry type (note, journal, work, etc.)
            conditions.append({"vault_type": type_val})

    if "importance" in filter_dict and filter_dict["importance"]:
        conditions.append({"importance": filter_dict["importance"]})

    if "tags" in filter_dict and filter_dict["tags"]:
        # Tags stored as comma-separated string in metadata
        # ChromaDB $contains checks if value is substring of stored string
        conditions.append({"tags": {"$contains": filter_dict["tags"].split(",")[0].strip()}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _display_path(doc_id: str) -> str:
    """Strip namespace prefix from ID for display purposes."""
    parsed = parse_id(doc_id)
    return parsed.content_id


def query_vault(query: str, n_results: int = 5,
                filter: Optional[dict] = None) -> dict:
    """Semantic search across vault memory.

    Args:
        query: Natural language search query
        n_results: Max results (capped at 20)
        filter: Optional metadata filters (directory, type, importance, tags)

    Returns:
        Formatted results dict with titles, paths, excerpts, relevance scores
    """
    try:
        collection = _get_collection()
    except Exception as e:
        return {"success": False, "error": f"ChromaDB unavailable: {e}"}

    total = collection.count()
    if total == 0:
        return {
            "success": True,
            "query": query,
            "results": [],
            "total_in_collection": 0,
            "message": "No documents indexed. Run /memory-index."
        }

    n_results = min(max(1, n_results), 20)
    where = _translate_filter(filter)

    try:
        query_params = {
            "query_texts": [query],
            "n_results": min(n_results, total),
        }
        if where:
            query_params["where"] = where

        raw = collection.query(**query_params)
    except Exception as e:
        return {"success": False, "error": f"Query failed: {e}"}

    results = []
    ids = raw.get("ids", [[]])[0]
    distances = raw.get("distances", [[]])[0]
    documents = raw.get("documents", [[]])[0]
    metadatas = raw.get("metadatas", [[]])[0]

    for rank, (doc_id, distance, document, metadata) in enumerate(
        zip(ids, distances, documents, metadatas), start=1
    ):
        importance = (metadata or {}).get("importance", "medium")
        relevance = _compute_relevance(distance, importance)
        preview = _extract_preview(document) if document else ""
        title = (metadata or {}).get("title", doc_id)
        # Use vault_type if available, fall back to type
        doc_type = (metadata or {}).get("vault_type") or (metadata or {}).get("type", "unknown")
        doc_importance = (metadata or {}).get("importance", "medium")
        tags = (metadata or {}).get("tags", "")

        results.append({
            "rank": rank,
            "path": _display_path(doc_id),
            "title": title,
            "type": doc_type,
            "importance": doc_importance,
            "relevance": round(relevance, 3),
            "preview": preview,
            "tags": tags,
        })

    return {
        "success": True,
        "query": query,
        "results": results,
        "total_in_collection": total,
    }


def memory_read(ids: list, include_metadata: bool = True) -> dict:
    """Read specific documents from vault memory by ID.

    Accepts both namespaced IDs (vault::notes/my-note.md) and bare paths
    (notes/my-note.md). Bare paths are automatically prefixed with vault::.

    Args:
        ids: Document IDs (vault-relative paths or namespaced IDs)
        include_metadata: Whether to include metadata in response

    Returns:
        Documents with optional metadata, plus not_found list
    """
    if not ids:
        return {"success": False, "error": "No IDs provided"}

    try:
        collection = _get_collection()
    except Exception as e:
        return {"success": False, "error": f"ChromaDB unavailable: {e}"}

    # Normalize IDs: bare paths get vault:: prefix
    lookup_ids = []
    id_map = {}  # lookup_id -> original_id (for display)
    for doc_id in ids:
        if "::" in doc_id:
            lookup_ids.append(doc_id)
            id_map[doc_id] = doc_id
        else:
            namespaced = f"vault::{doc_id}"
            lookup_ids.append(namespaced)
            id_map[namespaced] = doc_id

    include = ["documents"]
    if include_metadata:
        include.append("metadatas")

    try:
        raw = collection.get(ids=lookup_ids, include=include)
    except Exception as e:
        return {"success": False, "error": f"Read failed: {e}"}

    found = []
    found_ids = set(raw.get("ids", []))

    not_found = []
    for lid in lookup_ids:
        if lid not in found_ids:
            not_found.append(id_map.get(lid, lid))

    for i, doc_id in enumerate(raw.get("ids", [])):
        entry = {
            "id": _display_path(doc_id),
            "document": raw["documents"][i] if raw.get("documents") else None,
        }
        if include_metadata and raw.get("metadatas"):
            entry["metadata"] = raw["metadatas"][i]
        found.append(entry)

    return {
        "success": True,
        "documents": found,
        "not_found": not_found,
    }


def memory_stats(sample_size: int = 5) -> dict:
    """Get memory system health and statistics.

    Args:
        sample_size: Number of sample entries to peek

    Returns:
        Stats dict with count, samples, type distribution
    """
    try:
        collection = _get_collection()
    except Exception as e:
        return {"success": False, "error": f"ChromaDB unavailable: {e}"}

    total = collection.count()

    if total == 0:
        return {
            "success": True,
            "total_documents": 0,
            "samples": [],
            "message": "No documents indexed. Run /memory-index.",
        }

    sample_size = min(max(1, sample_size), total)

    try:
        peek = collection.peek(limit=sample_size)
    except Exception as e:
        return {"success": False, "error": f"Stats failed: {e}"}

    samples = []
    for i, doc_id in enumerate(peek.get("ids", [])):
        meta = peek["metadatas"][i] if peek.get("metadatas") else {}
        # Use vault_type for vault entries, fall back to type
        entry_type = (meta or {}).get("vault_type") or (meta or {}).get("type", "unknown")
        samples.append({
            "path": _display_path(doc_id),
            "title": (meta or {}).get("title", doc_id),
            "type": entry_type,
        })

    return {
        "success": True,
        "total_documents": total,
        "samples": samples,
    }
