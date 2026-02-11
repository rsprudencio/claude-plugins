"""Tier 2 to Tier 1 promotion for important content.

Promotes ephemeral ChromaDB-only content to durable file-backed storage
when it meets importance/retrieval thresholds.
"""
import logging
import os
from datetime import datetime, timezone
from typing import Optional

from .config import get_promotion_config
from .file_ops import write_vault_file
from .memory import _get_collection
from .namespaces import vault_id, parse_id, get_tier, TIER_CHROMADB, TIER_FILE
from .paths import get_path

logger = logging.getLogger("jarvis-tools")


def check_promotion_criteria(metadata: dict) -> dict:
    """Check if Tier 2 content meets promotion criteria.
    
    Args:
        metadata: ChromaDB metadata dict
    
    Returns:
        Dict with should_promote (bool), reason (str), criteria_met (list)
    """
    config = get_promotion_config()
    
    # Extract values from metadata (stored as strings)
    importance_score = float(metadata.get("importance_score", "0.5"))
    retrieval_count = float(metadata.get("retrieval_count", "0"))
    created_at = metadata.get("created_at")
    
    criteria_met = []
    
    # Criterion 1: High importance score
    if importance_score >= config["importance_threshold"]:
        criteria_met.append(f"importance {importance_score:.2f} >= {config['importance_threshold']}")
    
    # Criterion 2: High retrieval count
    if retrieval_count > config["retrieval_count_threshold"]:
        criteria_met.append(f"retrievals {retrieval_count:.2f} > {config['retrieval_count_threshold']}")
    
    # Criterion 3: Age + importance combo
    if created_at:
        try:
            created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            days_old = (now - created).total_seconds() / 86400
            
            if (days_old >= config["age_importance_days"] and 
                importance_score >= config["age_importance_score"]):
                criteria_met.append(
                    f"age {days_old:.0f}d >= {config['age_importance_days']}d "
                    f"+ importance {importance_score:.2f} >= {config['age_importance_score']}"
                )
        except (ValueError, TypeError):
            pass
    
    should_promote = len(criteria_met) > 0
    reason = "; ".join(criteria_met) if criteria_met else "No criteria met"
    
    return {
        "should_promote": should_promote,
        "reason": reason,
        "criteria_met": criteria_met,
    }


