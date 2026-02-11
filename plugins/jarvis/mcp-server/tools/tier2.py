"""Tier 2 (ChromaDB-first) content CRUD operations.

Tier 2 stores auto-generated, ephemeral content in ChromaDB without file backing.
Content types: observation, pattern, summary, relationship, hint, plan.

Tier 2 content can be promoted to Tier 1 (file-backed) via the promotion module.
"""
import logging
import time
from datetime import datetime, timezone
from typing import Optional

from .memory import _get_collection
from .namespaces import (
    ContentType,
    observation_id, pattern_id, summary_id, code_id,
    relationship_id, hint_id, plan_id, learning_id, decision_id,
    NAMESPACE_OBS, NAMESPACE_PATTERN, NAMESPACE_SUMMARY, NAMESPACE_CODE,
    NAMESPACE_REL, NAMESPACE_HINT, NAMESPACE_PLAN, NAMESPACE_LEARNING, NAMESPACE_DECISION,
)
from .secret_scan import scan_for_secrets

logger = logging.getLogger("jarvis-tools")

VALID_CONTENT_TYPES = (
    "observation", "pattern", "summary", "code",
    "relationship", "hint", "plan", "learning", "decision"
)

# Map content_type string to (ContentType enum, NAMESPACE constant, ID generator)
_TYPE_MAP = {
    "observation": (ContentType.OBSERVATION, NAMESPACE_OBS, observation_id),
    "pattern": (ContentType.PATTERN, NAMESPACE_PATTERN, pattern_id),
    "summary": (ContentType.SUMMARY, NAMESPACE_SUMMARY, summary_id),
    "code": (ContentType.CODE, NAMESPACE_CODE, code_id),
    "relationship": (ContentType.RELATIONSHIP, NAMESPACE_REL, relationship_id),
    "hint": (ContentType.HINT, NAMESPACE_HINT, hint_id),
    "plan": (ContentType.PLAN, NAMESPACE_PLAN, plan_id),
    "learning": (ContentType.LEARNING, NAMESPACE_LEARNING, learning_id),
    "decision": (ContentType.DECISION, NAMESPACE_DECISION, decision_id),
}


