"""Vault memory indexing for ChromaDB semantic search.

Provides bulk and incremental indexing of vault .md files into ChromaDB.
ChromaDB auto-embeds content using sentence-transformers (all-MiniLM-L6-v2).
"""
import glob
import os
import re
import time
from pathlib import Path
from typing import Optional

import chromadb

from .config import get_verified_vault_path

# Singleton client
_chroma_client = None
_COLLECTION_NAME = "vault"
_BATCH_SIZE = 50
_DB_DIR = os.path.join(str(Path.home()), ".jarvis", "memory_db")

# Directories to skip by default
_SKIP_DIRS = {"templates", ".obsidian", ".git", ".trash"}
_SENSITIVE_DIRS = {"documents", "people"}


def _get_client() -> chromadb.ClientAPI:
    """Get or create singleton ChromaDB PersistentClient."""
    global _chroma_client
    if _chroma_client is None:
        os.makedirs(_DB_DIR, exist_ok=True)
        _chroma_client = chromadb.PersistentClient(path=_DB_DIR)
    return _chroma_client


def _get_collection() -> chromadb.Collection:
    """Get or create the vault collection."""
    client = _get_client()
    return client.get_or_create_collection(
        name=_COLLECTION_NAME,
        metadata={"hnsw:space": "cosine"}
    )


def _parse_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from markdown content."""
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if not match:
        return {}
    fm = {}
    for line in match.group(1).split('\n'):
        if ':' in line and not line.strip().startswith('-'):
            key, _, value = line.partition(':')
            fm[key.strip()] = value.strip().strip('"').strip("'")
    # Extract list-style tags
    tag_match = re.search(r'tags:\s*\n((?:\s+-\s+.*\n)*)', match.group(1) + '\n')
    if tag_match:
        tags = re.findall(r'-\s+(.+)', tag_match.group(1))
        fm['tags'] = ','.join(t.strip().strip('"').strip("'") for t in tags)
    return fm


def _extract_title(content: str, filename: str) -> str:
    """Get title from first H1 heading or filename."""
    match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return Path(filename).stem.replace('-', ' ').title()


def _build_metadata(frontmatter: dict, relative_path: str) -> dict:
    """Build ChromaDB metadata dict from frontmatter and path."""
    directory = relative_path.split('/')[0] if '/' in relative_path else ''
    meta = {
        "directory": directory,
        "has_frontmatter": "true" if frontmatter else "false",
    }
    # Map frontmatter fields (ChromaDB only accepts str, int, float, bool)
    for key in ('type', 'tags', 'created', 'modified', 'importance', 'sentiment'):
        if key in frontmatter:
            meta[key] = str(frontmatter[key])
    # Defaults
    if 'type' not in meta:
        # Infer from directory
        type_map = {'journal': 'journal', 'notes': 'note', 'work': 'work', 'inbox': 'inbox'}
        meta['type'] = type_map.get(directory, 'unknown')
    if 'importance' not in meta:
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
    if not include_sensitive and top_dir in _SENSITIVE_DIRS:
        return True
    return False


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

    # Get existing IDs to skip (unless force)
    existing_ids = set()
    if not force:
        try:
            result = collection.get()
            existing_ids = set(result['ids'])
        except Exception:
            pass

    # Collect files
    md_files = glob.glob(os.path.join(search_path, '**', '*.md'), recursive=True)

    indexed = 0
    skipped = 0
    errors = []
    batch_ids = []
    batch_docs = []
    batch_meta = []

    for filepath in md_files:
        relative = os.path.relpath(filepath, vault_path)

        if _should_skip(relative, include_sensitive):
            skipped += 1
            continue

        if relative in existing_ids and not force:
            skipped += 1
            continue

        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                content = f.read()

            if not content.strip():
                skipped += 1
                continue

            frontmatter = _parse_frontmatter(content)
            title = _extract_title(content, os.path.basename(filepath))
            metadata = _build_metadata(frontmatter, relative)
            metadata['title'] = title

            batch_ids.append(relative)
            batch_docs.append(content)
            batch_meta.append(metadata)

            # Flush batch
            if len(batch_ids) >= _BATCH_SIZE:
                collection.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_meta)
                indexed += len(batch_ids)
                batch_ids, batch_docs, batch_meta = [], [], []

        except Exception as e:
            errors.append({"file": relative, "error": str(e)})

    # Flush remaining
    if batch_ids:
        collection.upsert(ids=batch_ids, documents=batch_docs, metadatas=batch_meta)
        indexed += len(batch_ids)

    duration = round(time.time() - start, 2)
    return {
        "success": True,
        "files_indexed": indexed,
        "files_skipped": skipped,
        "errors": errors,
        "duration_seconds": duration,
        "collection_total": collection.count()
    }


def index_file(relative_path: str) -> dict:
    """Index a single file into ChromaDB.

    Args:
        relative_path: Path relative to vault root

    Returns:
        Summary dict with success status and metadata
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

        frontmatter = _parse_frontmatter(content)
        title = _extract_title(content, os.path.basename(filepath))
        metadata = _build_metadata(frontmatter, relative_path)
        metadata['title'] = title

        collection = _get_collection()
        collection.upsert(ids=[relative_path], documents=[content], metadatas=[metadata])

        return {
            "success": True,
            "id": relative_path,
            "title": title,
            "metadata": metadata
        }
    except Exception as e:
        return {"success": False, "error": str(e)}