def promote(doc_id: str) -> dict:
    """Promote Tier 2 content to Tier 1 (file-backed).
    
    Process:
    1. Read content + metadata from ChromaDB
    2. Verify it's Tier 2 and not already promoted
    3. Resolve promotion path based on content type
    4. Generate markdown with YAML frontmatter
    5. Write file via write_vault_file (vault boundary safety)
    6. Delete old ChromaDB entry
    7. Upsert new entry with vault:: ID and tier="file"
    
    Args:
        doc_id: Tier 2 document ID to promote
    
    Returns:
        Result dict with success, original_id, promoted_path, vault_id,
        file_written, chromadb_updated, needs_git_commit
    """
    try:
        collection = _get_collection()
        
        # Read existing content
        result = collection.get(ids=[doc_id])
        if not result["ids"]:
            return {
                "success": False,
                "error": f"Document not found: {doc_id}"
            }
        
        content = result["documents"][0]
        metadata = result["metadatas"][0]
        
        # Verify Tier 2
        tier = get_tier(doc_id)
        if tier != TIER_CHROMADB:
            return {
                "success": False,
                "error": f"Document {doc_id} is not Tier 2 (tier={tier})"
            }
        
        # Check if already promoted
        if metadata.get("promoted") == "true":
            return {
                "success": False,
                "error": f"Document {doc_id} is already promoted",
                "already_promoted": True,
            }
        
        # Parse ID to get content type
        parsed = parse_id(doc_id)
        content_type = metadata.get("type")
        
        # Resolve promotion path based on content type
        if content_type == "observation":
            path_name = "observations_promoted"
            filename_prefix = "observation"
        elif content_type == "pattern":
            path_name = "patterns_promoted"
            filename_prefix = "pattern"
        elif content_type == "summary":
            path_name = "summaries_promoted"
            filename_prefix = "summary"
        elif content_type == "learning":
            path_name = "learnings_promoted"
            filename_prefix = "learning"
        elif content_type == "decision":
            path_name = "decisions_promoted"
            filename_prefix = "decision"
        else:
            return {
                "success": False,
                "error": f"Content type '{content_type}' does not support promotion"
            }
        
        # Get promotion directory
        promotion_dir = get_path(path_name, ensure_exists=True)

        # Project-aware nesting: if observation has project_dir, nest under it
        project_dir_meta = metadata.get("project_dir", "")
        if project_dir_meta:
            promotion_dir = os.path.join(promotion_dir, project_dir_meta)
            os.makedirs(promotion_dir, exist_ok=True)

        # Generate filename
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        name_slug = metadata.get("name", "unnamed").lower().replace(" ", "-")
        filename = f"{filename_prefix}-{name_slug}-{timestamp}.md"

        # Build full path (relative to vault)
        from .config import get_verified_vault_path
        vault_path, error = get_verified_vault_path()
        if error:
            return {"success": False, "error": error}

        relative_path = os.path.relpath(os.path.join(promotion_dir, filename), vault_path)
        
        # Check if file already exists (idempotency)
        full_path = os.path.join(promotion_dir, filename)
        if os.path.exists(full_path):
            return {
                "success": True,
                "already_promoted": True,
                "promoted_path": relative_path,
                "reason": "File already exists",
            }
        
        # Build YAML frontmatter
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        tags = metadata.get("tags", "").split(",") if metadata.get("tags") else []
        tags_yaml = "\n".join(f"  - {tag.strip()}" for tag in tags if tag.strip())

        frontmatter = f"""---
type: {content_type}
importance: {metadata.get('importance_score', '0.5')}
original_id: {doc_id}
promoted_at: {now_iso}
source: {metadata.get('source', 'unknown')}
created_at: {metadata.get('created_at', now_iso)}
retrieval_count: {metadata.get('retrieval_count', '0')}"""

        # Add scope if present
        scope_meta = metadata.get("scope", "")
        if scope_meta:
            frontmatter += f"\nscope: {scope_meta}"

        # Add project if present
        if project_dir_meta:
            frontmatter += f"\nproject: {project_dir_meta}"

        # Add relevant files if present
        files_meta = metadata.get("relevant_files", "")
        if files_meta:
            files_list = [f.strip() for f in files_meta.split(",") if f.strip()]
            if files_list:
                files_yaml = "\n".join(f"  - {f}" for f in files_list)
                frontmatter += f"\nfiles:\n{files_yaml}"

        if tags_yaml:
            frontmatter += f"\ntags:\n{tags_yaml}"

        frontmatter += "\n---\n\n"
        
        # Combine frontmatter + content
        file_content = frontmatter + content
        
        # Write file via write_vault_file (reuse existing vault boundary safety)
        write_result = write_vault_file(relative_path, file_content)
        if not write_result["success"]:
            return {
                "success": False,
                "error": f"Failed to write file: {write_result.get('error')}"
            }
        
        # Delete old ChromaDB entry
        collection.delete(ids=[doc_id])
        
        # Create new vault:: ID
        new_vault_id = vault_id(relative_path)
        
        # Upsert with new ID and tier="file"
        new_metadata = {**metadata}
        new_metadata["tier"] = "file"
        new_metadata["promoted"] = "true"
        new_metadata["promoted_at"] = now_iso
        new_metadata["original_tier2_id"] = doc_id
        new_metadata["type"] = "vault"  # Universal type is now vault
        new_metadata["vault_type"] = content_type  # Vault-specific type
        new_metadata["namespace"] = "vault::"
        
        collection.upsert(
            ids=[new_vault_id],
            documents=[file_content],
            metadatas=[new_metadata]
        )
        
        return {
            "success": True,
            "original_id": doc_id,
            "promoted_path": relative_path,
            "vault_id": new_vault_id,
            "file_written": True,
            "chromadb_updated": True,
            "needs_git_commit": True,
        }
        
    except Exception as e:
        logger.error(f"promote failed: {e}")
        return {"success": False, "error": str(e)}
