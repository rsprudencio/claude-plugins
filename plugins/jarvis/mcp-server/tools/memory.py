"""Vault memory indexing for pgvector semantic search.

Provides bulk and incremental indexing of vault .md files into PostgreSQL
with pgvector. Embeddings are generated explicitly via EmbeddingService
(granite-embedding-small-english-r2, 384d, ONNX backend).

All documents are stored in the obsidian.documents table with proper columns
for vault-specific fields (parent_file, directory, vault_type, etc.).
Remaining flexible metadata goes into a JSONB column.
"""

import gc
import glob
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .config import (
    get_verified_vault_path,
    get_chunking_config,
    get_scoring_config,
    get_memory_config,
    get_contextual_embeddings_enabled,
)
from .chunking import chunk_document
from .chunk_context import augment_chunk_for_model
from .scoring import compute_importance
from .secret_scan import scan_for_secrets
from .namespaces import vault_id
from .paths import get_path, get_relative_path, is_sensitive_path, SENSITIVE_PATHS
from .format_support import (
    detect_format,
    is_indexable,
    parse_frontmatter,
    extract_title,
    INDEXABLE_EXTENSIONS,
)
from .schema import execute_query, metadata_to_jsonb

logger = logging.getLogger("jarvis-core")

_BATCH_SIZE = 10

# Directories to skip during indexing (non-content directories)
_SKIP_DIRS = {"templates", ".obsidian", ".git", ".trash", ".serena"}

# Fields that are proper columns in obsidian.documents (not JSONB)
_VAULT_COLUMNS = frozenset({
    "parent_file", "directory", "vault_type", "title",
    "chunk_index", "chunk_total", "chunk_heading", "importance_score",
})


def _sanitize_timestamp(value, default: str) -> str:
    """Convert frontmatter timestamps to ISO 8601 strings.

    Obsidian stores timestamps as Unix milliseconds (e.g. 1695032292344).
    PostgreSQL TIMESTAMPTZ rejects raw integers. Convert to ISO format.
    Also strips Obsidian's non-standard timezone suffixes like "(UTC +00:00)".
    """
    if value is None:
        return default
    if isinstance(value, (int, float)):
        # Unix milliseconds → seconds
        ts = value / 1000 if value > 1e12 else value
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%SZ"
            )
        except (ValueError, OSError, OverflowError):
            return default
    if isinstance(value, str) and value.isdigit():
        return _sanitize_timestamp(int(value), default)
    if isinstance(value, str) and value:
        # Strip Obsidian's non-standard "(UTC ...)" / "(GMT ...)" timezone
        # suffixes, which PostgreSQL's ::timestamptz cast rejects. Handles
        # "(UTC +00:00)", "(UTC +1)", "(UTC)", "(GMT -05:00)", etc. A real
        # offset is preserved (normalized to ±HH:MM); a bare zone or zero
        # offset becomes "Z". End-anchored so legitimate trailing
        # parentheticals elsewhere in the value are left untouched.
        m = re.search(
            r"\s*\((?:UTC|GMT)\s*([+\-]?\d{1,2})?(?::?(\d{2}))?\s*\)\s*$",
            value,
        )
        if m:
            base = value[: m.start()].rstrip()
            if not base:
                # Value was nothing but a timezone suffix → not a real
                # timestamp; fall back to the default rather than emit a
                # bare offset string.
                return default
            hours, minutes = m.group(1), m.group(2)
            if hours is None:
                offset = "Z"
            else:
                sign = "-" if hours.startswith("-") else "+"
                hh = abs(int(hours))
                mm = minutes or "00"
                offset = "Z" if (hh == 0 and mm == "00") else f"{sign}{hh:02d}:{mm}"
            return f"{base}{offset}"
        return value.strip()
    return default


def _parse_frontmatter_for_file(content: str, filename: str) -> dict:
    """Extract frontmatter/properties from content, detecting format from filename."""
    fmt = detect_format(filename)
    return parse_frontmatter(content, fmt)


