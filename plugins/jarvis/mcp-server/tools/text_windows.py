"""Lossless canonical storage with bounded inference windows."""

from __future__ import annotations

from collections.abc import Callable


def _piece_bytes(piece) -> bytes:
    if isinstance(piece, str):
        return piece.encode("utf-8")
    if isinstance(piece, list) and all(
        isinstance(value, int) and 0 <= value <= 255 for value in piece
    ):
        return bytes(piece)
    raise ValueError(f"invalid tokenizer piece: {piece!r}")


def _fallback_byte_windows(
    text: str, max_tokens: int, overlap_tokens: int
) -> list[str]:
    """Conservative UTF-8 windows when the host tokenizer is unavailable.

    A byte-fallback tokenizer cannot emit more tokens than UTF-8 bytes, so a
    max_tokens-byte window is safe for any language. Invalid partial codepoints
    at boundaries are ignored; overlap guarantees they appear intact nearby.
    """
    raw = text.encode("utf-8")
    if len(raw) <= max_tokens:
        return [text]
    step = max_tokens - overlap_tokens
    windows = []
    for start in range(0, len(raw), step):
        value = raw[start : start + max_tokens].decode("utf-8", errors="ignore")
        if value:
            windows.append(value)
        if start + max_tokens >= len(raw):
            break
    return windows


def split_text_windows(
    text: str,
    *,
    max_tokens: int = 2048,
    overlap_tokens: int = 128,
    tokenize: Callable[..., list] | None = None,
) -> list[str]:
    """Split all text into bounded overlapping inference windows.

    The original string is returned unchanged whenever its UTF-8 byte count is
    already below the token ceiling. Canonical callers keep the original text;
    these windows exist only for embeddings and cross-encoder inference.
    """
    if not isinstance(text, str):
        raise TypeError("text must be a string")
    if max_tokens < 1:
        raise ValueError("max_tokens must be positive")
    if overlap_tokens < 0 or overlap_tokens >= max_tokens:
        raise ValueError("overlap_tokens must be between 0 and max_tokens - 1")
    if not text:
        return []
    if len(text.encode("utf-8")) <= max_tokens:
        return [text]
    if tokenize is None:
        return _fallback_byte_windows(text, max_tokens, overlap_tokens)

    try:
        tokens = tokenize(text, with_pieces=True)
        if len(tokens) <= max_tokens:
            return [text]
        pieces = [_piece_bytes(token["piece"]) for token in tokens]
    except Exception:
        return _fallback_byte_windows(text, max_tokens, overlap_tokens)

    step = max_tokens - overlap_tokens
    windows = []
    for start in range(0, len(pieces), step):
        raw = b"".join(pieces[start : start + max_tokens])
        value = raw.decode("utf-8", errors="ignore")
        if value:
            windows.append(value)
        if start + max_tokens >= len(pieces):
            break
    return windows
