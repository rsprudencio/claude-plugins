"""Vault memory indexing for ChromaDB semantic search.

Provides bulk and incremental indexing of vault .md files into ChromaDB.
ChromaDB auto-embeds content using sentence-transformers (all-MiniLM-L6-v2).

All documents are stored in the unified 'jarvis' collection with namespaced
IDs (vault:: prefix) and enriched metadata.
"""
import glob
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import chromadb

from .config import get_verified_vault_path, get_chunking_config, get_scoring_config
from .chunking import chunk_document
from .scoring import compute_importance
from .namespaces import vault_id, NAMESPACE_VAULT, ContentType
from .paths import get_path, get_relative_path, is_sensitive_path, SENSITIVE_PATHS
from .format_support import (
    detect_format, is_indexable, parse_frontmatter, extract_title, INDEXABLE_EXTENSIONS,
)

logger = logging.getLogger("jarvis-core")

# Singleton client
_chroma_client = None
_COLLECTION_NAME = "jarvis"
_BATCH_SIZE = 50
# Directories to skip during indexing (non-content directories)
_SKIP_DIRS = {"templates", ".obsidian", ".git", ".trash"}


def _get_client() -> chromadb.ClientAPI:
    """Get or create singleton ChromaDB PersistentClient."""
    global _chroma_client
    if _chroma_client is None:
        db_dir = get_path("db_path", ensure_exists=True)
        _chroma_client = chromadb.PersistentClient(path=db_dir)
    return _chroma_client


def _get_collection() -> chromadb.Collection:
    """Get or create the unified jarvis collection."""
    client = _get_client()
    return client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"}
    )


def _parse_frontmatter_for_file(content: str, filename: str) -> dict:
    """Extract frontmatter/properties from content, detecting format from filename."""
    fmt = detect_format(filename)
    return parse_frontmatter(content, fmt)


def _extract_title_for_file(content: str, filename: str) -> str:
    """Get title from content, detecting format from filename."""
    fmt = detect_format(filename)
    return extract_title(content, filename, fmt)


def _build_metadata(frontmatter: dict, relative_path: str) -> dict:
    """Build ChromaDB metadata dict with universal + vault-specific fields.

    Universal fields: type, namespace, created_at, updated_at, source
    Vault-specific: directory, vault_type, title, tags, importance, has_frontmatter
    """
    directory = relative_path.split('/')[0] if '/' in relative_path else ''
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Universal fields
    meta = {
        "type": ContentType.VAULT,
        "namespace": NAMESPACE_VAULT,
        "tier": "file",
        "source": "vault-index",
        "created_at": frontmatter.get("created", now_iso),
        "updated_at": frontmatter.get("modified", now_iso),
        # Vault-specific
        "directory": directory,
        "has_frontmatter": "true" if frontmatter else "false",
        "chunk_index": 0,
        "chunk_total": 1,
    }

    # Vault type: from frontmatter 'type' or inferred from directory
    vault_type = frontmatter.get("type")
    if not vault_type:
        type_map = {
            'journal': 'journal', 'notes': 'note', 'work': 'work', 'inbox': 'inbox',
            '.jarvis': 'strategic',
        }
        vault_type = type_map.get(directory, directory or 'document')
    meta["vault_type"] = vault_type

    # Optional fields from frontmatter
    for key in ('tags', 'sentiment'):
        if key in frontmatter:
            meta[key] = str(frontmatter[key])
    if 'importance' in frontmatter:
        meta['importance'] = str(frontmatter['importance'])
    else:
        meta['importance'] = 'medium'

    return meta


def _should_skip(relative_path: str, include_sensitive: bool) -> bool:
    """Check if a file should be skipped during indexing."""
    parts = Path(relative_path).parts
    if not parts:
        return True
    top_dir = parts[0]
    if top_dir in _SKIP_DIRS:
        return True
    if not include_sensitive:
        # Check against configurable sensitive path names
        sensitive_dirs = {get_relative_path(name) for name in SENSITIVE_PATHS}
        if top_dir in sensitive_dirs:
            return True
    return False


