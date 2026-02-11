"""Vault memory querying for semantic search.

Provides query, read, and stats operations against the ChromaDB jarvis collection.
Uses the shared ChromaDB client from tools.memory.

All document IDs use namespaced format (vault:: prefix) for type-safe identification.
"""
import os
import re
import time
from datetime import datetime, timezone
from typing import Optional

from .memory import _get_collection
from .paths import get_path, SENSITIVE_PATHS
from .namespaces import parse_id, ALL_TYPES, get_tier, TIER_FILE, TIER_CHROMADB
from .expansion import expand_query as _expand_query
from .config import get_expansion_config, get_per_prompt_config


def _compute_relevance(distance: float, importance: str = "medium",
                       updated_at: Optional[str] = None,
                       importance_score: Optional[float] = None) -> float:
    """Convert ChromaDB cosine distance to relevance score with boosts.

    ChromaDB cosine distance ranges from 0 (identical) to 2 (opposite).
    We convert to a 0-1 relevance score and apply importance + recency adjustments.

    When importance_score (float 0-1 from scoring module) is available, it
    provides a more nuanced boost than the string importance field.
    """
    base = 1.0 - (distance / 2.0)

    # Use numeric importance_score when available, fall back to string importance
    if importance_score is not None:
        # Map 0.0-1.0 score to -0.12..+0.12 boost (centered at 0.5)
        boost = (importance_score - 0.5) * 0.24
    else:
        boost = {"high": 0.10, "critical": 0.12, "medium": 0.0, "low": -0.05}.get(importance, 0.0)

    # Recency boost: recent updates get a small relevance bump
    recency_boost = 0.0
    if updated_at:
        try:
            updated = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days_ago = (now - updated).total_seconds() / 86400
            if days_ago <= 1:
                recency_boost = 0.08
            elif days_ago <= 7:
                recency_boost = 0.05
        except (ValueError, TypeError):
            pass

    return max(0.0, min(1.0, base + boost + recency_boost))


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