def _extract_title_for_file(content: str, filename: str) -> str:
    """Get title from content, detecting format from filename."""
    fmt = detect_format(filename)
    return extract_title(content, filename, fmt)


def _build_metadata(frontmatter: dict, relative_path: str) -> dict:
    """Build metadata dict for a vault document.

    Returns a flat dict where keys matching _VAULT_COLUMNS will be extracted
    to proper SQL columns by _upsert_batch(). Remaining keys go to JSONB.

    Column fields: parent_file, directory, vault_type, title,
                   chunk_index, chunk_total, chunk_heading, importance_score
    JSONB fields: tags, sentiment, has_frontmatter, user, etc.
    """
    directory = relative_path.split("/")[0] if "/" in relative_path else ""
    now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    meta = {
        # Column fields (extracted by _upsert_batch)
        "directory": directory,
        "chunk_index": 0,
        "chunk_total": 1,
        "chunk_heading": "",
        # JSONB fields
        "source": "vault-index",
        "created_at": _sanitize_timestamp(frontmatter.get("created"), now_iso),
        "updated_at": _sanitize_timestamp(frontmatter.get("modified"), now_iso),
        "has_frontmatter": "true" if frontmatter else "false",
    }

    # Vault type: from frontmatter 'type' or inferred from directory
    vault_type = frontmatter.get("type")
    if not vault_type:
        type_map = {
            "journal": "journal",
            "notes": "note",
            "work": "work",
            "inbox": "inbox",
            ".jarvis": "strategic",
        }
        vault_type = type_map.get(directory, directory or "document")
    meta["vault_type"] = vault_type

    # Optional fields from frontmatter (→ JSONB)
    for key in ("tags", "sentiment"):
        if key in frontmatter:
            meta[key] = str(frontmatter[key])

    # Importance score (→ column)
    if "importance" in frontmatter:
        _CATEGORICAL_MAP = {
            "critical": 0.95,
            "high": 0.8,
            "medium": 0.5,
            "low": 0.3,
        }
        raw = frontmatter["importance"]
        if isinstance(raw, str) and raw.lower() in _CATEGORICAL_MAP:
            meta["importance_score"] = _CATEGORICAL_MAP[raw.lower()]
        else:
            try:
                meta["importance_score"] = max(0.0, min(1.0, float(raw)))
            except (ValueError, TypeError):
                meta["importance_score"] = 0.5
    else:
        meta["importance_score"] = 0.5

    # Multi-user attribution (→ JSONB)
    from jarvis_common.auth import get_current_user

    user = get_current_user()
    if user != "anonymous":
        meta["user"] = user

    return meta


def _should_skip(relative_path: str, include_sensitive: bool) -> bool:
    """Check if a file should be skipped during indexing.

    DAR F17: Excludes .jarvis/strategic/ directory to prevent duplicate
    retrieval with memory_crud dual-write entries.
    """
    parts = Path(relative_path).parts
    if not parts:
        return True
    top_dir = parts[0]
    if top_dir in _SKIP_DIRS:
        return True

    # DAR F17: Skip strategic memory directory (indexed via memory_crud)
    if top_dir == ".jarvis" and len(parts) > 1 and parts[1] == "strategic":
        return True

    if not include_sensitive:
        # Check against configurable sensitive path names
        sensitive_dirs = {get_relative_path(name) for name in SENSITIVE_PATHS}
        if top_dir in sensitive_dirs:
            return True
    return False


def _delete_existing_chunks(relative_path: str) -> int:
    """Delete all existing chunks for a file before re-indexing.

    Deletes from obsidian.documents using the parent_file column
    and legacy single-doc vault::path IDs.

    Returns number of deleted documents.
    """
    from .schema import _get_pool

    pool = _get_pool()
    deleted = 0

    with pool.connection() as conn:
        with conn.cursor() as cur:
            # Delete chunks by parent_file column
            cur.execute(
                "DELETE FROM obsidian.documents WHERE parent_file = %s",
                (relative_path,),
            )
            deleted += cur.rowcount

            # Also delete legacy single-doc ID if it exists
            legacy_id = vault_id(relative_path)
            cur.execute("DELETE FROM obsidian.documents WHERE id = %s", (legacy_id,))
            deleted += cur.rowcount

            conn.commit()

    return deleted


