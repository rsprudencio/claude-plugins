"""Prepare canonical local documents for bounded semantic indexing."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass

from .text_windows import split_text_windows


def _positive_int(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


DOCUMENT_WINDOW_TOKENS = _positive_int("JARVIS_DOCUMENT_WINDOW_TOKENS", 2048)
DOCUMENT_WINDOW_OVERLAP = _positive_int("JARVIS_DOCUMENT_WINDOW_OVERLAP", 128)


@dataclass(frozen=True)
class PreparedDocument:
    canonical_embedding: list[float]
    windows: list[str]
    window_embeddings: list[list[float]]

    @property
    def is_chunked(self) -> bool:
        return len(self.windows) > 1


def _normalized_mean(vectors: list[list[float]]) -> list[float]:
    if not vectors:
        raise ValueError("at least one vector is required")
    dimensions = len(vectors[0])
    if not dimensions or any(len(vector) != dimensions for vector in vectors):
        raise ValueError("embedding dimensions must be non-empty and consistent")
    mean = [sum(vector[index] for vector in vectors) / len(vectors)
            for index in range(dimensions)]
    norm = math.sqrt(sum(value * value for value in mean))
    if not math.isfinite(norm) or norm <= 0:
        raise ValueError("mean embedding is not normalizable")
    return [value / norm for value in mean]


def prepare_document(content: str, service, *, batch_size: int = 8) -> PreparedDocument:
    """Embed every part of canonical content without sending an oversized input."""
    tokenizer = getattr(service, "tokenize", None)
    overlap = min(DOCUMENT_WINDOW_OVERLAP, DOCUMENT_WINDOW_TOKENS - 1)
    windows = split_text_windows(
        content,
        max_tokens=DOCUMENT_WINDOW_TOKENS,
        overlap_tokens=overlap,
        tokenize=tokenizer,
    )
    if not windows:
        windows = [""]
    if len(windows) == 1:
        vector = service.encode(windows[0])
        return PreparedDocument(vector, windows, [vector])
    vectors = service.encode_batch(windows, batch_size=batch_size)
    if len(vectors) != len(windows):
        raise ValueError(
            f"embedding service returned {len(vectors)} vectors for {len(windows)} windows"
        )
    return PreparedDocument(_normalized_mean(vectors), windows, vectors)


def replace_local_chunks(cursor, parent_id: str, prepared: PreparedDocument) -> None:
    """Atomically replace search-only chunks for a canonical local memory."""
    cursor.execute("DELETE FROM local.memory_chunks WHERE parent_id = %s", (parent_id,))
    if not prepared.is_chunked:
        return
    total = len(prepared.windows)
    cursor.executemany(
        """INSERT INTO local.memory_chunks
           (parent_id, chunk_index, chunk_total, document, embedding)
           VALUES (%s, %s, %s, %s, %s::halfvec)""",
        [
            (parent_id, index, total, window, embedding)
            for index, (window, embedding) in enumerate(
                zip(prepared.windows, prepared.window_embeddings)
            )
        ],
    )
