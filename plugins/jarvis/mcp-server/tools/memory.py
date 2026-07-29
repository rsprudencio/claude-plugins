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
    get_contextual_augmentation_mode,
)
from .chunking import chunk_document
from .chunk_context import augment_vault_row
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


def _upsert_batch(ids: list, docs: list, metas: list, summaries: Optional[dict] = None) -> list:
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
    # begin with their own title. In 'summary' mode the file's cached LLM
    # sentence rides along (resolved by the caller, one lookup per batch); a
    # file without one degrades to the mechanical prefix.
    # See tools/chunk_context.py — the SAME entry point every other site uses.
    mode = get_contextual_augmentation_mode()
    summaries = summaries or {}
    embed_inputs = [
        augment_vault_row(
            doc,
            parent_file=meta.get("parent_file", ""),
            title=meta.get("title", ""),
            chunk_heading=meta.get("chunk_heading", ""),
            chunk_total=meta.get("chunk_total", 1),
            mode=mode,
            summary=summaries.get(meta.get("parent_file", "")),
        )
        for doc, meta in zip(docs, metas)
    ]
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


def _build_summary_request(relative_path: str, content: str, title: str):
    """Build one file's summary CACHE-LOOKUP key, or None when not applicable.

    Despite the name inherited from the generation era, this builds a lookup
    key, not a work item: it carries the file's ``content_hash`` so the index
    path can tell a valid cached summary from a stale one. Nothing here calls an
    LLM.

    Returns None when summary mode is off (so no work and no import cost is
    incurred on the mechanical path) or when the inputs can't be assembled.
    """
    try:
        from .config import get_contextual_embeddings_enabled, get_contextual_summaries_config
        from .context_summary import build_summary_request

        # Deliberately NOT gated on summary mode. In mechanical/none mode the
        # chunks are embedded without a summary, so any cache row for this file
        # is stale the moment the content changes — and the readers are
        # hash-blind by design. Building the request anyway lets
        # resolve_indexed_summaries DELETE those rows, which is what keeps the
        # documented rollback (contextual_summaries.enabled=false) from arming
        # the cache to serve stale sentences after the next flip back on.
        # Only a fully disabled augmentation ('none') can skip the work: then
        # no site consults the cache at all.
        if not get_contextual_embeddings_enabled():
            return None
        return build_summary_request(
            relative_path,
            content,
            title=title,
            fmt=detect_format(relative_path),
            config=get_contextual_summaries_config(),
        )
    except Exception as exc:
        logger.warning(
            "Could not prepare contextual summary input for %s: %s",
            relative_path, exc,
        )
        return None


def _lookup_batch_summaries(requests: list):
    """Resolve a batch's cached summaries. CACHE ONLY — never generates.

    Generation used to happen right here, inline in the indexing and vault-write
    paths. That single choice is what made the feature unshippable: it could
    never succeed in the container, the per-run spend cap was applied per
    10-chunk flush, configured concurrency was scoped to a flush, and every
    ``jarvis_store`` write blocked the MCP event loop on an untimed LLM call
    (~30 min worst case). Generation now lives ONLY in
    ``bin/generate_summaries.py``.

    Also drops cache rows that no longer describe the content being indexed —
    see ``context_summary.resolve_indexed_summaries``. Never raises.

    Returns an ``IndexSummaryResolution`` (``.summaries``, ``.requested``).
    """
    from .context_summary import IndexSummaryResolution, resolve_indexed_summaries

    if not requests:
        return IndexSummaryResolution()
    try:
        return resolve_indexed_summaries(None, requests)
    except Exception as exc:
        logger.warning("Contextual summary lookup failed for batch: %s", exc)
        return IndexSummaryResolution(requested=len(requests))


