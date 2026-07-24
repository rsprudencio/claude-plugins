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

Everything here is pure and deterministic.
"""

from __future__ import annotations

import os
from typing import Iterable, Optional, Union

# Cap the prefix so it can never dominate a small (min ~200 char) chunk.
DEFAULT_MAX_CHARS = 200


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


def build_chunk_context(
    path: str,
    title: Optional[str] = None,
    heading_trail: Optional[Union[str, Iterable[str]]] = None,
    *,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Return a compact ``Document: …`` prefix ending in a blank line.

    Format::

        Document: <path> — <title> › <heading> › <sub-heading>\\n\\n

    The ``— <title>`` clause is omitted when no title is available (or it would
    merely repeat the path); the ``› <heading>`` clauses are omitted when there
    is no heading trail. The whole prefix (excluding the trailing blank line) is
    capped at ``max_chars`` so it can't overwhelm a small chunk. Returns ``""``
    when there is nothing to anchor to (no path and no title).

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
    return body + "\n\n"


def augment_chunk_for_model(
    document: str,
    *,
    path: str,
    title: Optional[str] = None,
    heading_trail: Optional[Union[str, Iterable[str]]] = None,
    is_chunk: bool,
    enabled: bool = True,
    max_chars: int = DEFAULT_MAX_CHARS,
) -> str:
    """Return the text to embed/rerank for a chunk (single choke point).

    Prepends a document-context prefix to ``document`` iff augmentation is
    ``enabled`` AND the row is a genuine fragment (``is_chunk``). Whole-document
    rows (which already begin with their own title) and the disabled config path
    return ``document`` unchanged — byte-identical to the stored column. Callers
    pass this text to the embedder/reranker while persisting/displaying the raw
    ``document`` separately.
    """
    document = document or ""
    if not enabled or not is_chunk:
        return document
    prefix = build_chunk_context(path, title, heading_trail, max_chars=max_chars)
    if not prefix:
        return document
    return prefix + document