def _increment_retrieval_counts(collection, doc_ids: list, increment: float = 1.0) -> None:
    """Batch increment retrieval counts for Tier 2 documents.

    Best-effort operation: errors are logged but don't block query response.
    Only updates Tier 2 documents (Tier 1 doesn't track retrieval counts).

    Args:
        collection: ChromaDB collection
        doc_ids: List of document IDs to increment
        increment: Amount to add to retrieval_count (default 1.0).
                   Use fractional values (e.g. 0.01) for passive surfacing.
    """
    if not doc_ids:
        return

    try:
        # Filter to only Tier 2 IDs
        tier2_ids = [doc_id for doc_id in doc_ids if get_tier(doc_id) == TIER_CHROMADB]
        if not tier2_ids:
            return

        # Batch get current data
        result = collection.get(ids=tier2_ids, include=["documents", "metadatas"])

        # Increment counts
        updated_ids = []
        updated_docs = []
        updated_metas = []

        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

        for i, doc_id in enumerate(result["ids"]):
            metadata = result["metadatas"][i]
            retrieval_count = float(metadata.get("retrieval_count", "0"))
            retrieval_count = round(retrieval_count + increment, 2)

            updated_metadata = {**metadata}
            updated_metadata["retrieval_count"] = str(retrieval_count)
            updated_metadata["updated_at"] = now_iso

            updated_ids.append(doc_id)
            updated_docs.append(result["documents"][i])
            updated_metas.append(updated_metadata)

        # Batch upsert
        if updated_ids:
            collection.upsert(ids=updated_ids, documents=updated_docs, metadatas=updated_metas)

    except Exception as e:
        # Log but don't fail query
        import logging
        logger = logging.getLogger("jarvis-tools")
        logger.warning(f"Failed to increment retrieval counts: {e}")


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
            "message": "No documents indexed. Ask Jarvis to 'index my vault' or use jarvis_index_vault tool."
        }

    n_results = min(max(1, n_results), 20)
    where = _translate_filter(filter)

    # Query expansion
    expansion_config = get_expansion_config()
    expansion = _expand_query(query, expansion_config)
    search_text = expansion["expanded"]

    # Over-fetch to account for chunk deduplication
    fetch_count = min(n_results * 3, 60, total)

    try:
        query_params = {
            "query_texts": [search_text],
            "n_results": fetch_count,
        }
        if where:
            query_params["where"] = where

        raw = collection.query(**query_params)
    except Exception as e:
        return {"success": False, "error": f"Query failed: {e}"}

    # Build raw result entries with relevance scores
    raw_entries = []
    ids = raw.get("ids", [[]])[0]
    distances = raw.get("distances", [[]])[0]
    documents = raw.get("documents", [[]])[0]
    metadatas = raw.get("metadatas", [[]])[0]

    for doc_id, distance, document, metadata in zip(ids, distances, documents, metadatas):
        meta = metadata or {}
        importance = meta.get("importance", "medium")
        updated_at = meta.get("updated_at")

        # Use numeric importance_score if available
        imp_score = None
        if "importance_score" in meta:
            try:
                imp_score = float(meta["importance_score"])
            except (ValueError, TypeError):
                pass

        relevance = _compute_relevance(distance, importance, updated_at, imp_score)

        # Determine parent file for chunk dedup
        parent_file = meta.get("parent_file")
        if not parent_file:
            # Legacy: strip #chunk-N from ID
            parsed = parse_id(doc_id)
            parent_file = parsed.content_id

        raw_entries.append({
            "doc_id": doc_id,
            "parent_file": parent_file,
            "relevance": relevance,
            "document": document,
            "metadata": meta,
        })

    # Chunk deduplication: keep best-relevance chunk per parent_file
    best_per_file = {}
    for entry in raw_entries:
        pf = entry["parent_file"]
        if pf not in best_per_file or entry["relevance"] > best_per_file[pf]["relevance"]:
            best_per_file[pf] = entry

    # Sort by relevance descending and trim to n_results
    deduped = sorted(best_per_file.values(), key=lambda e: e["relevance"], reverse=True)[:n_results]

    results = []
    all_ids = []
    for rank, entry in enumerate(deduped, start=1):
        meta = entry["metadata"]
        doc_id = entry["doc_id"]
        preview = _extract_preview(entry["document"]) if entry["document"] else ""
        title = meta.get("title", doc_id)
        doc_type = meta.get("vault_type") or meta.get("type", "unknown")
        doc_importance = meta.get("importance", "medium")
        tags = meta.get("tags", "")
        chunk_heading = meta.get("chunk_heading", "")

        tier = get_tier(doc_id)
        if tier == TIER_FILE:
            source = "file"
        else:
            source = meta.get("type", "chromadb")

        result_entry = {
            "rank": rank,
            "path": entry["parent_file"],
            "title": title,
            "type": doc_type,
            "importance": doc_importance,
            "relevance": round(entry["relevance"], 3),
            "preview": preview,
            "tags": tags,
            "tier": tier,
            "source": source,
        }
        if chunk_heading:
            result_entry["chunk_heading"] = chunk_heading
        results.append(result_entry)
        all_ids.append(doc_id)

    # Increment retrieval counts for Tier 2 results (best-effort, non-blocking)
    _increment_retrieval_counts(collection, all_ids)

    response = {
        "success": True,
        "query": query,
        "results": results,
        "total_in_collection": total,
    }

    # Include expansion metadata when terms were added
    if expansion["terms_added"]:
        response["expansion"] = {
            "terms_added": expansion["terms_added"],
            "intent": expansion["intent"],
        }

    return response