def tier2_write(
    content: str,
    content_type: str,
    name: Optional[str] = None,
    importance_score: float = 0.5,
    source: str = "auto-extract",
    tags: Optional[list] = None,
    session_id: Optional[str] = None,
    extra_metadata: Optional[dict] = None,
    skip_secret_scan: bool = False,
) -> dict:
    """Write Tier 2 content to ChromaDB.

    Args:
        content: Document content (markdown)
        content_type: Type of content (observation, pattern, summary, etc.)
        name: Required for pattern/plan/decision, optional for others
        importance_score: Importance score 0.0-1.0 (default 0.5)
        source: Source of content (default "auto-extract")
        tags: Optional list of tags for categorization
        session_id: Optional session identifier
        extra_metadata: Optional dict of additional metadata key-value pairs
        skip_secret_scan: Skip secret detection (default False)

    Returns:
        Result dict with success, id, content_type, importance_score
    """
    # Validate content type
    if content_type not in VALID_CONTENT_TYPES:
        return {
            "success": False,
            "error": f"Invalid content_type '{content_type}'. "
                     f"Valid types: {', '.join(VALID_CONTENT_TYPES)}"
        }
    
    # Validate name requirement
    if content_type in ("pattern", "plan", "decision") and not name:
        return {
            "success": False,
            "error": f"content_type '{content_type}' requires a name parameter"
        }
    
    # Validate importance score
    if not 0.0 <= importance_score <= 1.0:
        return {
            "success": False,
            "error": f"importance_score must be between 0.0 and 1.0, got {importance_score}"
        }
    
    # Secret scan
    if not skip_secret_scan:
        detections = scan_for_secrets(content)
        if detections:
            return {
                "success": False,
                "error": "Secret detected in content",
                "detections": detections,
            }
    
    # Generate ID
    type_const, namespace, id_gen = _TYPE_MAP[content_type]
    
    if content_type == "observation":
        doc_id = id_gen()  # Auto-generates timestamp
    elif content_type == "pattern":
        doc_id = id_gen(name)
    elif content_type == "summary":
        doc_id = id_gen(session_id)  # Uses session_id if provided
    elif content_type == "code":
        # For code, name should be "file_path::symbol"
        if name and "::" in name:
            file_path, symbol = name.split("::", 1)
            doc_id = id_gen(file_path, symbol)
        else:
            doc_id = id_gen(name or "unknown", "__module__")
    elif content_type == "relationship":
        # For relationship, name should be "entity_a::entity_b"
        if name and "::" in name:
            entity_a, entity_b = name.split("::", 1)
            doc_id = id_gen(entity_a, entity_b)
        else:
            return {
                "success": False,
                "error": "relationship type requires name in format 'entity_a::entity_b'"
            }
    elif content_type == "hint":
        # For hint, name should be "topic::seq"
        if name and "::" in name:
            topic, seq_str = name.split("::", 1)
            doc_id = id_gen(topic, int(seq_str))
        else:
            doc_id = id_gen(name or "general", 0)
    elif content_type == "plan":
        doc_id = id_gen(name)
    elif content_type == "learning":
        doc_id = id_gen()  # Auto-generates timestamp
    elif content_type == "decision":
        doc_id = id_gen(name)
    else:
        return {"success": False, "error": f"Unknown content_type: {content_type}"}
    
    # Build metadata
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    metadata = {
        "type": type_const,
        "namespace": namespace,
        "tier": "chromadb",
        "promoted": "false",
        "retrieval_count": "0",
        "importance_score": str(importance_score),
        "source": source,
        "created_at": now_iso,
        "updated_at": now_iso,
    }
    
    if tags:
        metadata["tags"] = ",".join(tags)
    if session_id:
        metadata["session_id"] = session_id
    if name:
        metadata["name"] = name
    if extra_metadata:
        metadata.update(extra_metadata)
    
    # Write to ChromaDB
    try:
        collection = _get_collection()
        collection.upsert(
            ids=[doc_id],
            documents=[content],
            metadatas=[metadata]
        )
        return {
            "success": True,
            "id": doc_id,
            "content_type": content_type,
            "importance_score": importance_score,
        }
    except Exception as e:
        logger.error(f"tier2_write failed: {e}")
        return {"success": False, "error": str(e)}


def tier2_read(doc_id: str) -> dict:
    """Read Tier 2 content from ChromaDB and increment retrieval count.
    
    Args:
        doc_id: Document ID to read
    
    Returns:
        Result dict with success, found, id, content, metadata
    """
    try:
        collection = _get_collection()
        result = collection.get(ids=[doc_id])
        
        if not result["ids"]:
            return {
                "success": True,
                "found": False,
                "id": doc_id,
            }
        
        # Get current retrieval count and increment
        metadata = result["metadatas"][0]
        retrieval_count = float(metadata.get("retrieval_count", "0"))
        retrieval_count += 1
        
        # Update retrieval count and updated_at
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        updated_metadata = {**metadata}
        updated_metadata["retrieval_count"] = str(retrieval_count)
        updated_metadata["updated_at"] = now_iso
        
        # Write back
        collection.upsert(
            ids=[doc_id],
            documents=[result["documents"][0]],
            metadatas=[updated_metadata]
        )
        
        return {
            "success": True,
            "found": True,
            "id": doc_id,
            "content": result["documents"][0],
            "metadata": updated_metadata,
        }
    except Exception as e:
        logger.error(f"tier2_read failed: {e}")
        return {"success": False, "error": str(e)}


VALID_SORT_OPTIONS = (
    "importance_desc", "importance_asc",
    "created_at_desc", "created_at_asc",
    "none",
)