def _delete_existing_chunks(collection, relative_path: str) -> int:
    """Delete all existing chunks for a file before re-indexing.

    Handles both chunked docs (parent_file metadata) and legacy
    single-doc format (vault::{path} ID).

    Returns number of deleted documents.
    """
    deleted = 0
    # Delete chunks by parent_file metadata
    try:
        result = collection.get(
            where={"parent_file": relative_path},
            include=[]
        )
        if result["ids"]:
            collection.delete(ids=result["ids"])
            deleted += len(result["ids"])
    except Exception:
        pass

    # Also delete legacy single-doc ID if it exists
    legacy_id = vault_id(relative_path)
    try:
        result = collection.get(ids=[legacy_id], include=[])
        if result["ids"]:
            collection.delete(ids=[legacy_id])
            deleted += 1
    except Exception:
        pass

    return deleted


def _index_single_file(collection, content: str, frontmatter: dict,
                        relative_path: str, title: str,
                        chunking_config: dict, scoring_config: dict) -> tuple:
    """Index a single file with chunking and scoring.

    Returns (chunk_ids, chunk_docs, chunk_metas, chunk_count).
    """
    metadata = _build_metadata(frontmatter, relative_path)
    metadata['title'] = title
    metadata['parent_file'] = relative_path

    # Chunk the document (format-aware)
    fmt = detect_format(relative_path)
    chunk_result = chunk_document(content, chunking_config, fmt=fmt)

    # Shared scoring inputs (file-level)
    scoring_cfg = scoring_config if scoring_config.get("enabled", True) else {"type_weights": {"unknown": 0.5}, "concept_patterns": {}}
    vault_type = metadata.get("vault_type", "unknown")
    fm_importance = frontmatter.get("importance")
    created_at = metadata.get("created_at")

    ids = []
    docs = []
    metas = []

    for chunk in chunk_result.chunks:
        # Score each chunk on its own content (concept patterns match per-chunk)
        importance_score = compute_importance(
            content=chunk.content,
            vault_type=vault_type,
            frontmatter_importance=fm_importance,
            created_at=created_at,
            config=scoring_cfg,
        )

        chunk_meta = {**metadata}
        chunk_meta['importance_score'] = round(importance_score, 4)
        chunk_meta['chunk_index'] = chunk.index
        chunk_meta['chunk_total'] = chunk_result.total
        chunk_meta['chunk_heading'] = chunk.heading

        if chunk_result.was_chunked:
            doc_id = vault_id(relative_path, chunk=chunk.index)
        else:
            doc_id = vault_id(relative_path)

        ids.append(doc_id)
        docs.append(chunk.content)
        metas.append(chunk_meta)

    return ids, docs, metas, chunk_result.total


