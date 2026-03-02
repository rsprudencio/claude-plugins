"""Embedding service for Jarvis v3.0 — explicit vector generation.

Replaces ChromaDB's auto-embed with an explicit pipeline using
granite-embedding-english-r2 (768d, 8192 tokens, ModernBERT architecture).
Stored as halfvec (16-bit) for zero storage overhead vs 384d float32.

The service uses ONNX Runtime by default for 2-5x faster CPU inference,
with automatic fallback to PyTorch if ONNX is unavailable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)

# Singleton state
_service: EmbeddingService | None = None
_service_cache_key: tuple | None = None


class EmbeddingService:
    """Encode text into dense vector embeddings.

    Wraps sentence-transformers with ONNX backend for fast CPU inference.
    Designed as a lazy singleton via get_embedding_service().

    Args:
        model_name: HuggingFace model identifier.
        dimensions: Expected embedding dimensions (validated on first encode).
        device: Torch device string ("cpu", "cuda", etc.).
        backend: "onnx" (preferred) or "torch".
    """

    def __init__(
        self,
        model_name: str,
        dimensions: int,
        device: str = "cpu",
        backend: str = "onnx",
    ) -> None:
        self._model_name = model_name
        self._dimensions = dimensions
        self._device = device
        self._backend = backend
        self._model: SentenceTransformer | None = None

    def _load_model(self) -> SentenceTransformer:
        """Lazy-load the sentence-transformers model."""
        if self._model is not None:
            return self._model

        from sentence_transformers import SentenceTransformer

        kwargs: dict = {
            "model_name_or_path": self._model_name,
            "device": self._device,
        }

        if self._backend == "onnx":
            try:
                kwargs["backend"] = "onnx"
                self._model = SentenceTransformer(**kwargs)
                logger.info(
                    "Loaded %s with ONNX backend on %s",
                    self._model_name,
                    self._device,
                )
                return self._model
            except Exception:
                logger.warning(
                    "ONNX backend unavailable for %s, falling back to PyTorch",
                    self._model_name,
                )
                kwargs.pop("backend", None)

        self._model = SentenceTransformer(**kwargs)
        logger.info(
            "Loaded %s with PyTorch backend on %s",
            self._model_name,
            self._device,
        )
        return self._model

    def encode(self, text: str) -> list[float]:
        """Encode a single text into a vector. Used for queries."""
        model = self._load_model()
        embedding = model.encode(text, normalize_embeddings=True)
        vec = embedding.tolist() if isinstance(embedding, np.ndarray) else list(embedding)
        if len(vec) != self._dimensions:
            raise ValueError(
                f"Dimension mismatch: model produced {len(vec)}d, "
                f"expected {self._dimensions}d"
            )
        return vec

    def encode_batch(self, texts: list[str], batch_size: int = 64) -> list[list[float]]:
        """Encode multiple texts into vectors. Used for indexing.

        Args:
            texts: List of strings to encode.
            batch_size: Internal batch size for the model.

        Returns:
            List of embedding vectors, one per input text.
        """
        if not texts:
            return []
        model = self._load_model()
        embeddings = model.encode(
            texts,
            batch_size=batch_size,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        result = []
        for emb in embeddings:
            vec = emb.tolist() if isinstance(emb, np.ndarray) else list(emb)
            if len(vec) != self._dimensions:
                raise ValueError(
                    f"Dimension mismatch: model produced {len(vec)}d, "
                    f"expected {self._dimensions}d"
                )
            result.append(vec)
        return result

    @property
    def dimensions(self) -> int:
        """Return configured embedding dimensions."""
        return self._dimensions

    @property
    def model_name(self) -> str:
        """Return the model identifier."""
        return self._model_name

    @property
    def backend(self) -> str:
        """Return the backend in use."""
        return self._backend

    @property
    def is_loaded(self) -> bool:
        """Whether the model has been loaded into memory."""
        return self._model is not None


def get_embedding_service() -> EmbeddingService:
    """Get or create singleton EmbeddingService with config-based invalidation.

    Recreates the service if embedding config changes (model, dimensions, etc.).
    Same singleton + cache-key pattern as the ChromaDB client and PG pool.
    """
    global _service, _service_cache_key
    from .config import get_embedding_config

    cfg = get_embedding_config()
    key = (cfg["model"], cfg["dimensions"], cfg["device"], cfg["backend"])

    if _service is not None and _service_cache_key == key:
        return _service

    _service = EmbeddingService(
        model_name=cfg["model"],
        dimensions=cfg["dimensions"],
        device=cfg["device"],
        backend=cfg["backend"],
    )
    _service_cache_key = key
    return _service


def reset_embedding_service() -> None:
    """Reset the singleton. Used in tests and config changes."""
    global _service, _service_cache_key
    _service = None
    _service_cache_key = None
