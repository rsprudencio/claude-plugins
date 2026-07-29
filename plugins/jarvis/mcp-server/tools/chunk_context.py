"""Contextual chunk augmentation for retrieval.

A document is split into ~200-900 char fragments before embedding/reranking.
A bare fragment loses its document identity — the mandate note's body chunk
says nothing about being "Igor's vulnerability management mandate", so both the
bi-encoder and the BGE cross-encoder fail on identity-referencing queries.

This module builds a compact ``Document: …`` prefix that is prepended to a
chunk's text ONLY at embedding and reranking time. The stored canonical
``document`` column is never touched — UI, injection budgets, and telemetry all
read the stored (raw) text, so the prefix must stay out of it. The exact same
prefix must be reconstructed everywhere a chunk is embedded or reranked (vault
indexing, both query rerank paths, and the shadow scorer) or live and shadow
scores diverge.

Two augmentation modes exist, and they are DIFFERENT embedding spaces:

``mechanical``
    Path/title/heading only. Measured on the live BGE reranker, the flagship
    relational query scored −5.1 against the mandate note's best chunk — below
    the −4.0 gate, so nothing injected.
``summary``
    The mechanical line PLUS one LLM-written situating sentence naming the
    relational frame ("… vulnerability management mandate from Igor (my
    manager)"). The same measurement scored −0.8. The summary is generated once
    per FILE and cached in ``obsidian.document_context``
    (see tools/context_summary.py); this module only formats it.

``augment_vault_row`` is the single entry point every vault augmentation site
calls, so all four sites (index embed, both query rerank paths, shadow scorer,
reindexer) produce byte-identical text by construction rather than by four
copies of the same keyword plumbing. That is also why the summary-line cap is
resolved from config INSIDE ``augment_vault_row`` (``summary_max_chars=None``)
rather than at the four call sites: a per-site keyword would be four chances to
diverge, and four chances to forget — which is exactly how the configured
``max_chars`` came to be ignored in favour of a hardcoded default.

The prefix builders (``build_chunk_context`` / ``augment_chunk_for_model``) are
pure and deterministic. ``augment_vault_row`` is the one config-aware wrapper.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional, Union

# Cap the mechanical prefix line so it can never dominate a small (min ~200
# char) chunk. The summary line carries its own, separate cap.
DEFAULT_MAX_CHARS = 200
DEFAULT_SUMMARY_MAX_CHARS = 200

# Augmentation modes = embedding-space identities. Recorded in
# local.meta.embedding_config['contextual_chunks'] and stamped into retrieval
# telemetry so shadow scoring can refuse to rescore across a mode change.
MODE_NONE = "none"
MODE_MECHANICAL = "mechanical"
MODE_SUMMARY = "summary"
AUGMENTATION_MODES = (MODE_NONE, MODE_MECHANICAL, MODE_SUMMARY)

# A RECORDED-ONLY state: config can never resolve to it, but an indexing run
# CAN produce it — some chunked files were embedded with their LLM summary and
# some (no cached summary yet, or generation never ran) with the mechanical
# prefix only. That is a genuinely mixed embedding space, and recording the
# configured 'summary' for it is the lie that made a summary-less reindex look
# like a success. It is deliberately absent from AUGMENTATION_MODES so it can
# never be mistaken for a live mode by the augmentation or shadow-guard paths.
MODE_PARTIAL_SUMMARY = "partial-summary"
RECORDED_AUGMENTATION_STATES = AUGMENTATION_MODES + (MODE_PARTIAL_SUMMARY,)


def normalize_augmentation_mode(value) -> str:
    """Coerce any recorded/stamped augmentation marker to a canonical LIVE mode.

    Accepts the three canonical strings and MIGRATES the legacy boolean form
    that pre-3.6 databases and telemetry events recorded:
    ``True`` → ``"mechanical"`` (all pre-3.6 augmentation was mechanical),
    ``False`` → ``"none"``. Anything unrecognized (including ``None`` and the
    recorded-only ``"partial-summary"``) reads as ``"none"``, which is the
    conservative answer: an unknown marker must never be mistaken for a match
    against a real mode.
    """
    if isinstance(value, bool):
        return MODE_MECHANICAL if value else MODE_NONE
    if isinstance(value, str):
        candidate = value.strip().lower()
        if candidate in AUGMENTATION_MODES:
            return candidate
    return MODE_NONE


def normalize_recorded_augmentation(value) -> str:
    """Like ``normalize_augmentation_mode`` but also accepts 'partial-summary'.

    Used ONLY where a *recorded* embedding-space identity is read back (
    ``local.meta.embedding_config['contextual_chunks']``), because that is the
    one place the mixed state can legitimately appear. Keeping it out of the
    live normalizer means a partial marker can never silently satisfy an
    augmentation-identity comparison.
    """
    if isinstance(value, str) and value.strip().lower() == MODE_PARTIAL_SUMMARY:
        return MODE_PARTIAL_SUMMARY
    return normalize_augmentation_mode(value)


def resolve_summary_max_chars() -> int:
    """Configured cap for the summary line, or the shipped default.

    ``memory.chunking.contextual_summaries.max_chars`` bounds BOTH generation
    (the prompt's budget and ``clean_summary``) and the prefix line — otherwise
    raising it spends tokens on text that every augmentation site then truncates
    away. Fail-soft: any config problem yields the shipped default rather than
    breaking augmentation.
    """
    try:
        from .config import get_contextual_summaries_config

        value = int(get_contextual_summaries_config().get(
            "max_chars", DEFAULT_SUMMARY_MAX_CHARS
        ) or 0)
    except Exception:
        return DEFAULT_SUMMARY_MAX_CHARS
    return value if value > 0 else DEFAULT_SUMMARY_MAX_CHARS


def _derive_title_from_path(path: str) -> str:
    """Best-effort human title from a file path when no title is supplied.

    Uses the basename stem verbatim (deterministic, no lossy humanizing). This
    is only a fallback — the indexing and rerank sites pass the real extracted
    title, so this rarely fires.
    """
    base = os.path.basename(path or "").strip()
    if not base:
        return ""
    return base.rsplit(".", 1)[0] if "." in base else base


def _normalize_trail(
    heading_trail: Optional[Union[str, Iterable[str]]],
) -> list[str]:
    """Coerce a heading trail (str, list, or None) into a clean list."""
    if heading_trail is None:
        return []
    if isinstance(heading_trail, str):
        heading_trail = [heading_trail]
    trail = []
    for item in heading_trail:
        text = str(item).strip()
        if text:
            trail.append(text)
    return trail


def _normalize_summary(summary: Optional[str], max_chars: int) -> str:
    """Collapse a generated summary to one capped line (never multi-line).

    A multi-line summary would break the ``…heading\\n<summary>\\n\\n`` prefix
    shape and make the prefix unbounded, so whitespace is collapsed here as a
    second line of defense behind the generator's own single-line enforcement.
    """
    if not summary:
        return ""
    line = " ".join(str(summary).split())
    if not line:
        return ""
    if max_chars > 0 and len(line) > max_chars:
        line = line[: max(0, max_chars - 1)].rstrip() + "…"
    return line


def build_chunk_context(
    path: str,
    title: Optional[str] = None,
    heading_trail: Optional[Union[str, Iterable[str]]] = None,
    *,
    summary: Optional[str] = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
) -> str:
    """Return a compact ``Document: …`` prefix ending in a blank line.

    Format (summary line present only when a summary is supplied)::

        Document: <path> — <title> › <heading> › <sub-heading>\\n
        <one-sentence situating summary>\\n\\n

    The ``— <title>`` clause is omitted when no title is available (or it would
    merely repeat the path); the ``› <heading>`` clauses are omitted when there
    is no heading trail. The mechanical line is capped at ``max_chars`` and the
    summary line at ``summary_max_chars``, so the whole prefix stays bounded and
    can't overwhelm a small chunk. Returns ``""`` when there is nothing to
    anchor to (no path and no title) — a summary alone is never emitted, since
    without an anchor there is no document identity to situate.

    With ``summary=None`` the output is byte-identical to the pre-summary
    (mechanical) format.

    Pure and deterministic — the same inputs always produce the same prefix, so
    every embed/rerank site can reconstruct byte-identical text.
    """
    path = (path or "").strip()
    trail = _normalize_trail(heading_trail)

    title = (title or "").strip()
    if not title and path:
        title = _derive_title_from_path(path)

    if not path and not title:
        return ""

    body = f"Document: {path}" if path else "Document:"
    if title and title != path:
        body += f" — {title}"
    if trail:
        body += " › " + " › ".join(trail)

    # Collapse incidental whitespace/newlines so the prefix is a single line.
    body = " ".join(body.split())
    if len(body) > max_chars:
        body = body[: max(0, max_chars - 1)].rstrip() + "…"

    summary_line = _normalize_summary(summary, summary_max_chars)
    if summary_line:
        body += "\n" + summary_line
    return body + "\n\n"


def augment_chunk_for_model(
    document: str,
    *,
    path: str,
    title: Optional[str] = None,
    heading_trail: Optional[Union[str, Iterable[str]]] = None,
    is_chunk: bool,
    enabled: bool = True,
    summary: Optional[str] = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS,
) -> str:
    """Return the text to embed/rerank for a chunk (single choke point).

    Prepends a document-context prefix to ``document`` iff augmentation is
    ``enabled`` AND the row is a genuine fragment (``is_chunk``). Whole-document
    rows (which already begin with their own title) and the disabled config path
    return ``document`` unchanged — byte-identical to the stored column. Callers
    pass this text to the embedder/reranker while persisting/displaying the raw
    ``document`` separately.

    ``summary`` adds the LLM-written situating line; omitting it yields exactly
    the mechanical prefix. Prefer ``augment_vault_row`` at call sites — it takes
    raw column values and cannot be called with mismatched keywords.
    """
    document = document or ""
    if not enabled or not is_chunk:
        return document
    prefix = build_chunk_context(
        path,
        title,
        heading_trail,
        summary=summary,
        max_chars=max_chars,
        summary_max_chars=summary_max_chars,
    )
    if not prefix:
        return document
    return prefix + document


def augment_vault_row(
    document: str,
    *,
    parent_file: str,
    title: Optional[str] = None,
    chunk_heading: Optional[str] = None,
    chunk_total=1,
    mode: str = MODE_SUMMARY,
    summary: Optional[str] = None,
    max_chars: int = DEFAULT_MAX_CHARS,
    summary_max_chars: Optional[int] = None,
) -> str:
    """Augment ONE obsidian.documents row — the shared four-site entry point.

    Takes the raw column values every site already has (``document``,
    ``parent_file``, ``title``, ``chunk_heading``, ``chunk_total``) plus the
    resolved augmentation ``mode`` and the file's cached ``summary``. Coercing
    ``chunk_total``, resolving the configured summary cap, and mapping mode →
    behavior HERE (rather than in four call sites) is what makes index embed,
    both query rerank paths, the shadow scorer, and the reindexer byte-identical
    by construction:

    * ``mode == "none"``      → stored text, unchanged.
    * ``mode == "mechanical"``→ path/title/heading prefix; ``summary`` ignored.
    * ``mode == "summary"``   → mechanical prefix + summary line when a summary
      is available; falls back to the mechanical prefix when it is not (per-file
      degradation, e.g. no cached summary for that one document).

    ``summary_max_chars=None`` (the default every site uses) reads the cap from
    ``memory.chunking.contextual_summaries.max_chars``; pass an explicit int
    only in tests that want to pin it.
    """
    mode = normalize_augmentation_mode(mode)
    if mode == MODE_NONE:
        return document or ""
    if summary_max_chars is None:
        summary_max_chars = resolve_summary_max_chars()
    try:
        total = int(chunk_total or 1)
    except (ValueError, TypeError):
        total = 1
    return augment_chunk_for_model(
        document,
        path=parent_file or "",
        title=title or "",
        heading_trail=chunk_heading or "",
        is_chunk=total > 1,
        enabled=True,
        summary=summary if mode == MODE_SUMMARY else None,
        max_chars=max_chars,
        summary_max_chars=summary_max_chars,
    )