def index_vault(force: bool = False, directory: Optional[str] = None,
                include_sensitive: bool = False) -> dict:
    """Bulk index all .md files in the vault into ChromaDB.

    Args:
        force: Re-index all files, even already indexed
        directory: Only index files in this subdirectory
        include_sensitive: Include documents/ and people/ directories

    Returns:
        Summary dict with counts and timing
    """
    vault_path, error = get_verified_vault_path()
    if error:
        return {"success": False, "error": error}

    start = time.time()
    collection = _get_collection()

    # Determine search path
    search_path = os.path.join(vault_path, directory) if directory else vault_path
    if not os.path.isdir(search_path):
        return {"success": False, "error": f"Directory not found: {search_path}"}

    chunking_config = get_chunking_config()
    scoring_config = get_scoring_config()

    # Get existing parent_files to skip (unless force)
    existing_files = set()
    if not force:
        try:
            result = collection.get(include=["metadatas"])
            for i, doc_id in enumerate(result['ids']):
                meta = result['metadatas'][i] if result.get('metadatas') else {}
                parent = (meta or {}).get('parent_file')
                if parent:
                    existing_files.add(parent)
                else:
                    # Legacy unchunked: extract path from vault::path ID
                    if doc_id.startswith("vault::") and "#chunk-" not in doc_id:
                        existing_files.add(doc_id[7:])
        except Exception:
            pass

    # Collect indexable files (all supported formats)
    indexable_files = []
    for ext in INDEXABLE_EXTENSIONS:
        indexable_files.extend(
            glob.glob(os.path.join(search_path, '**', f'*{ext}'), recursive=True)
        )

    files_indexed = 0
    chunks_total = 0
    skipped = 0
    errors = []
    batch_ids = []
    batch_docs = []
    batch_meta = []

    for filepath in indexable_files:
        relative = os.path.relpath(filepath, vault_path)

        if _should_skip(relative, include_sensitive):
            skipped += 1
            continue

        if relative in existing_files and not force:
            skipped += 1
            continue

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()

            if not content.strip():
                skipped += 1
                continue

            # On force re-index, clean up old chunks first
            if force:
                _delete_existing_chunks(collection, relative)

            frontmatter = _parse_frontmatter_for_file(content, filepath)
            title = _extract_title_for_file(content, os.path.basename(filepath))

            ids, docs, metas, n_chunks = _index_single_file(
                collection, content, frontmatter, relative, title,
                chunking_config, scoring_config
            )

            batch_ids.extend(ids)
            batch_docs.extend(docs)
            batch_meta.extend(metas)
            files_indexed += 1
            chunks_total += n_chunks

            # Flush batch
            if len(batch_ids) >= _BATCH_SIZE:
                collection.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_meta)
                batch_ids, batch_docs, batch_meta = [], [], []

        except Exception as e:
            errors.append({"file": relative, "error": str(e)})

    # Flush remaining
    if batch_ids:
        collection.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_meta)

    duration = round(time.time() - start, 2)
    return {
        "success": True,
        "files_indexed": files_indexed,
        "chunks_total": chunks_total,
        "files_skipped": skipped,
        "errors": errors,
        "duration_seconds": duration,
        "collection_total": collection.count()
    }


def index_file(relative_path: str) -> dict:
    """Index a single file into ChromaDB with chunking and scoring.

    Args:
        relative_path: Path relative to vault root

    Returns:
        Summary dict with success status, chunks count, and metadata
    """
    vault_path, error = get_verified_vault_path()
    if error:
        return {"success": False, "error": error}

    filepath = os.path.join(vault_path, relative_path)
    if not os.path.isfile(filepath):
        return {"success": False, "error": f"File not found: {relative_path}"}

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()

        collection = _get_collection()
        chunking_config = get_chunking_config()
        scoring_config = get_scoring_config()

        # Clean up old chunks/legacy doc before re-indexing
        _delete_existing_chunks(collection, relative_path)

        frontmatter = _parse_frontmatter_for_file(content, relative_path)
        title = _extract_title_for_file(content, relative_path)

        ids, docs, metas, n_chunks = _index_single_file(
            collection, content, frontmatter, relative_path, title,
            chunking_config, scoring_config
        )

        collection.upsert(ids=ids, documents=docs, metadatas=metas)

        return {
            "success": True,
            "id": ids[0] if len(ids) == 1 else ids,
            "title": title,
            "chunks": n_chunks,
            "metadata": metas[0] if metas else {}
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def unindex_file(relative_path: str) -> dict:
    """Remove a file's chunks from ChromaDB.

    Called when a vault file is deleted to keep the index in sync.

    Args:
        relative_path: Path relative to vault root

    Returns:
        Summary dict with success status and number of chunks removed.
    """
    try:
        collection = _get_collection()
        deleted = _delete_existing_chunks(collection, relative_path)
        return {"success": True, "deleted_chunks": deleted}
    except Exception as e:
        return {"success": False, "error": str(e)}