def _flush_batch(
    batch_ids: list, batch_docs: list, batch_meta: list, errors: list,
    summaries: Optional[dict] = None,
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
        row_failures = _upsert_batch(batch_ids, batch_docs, batch_meta, summaries)
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


def resolve_achieved_augmentation(
    chunked_files: int, files_with_summary: int, mode: Optional[str] = None
) -> str:
    """The augmentation state this run ACTUALLY produced, not the configured one.

    Recording the config mode was a lie with teeth: a force reindex with no LLM
    reachable embedded everything mechanically, stamped 'summary', and thereby
    silenced the only warning that would ever have prompted another reindex. The
    operator saw ``success: true`` and unchanged retrieval quality forever.

    * configured 'none'/'mechanical' → itself (no summary can be involved).
    * configured 'summary' →
        - ``summary`` when EVERY chunked file was embedded with its summary
          (vacuously true when a run had no chunked files at all — there is no
          mechanically-augmented vault vector to be inconsistent with),
        - ``mechanical`` when none were,
        - ``partial-summary`` otherwise: a genuinely mixed space.
    """
    from .chunk_context import (
        MODE_MECHANICAL, MODE_PARTIAL_SUMMARY, MODE_SUMMARY,
    )

    mode = mode or get_contextual_augmentation_mode()
    if mode != MODE_SUMMARY:
        return mode
    if files_with_summary >= chunked_files:
        return MODE_SUMMARY
    if files_with_summary <= 0:
        return MODE_MECHANICAL
    return MODE_PARTIAL_SUMMARY


def _count_chunked_files() -> int:
    """Chunked vault files in the whole store (the coverage denominator).

    A force reindex only sees the files it did not skip, so the recorded
    embedding-space coverage has to be measured against the store instead.
    Returns 0 on any failure (callers then fall back to run-local counters).
    """
    try:
        row = execute_query(
            "SELECT count(DISTINCT parent_file) AS cnt FROM obsidian.documents "
            "WHERE chunk_total > 1 AND parent_file IS NOT NULL",
            fetch="one",
        )
        return int((row or {}).get("cnt", 0) or 0)
    except Exception as exc:
        logger.warning("Could not count chunked vault files: %s", exc)
        return 0


def _record_contextual_meta(
    chunked_files: int = 0, files_with_summary: int = 0
) -> str:
    """Stamp the ACHIEVED augmentation state into the embedding-space identity.

    Records 'none' | 'mechanical' | 'summary' | 'partial-summary' plus the
    coverage counts behind the verdict, so ``check_model_consistency`` can tell
    a real summary space from a mechanical one wearing its label.

    Best-effort: only updates an existing local.meta record (first-run
    recording belongs to check_model_consistency). Returns the state it computed
    (even if the write failed) so the caller can report it.
    """
    achieved = resolve_achieved_augmentation(chunked_files, files_with_summary)
    try:
        from .schema import get_meta, set_meta

        stored = get_meta("embedding_config")
        if stored is None:
            return achieved
        stored["contextual_chunks"] = achieved
        stored["contextual_coverage"] = {
            "chunked_files": int(chunked_files),
            "files_with_summary": int(files_with_summary),
        }
        set_meta("embedding_config", stored)
    except Exception as exc:
        logger.warning("Could not record contextual_chunks in local.meta: %s", exc)
    return achieved


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
    # One cache-lookup key per chunked FILE in the current batch
    # (see _lookup_batch_summaries — cache reads only, never generation).
    batch_summary_requests = []
    # Coverage: how many chunked files COULD carry a summary vs how many
    # actually were embedded with one. Drives the recorded embedding-space
    # identity, so it must count files, not chunks.
    summary_candidates = 0
    summaries_used = 0

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
            # Only genuinely chunked files can carry a prefix, so only they need
            # a summary — no LLM spend on whole-document rows.
            if n_chunks > 1:
                request = _build_summary_request(relative, content, title)
                if request is not None:
                    batch_summary_requests.append(request)
            files_indexed += 1
            chunks_total += n_chunks

            # Flush batch — always clear even on error to prevent
            # cascade: a failed upsert must not cause an ever-growing
            # batch that re-encodes all prior items on each retry. Per-row
            # isolation lives in _upsert_batch; here we keep the counters
            # honest by backing out any files whose rows failed.
            if len(batch_ids) >= _BATCH_SIZE:
                resolution = _lookup_batch_summaries(batch_summary_requests)
                summary_candidates += resolution.requested
                summaries_used += resolution.resolved
                files_failed, chunks_failed = _flush_batch(
                    batch_ids, batch_docs, batch_meta, errors,
                    resolution.summaries,
                )
                files_indexed -= files_failed
                chunks_total -= chunks_failed
                batch_ids, batch_docs, batch_meta = [], [], []
                batch_summary_requests = []

            # Progress every 50 files
            if files_indexed % 50 == 0:
                logger.info(
                    "index_vault: %d/%d files, %d chunks",
                    file_num, len(indexable_files), chunks_total,
                )

        except Exception as e:
            errors.append({"file": relative, "error": str(e)})

    # Flush remaining
    resolution = _lookup_batch_summaries(batch_summary_requests)
    summary_candidates += resolution.requested
    summaries_used += resolution.resolved
    files_failed, chunks_failed = _flush_batch(
        batch_ids, batch_docs, batch_meta, errors, resolution.summaries,
    )
    files_indexed -= files_failed
    chunks_total -= chunks_failed

    # Get total count from obsidian.documents
    count_result = execute_query(
        "SELECT count(*) AS cnt FROM obsidian.documents", fetch="one"
    )
    total = count_result["cnt"] if count_result else 0

    duration = round(time.time() - start, 2)

    # A clean FULL force run re-embedded every vault chunk — record what it
    # ACTUALLY produced in the embedding-space identity. Recording the config
    # mode here is what let a summary-less reindex silence the consistency
    # check forever.
    contextual_mode = get_contextual_augmentation_mode()
    achieved = resolve_achieved_augmentation(
        summary_candidates, summaries_used, contextual_mode
    )
    if force and not directory and not errors:
        # Coverage must be measured against the WHOLE store, not just the files
        # this run touched. index_vault skips sensitive dirs (default) and
        # secret-bearing files BEFORE the force-delete, so their previous-era
        # rows survive; scoring the verdict on the run's own counters stamped a
        # mixed space with a clean 'summary' label and no warning.
        store_chunked = _count_chunked_files()
        untouched = max(0, store_chunked - summary_candidates)
        if untouched:
            logger.warning(
                "Force reindex left %d chunked vault file(s) untouched "
                "(sensitive directories and secret-flagged files are skipped by "
                "default); their vectors keep whatever augmentation they were "
                "built with. Recording partial coverage — re-run with "
                "include_sensitive=true to cover them.",
                untouched,
            )
        achieved = _record_contextual_meta(
            max(store_chunked, summary_candidates), summaries_used
        )

    result = {
        "success": True,
        "files_indexed": files_indexed,
        "chunks_total": chunks_total,
        "files_skipped": skipped,
        "errors": errors,
        "duration_seconds": duration,
        "collection_total": total,
        # Honest augmentation reporting for THIS run's files: `success: true`
        # used to be indistinguishable between "every chunk carries its summary"
        # and "zero summaries exist and nothing will ever tell you". On a full
        # force run this equals the recorded whole-vault state; on a partial or
        # incremental run it describes only the files just indexed (and
        # local.meta is deliberately left alone).
        "contextual_augmentation": achieved,
        "summary_candidates": summary_candidates,
        "summaries_used": summaries_used,
    }
    if contextual_mode == "summary" and summaries_used < summary_candidates:
        result["summaries_missing"] = summary_candidates - summaries_used
        result["summary_hint"] = (
            "Summary mode is configured but "
            f"{summary_candidates - summaries_used} of {summary_candidates} "
            "chunked files had no cached summary, so they were embedded with the "
            "mechanical prefix. Run bin/generate_summaries.py, then "
            "jarvis_index_vault(force=true)."
        )
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
        # CACHE LOOKUP ONLY. Every vault write reaches here (store.py auto-index),
        # on the MCP event loop, so an LLM call here froze /health, every other
        # tool, and the background loops for the duration of the request.
        summaries = {}
        if n_chunks > 1:
            request = _build_summary_request(relative_path, content, title)
            summaries = _lookup_batch_summaries(
                [request] if request else []
            ).summaries
        failures = _upsert_batch(ids, docs, metas, summaries)
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