def _index_single_file(
    content: str,
    frontmatter: dict,
    relative_path: str,
    title: str,
    chunking_config: dict,
    scoring_config: dict,
) -> tuple:
    """Index a single file with chunking and scoring.

    Returns (chunk_ids, chunk_docs, chunk_metas, chunk_count).
    Does NOT write to database — caller is responsible for batching.
    """
    metadata = _build_metadata(frontmatter, relative_path)
    metadata["title"] = title
    metadata["parent_file"] = relative_path

    # Chunk the document (format-aware)
    fmt = detect_format(relative_path)
    chunk_result = chunk_document(content, chunking_config, fmt=fmt)

    # Shared scoring inputs (file-level)
    scoring_cfg = (
        scoring_config
        if scoring_config.get("enabled", True)
        else {"type_weights": {"unknown": 0.5}, "concept_patterns": {}}
    )
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
        chunk_meta["importance_score"] = round(importance_score, 4)
        chunk_meta["chunk_index"] = chunk.index
        chunk_meta["chunk_total"] = chunk_result.total
        chunk_meta["chunk_heading"] = chunk.heading

        if chunk_result.was_chunked:
            doc_id = vault_id(relative_path, chunk=chunk.index)
        else:
            doc_id = vault_id(relative_path)

        ids.append(doc_id)
        docs.append(chunk.content)
        metas.append(chunk_meta)

    return ids, docs, metas, chunk_result.total


def _split_columns_metadata(meta: dict) -> tuple:
    """Split a flat metadata dict into (columns_dict, jsonb_dict).

    Column fields go into obsidian.documents columns directly.
    Everything else goes into the JSONB metadata column.
    """
    columns = {}
    jsonb = {}
    for key, value in meta.items():
        if key in _VAULT_COLUMNS:
            columns[key] = value
        else:
            jsonb[key] = value
    return columns, jsonb