def tier2_list(
    content_type: Optional[str] = None,
    min_importance: Optional[float] = None,
    source: Optional[str] = None,
    limit: int = 20,
    sort_by: str = "importance_desc",
) -> dict:
    """List Tier 2 documents with optional filtering and sorting.

    Args:
        content_type: Filter by content type (observation, pattern, etc.)
        min_importance: Minimum importance score (0.0-1.0)
        source: Filter by source (e.g., "auto-extract")
        limit: Maximum number of results (default 20)
        sort_by: Sort order. One of: importance_desc (default),
                 importance_asc, created_at_desc, created_at_asc, none

    Returns:
        Result dict with success, documents, total
    """
    try:
        collection = _get_collection()
        
        # Build where clause for ChromaDB
        conditions = [{"tier": "chromadb"}]
        
        if content_type:
            if content_type not in VALID_CONTENT_TYPES:
                return {
                    "success": False,
                    "error": f"Invalid content_type '{content_type}'. "
                             f"Valid types: {', '.join(VALID_CONTENT_TYPES)}"
                }
            type_const, _, _ = _TYPE_MAP[content_type]
            conditions.append({"type": type_const})
        
        if source:
            conditions.append({"source": source})
        
        # Construct where clause
        if len(conditions) == 1:
            where = conditions[0]
        else:
            where = {"$and": conditions}
        
        # Get all matching documents (ChromaDB doesn't support numeric comparisons)
        result = collection.get(where=where)
        
        # Apply min_importance filter in Python
        docs = []
        for i, doc_id in enumerate(result["ids"]):
            metadata = result["metadatas"][i]
            
            # Apply importance filter
            if min_importance is not None:
                importance = float(metadata.get("importance_score", "0.5"))
                if importance < min_importance:
                    continue
            
            docs.append({
                "id": doc_id,
                "content": result["documents"][i],
                "metadata": metadata,
            })
        
        # Validate sort_by
        if sort_by not in VALID_SORT_OPTIONS:
            return {
                "success": False,
                "error": f"Invalid sort_by '{sort_by}'. "
                         f"Valid options: {', '.join(VALID_SORT_OPTIONS)}"
            }

        # Sort before applying limit
        if sort_by == "importance_desc":
            docs.sort(key=lambda d: float(d["metadata"].get("importance_score", "0.5")), reverse=True)
        elif sort_by == "importance_asc":
            docs.sort(key=lambda d: float(d["metadata"].get("importance_score", "0.5")))
        elif sort_by == "created_at_desc":
            docs.sort(key=lambda d: d["metadata"].get("created_at", ""), reverse=True)
        elif sort_by == "created_at_asc":
            docs.sort(key=lambda d: d["metadata"].get("created_at", ""))
        # "none" — no sorting

        # Apply limit
        limited = docs[:limit] if len(docs) > limit else docs

        return {
            "success": True,
            "documents": limited,
            "total": len(docs),
            "returned": len(limited),
        }
    except Exception as e:
        logger.error(f"tier2_list failed: {e}")
        return {"success": False, "error": str(e)}


def tier2_delete(doc_id: str) -> dict:
    """Delete Tier 2 content from ChromaDB.
    
    Args:
        doc_id: Document ID to delete
    
    Returns:
        Result dict with success, id, deleted
    """
    try:
        collection = _get_collection()
        
        # Check if exists
        result = collection.get(ids=[doc_id])
        if not result["ids"]:
            return {
                "success": True,
                "id": doc_id,
                "deleted": False,
                "reason": "not found",
            }
        
        # Delete
        collection.delete(ids=[doc_id])
        
        return {
            "success": True,
            "id": doc_id,
            "deleted": True,
        }
    except Exception as e:
        logger.error(f"tier2_delete failed: {e}")
        return {"success": False, "error": str(e)}


def tier2_upsert(doc_id: str, content: str, metadata: dict) -> dict:
    """Update existing tier2 document by ID (ChromaDB upsert).

    Unlike tier2_write which generates new IDs, this updates in-place.

    Args:
        doc_id: Existing document ID
        content: Updated content
        metadata: Updated metadata dict

    Returns:
        Result dict with success, doc_id, updated
    """
    try:
        collection = _get_collection()
        metadata["updated_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        collection.upsert(ids=[doc_id], documents=[content], metadatas=[metadata])
        return {"success": True, "doc_id": doc_id, "updated": True}
    except Exception as e:
        logger.error(f"tier2_upsert failed: {e}")
        return {"success": False, "error": str(e)}