def semantic_context(query: str, max_results: int = 5,
                     threshold: float = 0.5,
                     max_content_length: int = 500) -> dict:
    """Search vault memories for per-prompt context injection.

    Optimized for automatic, per-message use. Differs from query_vault():
    - Applies minimum relevance threshold (filters low-scoring results)
    - Returns longer content previews (default 500 chars vs 150)
    - Fractionally increments retrieval counts (configurable, default 0.01)
    - Filters out sensitive directories (documents/, people/)
    - Returns query_ms timing for performance monitoring
    - Silently returns empty on any error (graceful degradation)

    Args:
        query: User's raw prompt text
        max_results: Maximum memories to return (default 5)
        threshold: Minimum relevance score 0.0-1.0 (default 0.5)
        max_content_length: Character limit per content preview (default 500)

    Returns:
        Dict with matches list and metadata
    """
    start = time.time()

    try:
        collection = _get_collection()
    except Exception:
        return {"matches": [], "query_ms": 0, "total_searched": 0}

    total = collection.count()
    if total == 0:
        return {"matches": [], "query_ms": 0, "total_searched": 0}

    # Query expansion (reuse existing infrastructure)
    expansion_config = get_expansion_config()
    expansion = _expand_query(query, expansion_config)
    search_text = expansion["expanded"]

    # Over-fetch to account for chunk dedup + threshold filtering
    fetch_count = min(max_results * 4, 60, total)

    try:
        raw = collection.query(
            query_texts=[search_text],
            n_results=fetch_count,
        )
    except Exception:
        return {"matches": [], "query_ms": 0, "total_searched": total}

    # Build raw entries with relevance scores
    raw_entries = []
    ids = raw.get("ids", [[]])[0]
    distances = raw.get("distances", [[]])[0]
    documents = raw.get("documents", [[]])[0]
    metadatas = raw.get("metadatas", [[]])[0]

    skipped_sensitive = 0

    for doc_id, distance, document, metadata in zip(ids, distances, documents, metadatas):
        meta = metadata or {}

        # Filter sensitive directories
        directory = meta.get("directory", "")
        if directory in SENSITIVE_PATHS:
            skipped_sensitive += 1
            continue

        importance = meta.get("importance", "medium")
        updated_at = meta.get("updated_at")

        imp_score = None
        if "importance_score" in meta:
            try:
                imp_score = float(meta["importance_score"])
            except (ValueError, TypeError):
                pass

        relevance = _compute_relevance(distance, importance, updated_at, imp_score)

        # Apply threshold
        if relevance < threshold:
            continue

        # Determine parent file for chunk dedup
        parent_file = meta.get("parent_file")
        if not parent_file:
            parsed = parse_id(doc_id)
            parent_file = parsed.content_id

        raw_entries.append({
            "doc_id": doc_id,
            "parent_file": parent_file,
            "relevance": relevance,
            "document": document,
            "metadata": meta,
        })

    # Chunk deduplication: keep best-relevance chunk per parent_file
    best_per_file = {}
    for entry in raw_entries:
        pf = entry["parent_file"]
        if pf not in best_per_file or entry["relevance"] > best_per_file[pf]["relevance"]:
            best_per_file[pf] = entry

    # Sort by relevance descending, trim to max_results
    deduped = sorted(best_per_file.values(), key=lambda e: e["relevance"], reverse=True)[:max_results]

    # Fractional retrieval bump for passively surfaced results
    if deduped:
        per_prompt_config = get_per_prompt_config()
        passive_increment = per_prompt_config.get("passive_retrieval_increment", 0.01)
        if passive_increment > 0:
            surfaced_ids = [entry["doc_id"] for entry in deduped]
            _increment_retrieval_counts(collection, surfaced_ids, increment=passive_increment)

    matches = []
    for entry in deduped:
        meta = entry["metadata"]
        content = _extract_preview(entry["document"], max_len=max_content_length) if entry["document"] else ""
        doc_type = meta.get("vault_type") or meta.get("type", "unknown")
        chunk_heading = meta.get("chunk_heading", "")

        match = {
            "source": entry["parent_file"],
            "title": meta.get("title", entry["parent_file"]),
            "relevance": round(entry["relevance"], 3),
            "type": doc_type,
            "content": content,
        }
        if chunk_heading:
            match["heading"] = chunk_heading
        matches.append(match)

    query_ms = round((time.time() - start) * 1000, 1)

    return {
        "matches": matches,
        "query_ms": query_ms,
        "total_searched": total,
        "skipped_sensitive": skipped_sensitive,
    }


def doc_read(ids: list, include_metadata: bool = True) -> dict:
    """Read specific documents from ChromaDB by ID.

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


def collection_stats(sample_size: int = 5, detailed: bool = False) -> dict:
    """Get memory system health and statistics.

    Args:
        sample_size: Number of sample entries to peek
        detailed: Include per-type/namespace breakdowns and storage size

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
            "message": "No documents indexed. Ask Jarvis to 'index my vault' or use jarvis_index_vault tool.",
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

    result = {
        "success": True,
        "total_documents": total,
        "samples": samples,
    }

    # Detailed breakdown
    if detailed:
        try:
            all_meta = collection.get(include=["metadatas"])
            type_counts = {}
            namespace_counts = {}

            for meta in all_meta.get("metadatas", []):
                if not meta:
                    continue
                # Count by type
                content_type = meta.get("type", "unknown")
                type_counts[content_type] = type_counts.get(content_type, 0) + 1
                # Count by namespace
                ns = meta.get("namespace", "unknown")
                namespace_counts[ns] = namespace_counts.get(ns, 0) + 1

            result["type_breakdown"] = type_counts
            result["namespace_breakdown"] = namespace_counts

            # Storage size
            storage_bytes = 0
            db_dir = get_path("db_path")
            if os.path.isdir(db_dir):
                for dirpath, _, filenames in os.walk(db_dir):
                    for f in filenames:
                        storage_bytes += os.path.getsize(os.path.join(dirpath, f))
            result["storage_bytes"] = storage_bytes
            result["storage_mb"] = round(storage_bytes / (1024 * 1024), 2)

        except Exception as e:
            result["detailed_error"] = str(e)

    return result


# Backward-compatible aliases (will be removed in future version)
memory_read = doc_read
memory_stats = collection_stats