def _upsert_batch(ids: list, docs: list, metas: list) -> list:
    """Embed and upsert a batch of documents into obsidian.documents.

    Each row is inserted inside its own transaction/savepoint
    (``conn.transaction()``), so a single bad row — e.g. an unparseable
    frontmatter timestamp that fails the ``::timestamptz`` cast — is rolled
    back in isolation without poisoning its batch siblings. Previously the
    whole batch shared one transaction, so one bad row silently dropped up to
    ``_BATCH_SIZE - 1`` other files.

    Returns a list of per-row failures::

        [{"id": ..., "parent_file": ..., "error": ...}, ...]

    An empty list means every row committed.
    """
    if not ids:
        return []

    from .embedding import get_embedding_service
    from .schema import _get_pool

    service = get_embedding_service()

    # Contextual augmentation: embed a compact document-context prefix ALONGSIDE
    # each fragment so the bi-encoder sees the chunk's document identity, while
    # the stored `document` column (inserted below) stays byte-identical. Only
    # genuine chunks (chunk_total > 1) get a prefix; whole-document rows already
    # begin with their own title. See tools/chunk_context.py.
    contextual_enabled = get_contextual_embeddings_enabled()
    embed_inputs = []
    for doc, meta in zip(docs, metas):
        try:
            chunk_total = int(meta.get("chunk_total", 1) or 1)
        except (ValueError, TypeError):
            chunk_total = 1
        embed_inputs.append(
            augment_chunk_for_model(
                doc,
                path=meta.get("parent_file", ""),
                title=meta.get("title", ""),
                heading_trail=meta.get("chunk_heading", ""),
                is_chunk=chunk_total > 1,
                enabled=contextual_enabled,
            )
        )
    embeddings = service.encode_batch(embed_inputs)

    failures: list = []
    pool = _get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            for doc_id, doc, meta, emb in zip(ids, docs, metas, embeddings):
                columns, jsonb = _split_columns_metadata(meta)
                try:
                    # Per-row transaction: commits this row on success, rolls
                    # back only this row on failure (leaving the connection
                    # usable for the next row).
                    with conn.transaction():
                        cur.execute(
                            """INSERT INTO obsidian.documents
                       (id, document, embedding,
                        parent_file, directory, vault_type, title,
                        chunk_index, chunk_total, chunk_heading,
                        importance_score, metadata,
                        created_at, updated_at)
                       VALUES (%s, %s, %s::halfvec,
                               %s, %s, %s, %s,
                               %s, %s, %s,
                               %s, %s::jsonb,
                               COALESCE(%s::timestamptz, now()),
                               COALESCE(%s::timestamptz, now()))
                       ON CONFLICT (id) DO UPDATE SET
                           document = EXCLUDED.document,
                           embedding = EXCLUDED.embedding,
                           parent_file = EXCLUDED.parent_file,
                           directory = EXCLUDED.directory,
                           vault_type = EXCLUDED.vault_type,
                           title = EXCLUDED.title,
                           chunk_index = EXCLUDED.chunk_index,
                           chunk_total = EXCLUDED.chunk_total,
                           chunk_heading = EXCLUDED.chunk_heading,
                           importance_score = EXCLUDED.importance_score,
                           metadata = EXCLUDED.metadata,
                           updated_at = EXCLUDED.updated_at""",
                            (
                                doc_id,
                                doc,
                                emb,
                                columns.get("parent_file", ""),
                                columns.get("directory", ""),
                                columns.get("vault_type", "document"),
                                columns.get("title", ""),
                                columns.get("chunk_index", 0),
                                columns.get("chunk_total", 1),
                                columns.get("chunk_heading", ""),
                                columns.get("importance_score", 0.5),
                                metadata_to_jsonb(jsonb),
                                meta.get("created_at"),
                                meta.get("updated_at"),
                            ),
                        )
                except Exception as row_err:
                    failures.append(
                        {
                            "id": doc_id,
                            "parent_file": columns.get("parent_file", ""),
                            "error": str(row_err),
                        }
                    )

    del embeddings
    gc.collect()
    return failures


def _flush_batch(
    batch_ids: list, batch_docs: list, batch_meta: list, errors: list
) -> tuple:
    """Upsert a batch with per-row isolation and account failures honestly.

    Appends one error per failed file to ``errors`` (using the real file path,
    not a misattributed sibling) and returns ``(files_failed, chunks_failed)``
    so the caller can keep its counters truthful. Never raises: a catastrophic
    failure (embedding/connection level) is attributed to every file in the
    batch rather than silently swallowed.

    Because rows commit independently, a multi-chunk file with one bad chunk
    could otherwise leave its other chunks committed — a half-indexed file that
    a later non-force run would skip (its ``parent_file`` is now in the DB).
    To preserve the "a file is fully indexed or cleanly absent" invariant, any
    partially-committed chunks of a failed file are deleted, and ``chunks_failed``
    counts ALL of that file's chunks in the batch (so ``chunks_total`` matches
    what actually remains).
    """
    if not batch_ids:
        return 0, 0
    try:
        row_failures = _upsert_batch(batch_ids, batch_docs, batch_meta)
    except Exception as batch_err:
        logger.error("Batch upsert failed: %s", batch_err)
        row_failures = [
            {
                "id": doc_id,
                "parent_file": meta.get("parent_file", ""),
                "error": str(batch_err),
            }
            for doc_id, meta in zip(batch_ids, batch_meta)
        ]
    if not row_failures:
        return 0, 0
    # One representative error per distinct failed file.
    failed_files: dict = {}
    for f in row_failures:
        failed_files.setdefault(f.get("parent_file", ""), f.get("error", ""))
    for parent_file, err in failed_files.items():
        errors.append({"file": parent_file, "error": err})
        # Remove any sibling chunks that committed before this file's bad
        # chunk failed, so the file is cleanly absent rather than half-indexed.
        if parent_file:
            try:
                _delete_existing_chunks(parent_file)
            except Exception as cleanup_err:
                logger.error(
                    "Failed to clean up partial index for %s: %s",
                    parent_file,
                    cleanup_err,
                )
    # Count every chunk belonging to a failed file (committed-then-deleted plus
    # failed rows), not just the rows that raised — so chunks_total stays honest.
    chunks_failed = sum(
        1 for meta in batch_meta if meta.get("parent_file", "") in failed_files
    )
    return len(failed_files), chunks_failed


def _record_contextual_meta() -> None:
    """Stamp the current augmentation flag into the embedding-space identity.

    Best-effort: only updates an existing local.meta record (first-run
    recording belongs to check_model_consistency).
    """
    try:
        from .schema import get_meta, set_meta

        stored = get_meta("embedding_config")
        if stored is None:
            return
        stored["contextual_chunks"] = bool(get_contextual_embeddings_enabled())
        set_meta("embedding_config", stored)
    except Exception as exc:
        logger.warning("Could not record contextual_chunks in local.meta: %s", exc)


def index_vault(
    force: bool = False,
    directory: Optional[str] = None,
    include_sensitive: bool = False,
) -> dict:
    """Bulk index all .md files in the vault into PostgreSQL.

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
            rows = execute_query(
                "SELECT id, parent_file FROM obsidian.documents"
            )
            for row in rows:
                parent = row.get("parent_file")
                if parent:
                    existing_files.add(parent)
                else:
                    doc_id = row["id"]
                    # Legacy unchunked: extract path from vault::path ID
                    if doc_id.startswith("vault::") and "#chunk-" not in doc_id:
                        existing_files.add(doc_id[7:])
        except Exception:
            pass

    # Collect indexable files (all supported formats)
    indexable_files = []
    for ext in INDEXABLE_EXTENSIONS:
        indexable_files.extend(
            glob.glob(os.path.join(search_path, "**", f"*{ext}"), recursive=True, include_hidden=True)
        )

    files_indexed = 0
    chunks_total = 0
    skipped = 0
    errors = []
    batch_ids = []
    batch_docs = []
    batch_meta = []

    secret_detection_enabled = get_memory_config().get("secret_detection", True)
    secrets_skipped = 0

    logger.info("index_vault: %d files to process (force=%s)", len(indexable_files), force)

    for file_num, filepath in enumerate(indexable_files, 1):
        relative = os.path.relpath(filepath, vault_path)

        if _should_skip(relative, include_sensitive):
            skipped += 1
            continue

        if relative in existing_files and not force:
            skipped += 1
            continue

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            if not content.strip():
                skipped += 1
                continue

            # Secret scan — skip file if secrets detected
            if secret_detection_enabled:
                detections = scan_for_secrets(content)
                if detections:
                    types = [d["type"] for d in detections]
                    logger.warning(
                        "Secret detected in %s, skipping indexing: %s",
                        relative, types,
                    )
                    secrets_skipped += 1
                    skipped += 1
                    continue

            # On force re-index, clean up old chunks first
            if force:
                _delete_existing_chunks(relative)

            frontmatter = _parse_frontmatter_for_file(content, filepath)
            title = _extract_title_for_file(content, os.path.basename(filepath))

            ids, docs, metas, n_chunks = _index_single_file(
                content,
                frontmatter,
                relative,
                title,
                chunking_config,
                scoring_config,
            )

            batch_ids.extend(ids)
            batch_docs.extend(docs)
            batch_meta.extend(metas)
            files_indexed += 1
            chunks_total += n_chunks

            # Flush batch — always clear even on error to prevent
            # cascade: a failed upsert must not cause an ever-growing
            # batch that re-encodes all prior items on each retry. Per-row
            # isolation lives in _upsert_batch; here we keep the counters
            # honest by backing out any files whose rows failed.
            if len(batch_ids) >= _BATCH_SIZE:
                files_failed, chunks_failed = _flush_batch(
                    batch_ids, batch_docs, batch_meta, errors
                )
                files_indexed -= files_failed
                chunks_total -= chunks_failed
                batch_ids, batch_docs, batch_meta = [], [], []

            # Progress every 50 files
            if files_indexed % 50 == 0:
                logger.info(
                    "index_vault: %d/%d files, %d chunks",
                    file_num, len(indexable_files), chunks_total,
                )

        except Exception as e:
            errors.append({"file": relative, "error": str(e)})

    # Flush remaining
    files_failed, chunks_failed = _flush_batch(
        batch_ids, batch_docs, batch_meta, errors
    )
    files_indexed -= files_failed
    chunks_total -= chunks_failed

    # Get total count from obsidian.documents
    count_result = execute_query(
        "SELECT count(*) AS cnt FROM obsidian.documents", fetch="one"
    )
    total = count_result["cnt"] if count_result else 0

    duration = round(time.time() - start, 2)

    # A clean FULL force run re-embedded every vault chunk under the current
    # augmentation flag — record that in the embedding-space identity so the
    # startup consistency check stops warning about a mixed space.
    if force and not directory and not errors:
        _record_contextual_meta()

    result = {
        "success": True,
        "files_indexed": files_indexed,
        "chunks_total": chunks_total,
        "files_skipped": skipped,
        "errors": errors,
        "duration_seconds": duration,
        "collection_total": total,
    }
    if secrets_skipped:
        result["secrets_skipped"] = secrets_skipped
    return result


def index_file(relative_path: str) -> dict:
    """Index a single file into PostgreSQL with chunking and scoring.

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
        with open(filepath, "r", encoding="utf-8") as f:
            content = f.read()

        # Secret scan — skip entire file if secrets detected
        if get_memory_config().get("secret_detection", True):
            detections = scan_for_secrets(content)
            if detections:
                types = [d["type"] for d in detections]
                logger.warning(
                    "Secret detected in %s, skipping indexing: %s",
                    relative_path, types,
                )
                return {
                    "success": False,
                    "error": "SECRET_DETECTED",
                    "message": f"File contains potential secrets ({', '.join(types)}), skipping indexing.",
                    "detections": detections,
                }

        chunking_config = get_chunking_config()
        scoring_config = get_scoring_config()

        frontmatter = _parse_frontmatter_for_file(content, relative_path)
        title = _extract_title_for_file(content, relative_path)

        ids, docs, metas, n_chunks = _index_single_file(
            content,
            frontmatter,
            relative_path,
            title,
            chunking_config,
            scoring_config,
        )

        # Delete old chunks + upsert new. _upsert_batch isolates per-row
        # failures and returns them rather than raising, so a single-file
        # index must surface those explicitly instead of reporting success.
        _delete_existing_chunks(relative_path)
        failures = _upsert_batch(ids, docs, metas)
        if failures:
            return {
                "success": False,
                "error": failures[0].get("error", "upsert failed"),
            }

        return {
            "success": True,
            "id": ids[0] if len(ids) == 1 else ids,
            "title": title,
            "chunks": n_chunks,
            "metadata": metas[0] if metas else {},
        }
    except Exception as e:
        return {"success": False, "error": str(e)}


def unindex_file(relative_path: str) -> dict:
    """Remove a file's chunks from PostgreSQL.

    Called when a vault file is deleted to keep the index in sync.

    Args:
        relative_path: Path relative to vault root

    Returns:
        Summary dict with success status and number of chunks removed.
    """
    try:
        deleted = _delete_existing_chunks(relative_path)
        return {"success": True, "deleted_chunks": deleted}
    except Exception as e:
        return {"success": False, "error": str(e)}
