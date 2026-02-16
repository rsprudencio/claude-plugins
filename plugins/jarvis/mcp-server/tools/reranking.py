"""Cross-encoder reranking for semantic search results.

Uses an ONNX-quantized ms-marco-MiniLM-L-6-v2 model to rescore (query, document)
pairs after initial vector search. This second-stage reranking dramatically improves
precision for queries where vector similarity alone falls short (negation, specificity,
context collapse).

Applied ONLY to query_vault(). NOT applied to semantic_context() (per-prompt search)
which has a ~100ms latency budget.

Dependencies: onnxruntime (already via chromadb), tokenizers (~5MB).
Model: ~23MB ONNX, downloaded on first use to ~/.jarvis/models/cross-encoder/.
"""

import logging
import math
import os
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

logger = logging.getLogger("jarvis-core")

# Model files on HuggingFace
_HF_REPO = "cross-encoder/ms-marco-MiniLM-L-6-v2"
_HF_BASE = f"https://huggingface.co/{_HF_REPO}/resolve/main"
_MODEL_FILES = {
    "model.onnx": f"{_HF_BASE}/onnx/model.onnx",
    "tokenizer.json": f"{_HF_BASE}/tokenizer.json",
}

# Thread-safe singleton state
_lock = threading.Lock()
_session = None  # onnxruntime.InferenceSession
_tokenizer = None  # tokenizers.Tokenizer
_init_failed = False  # Sticky failure flag


def _get_model_dir() -> Path:
    """Return the local model cache directory."""
    jarvis_home = os.environ.get("JARVIS_HOME", str(Path.home() / ".jarvis"))
    return Path(jarvis_home) / "models" / "cross-encoder"


def _download_file(url: str, dest: Path) -> None:
    """Download a file from URL to dest using stdlib urllib."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".tmp")
    try:
        urllib.request.urlretrieve(url, str(tmp))
        tmp.rename(dest)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def _ensure_model_files(model_dir: Path) -> None:
    """Download model files if not already present."""
    for filename, url in _MODEL_FILES.items():
        filepath = model_dir / filename
        if not filepath.exists():
            logger.info(f"Downloading {filename} for cross-encoder reranking...")
            _download_file(url, filepath)


def _init_model() -> bool:
    """Initialize the ONNX model and tokenizer (thread-safe singleton).

    Returns True on success, False on failure. Once failed, all subsequent
    calls return False immediately (sticky failure) until reset_model() is called.
    """
    global _session, _tokenizer, _init_failed

    # Fast path: already initialized
    if _session is not None and _tokenizer is not None:
        return True

    # Fast path: sticky failure
    if _init_failed:
        return False

    with _lock:
        # Double-check inside lock
        if _session is not None and _tokenizer is not None:
            return True
        if _init_failed:
            return False

        try:
            import onnxruntime as ort
            from tokenizers import Tokenizer
        except ImportError as e:
            logger.warning(f"Reranking dependencies unavailable: {e}")
            _init_failed = True
            return False

        try:
            model_dir = _get_model_dir()
            _ensure_model_files(model_dir)

            _session = ort.InferenceSession(
                str(model_dir / "model.onnx"),
                providers=["CPUExecutionProvider"],
            )
            _tokenizer = Tokenizer.from_file(str(model_dir / "tokenizer.json"))
            logger.info("Cross-encoder reranking model loaded successfully")
            return True

        except Exception as e:
            logger.warning(f"Failed to initialize reranking model: {e}")
            _init_failed = True
            _session = None
            _tokenizer = None
            return False


def reset_model() -> None:
    """Clear singleton state (for tests and config changes)."""
    global _session, _tokenizer, _init_failed
    with _lock:
        _session = None
        _tokenizer = None
        _init_failed = False


def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid function."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    else:
        exp_x = math.exp(x)
        return exp_x / (1.0 + exp_x)


def rerank(
    query: str, documents: list, vector_scores: list, config: Optional[dict] = None
) -> list:
    """Rerank documents using cross-encoder model.

    Tokenizes (query, document) pairs in batch, runs ONNX inference,
    applies sigmoid + min-max normalization, then alpha-blends with
    vector scores.

    Returns vector_scores identity on any failure (graceful fallback).
    Returns a NEW list on success. Caller can use identity check
    (result is not vector_scores) to detect whether reranking was applied.

    Args:
        query: Search query string
        documents: List of document texts
        vector_scores: List of vector similarity scores (same length as documents)
        config: Reranking config dict (alpha, max_latency_ms, batch_size)

    Returns:
        List of blended scores (new list on success, same object on fallback)
    """
    if not documents or len(documents) <= 1:
        return vector_scores

    if config is None:
        config = {}

    alpha = config.get("alpha", 0.7)
    max_latency_ms = config.get("max_latency_ms", 1000)
    batch_size = config.get("batch_size", 32)

    # Try to initialize model
    if not _init_model():
        return vector_scores

    start = time.time()

    try:
        # Tokenize all (query, document) pairs
        pairs = [(query, doc) for doc in documents]

        raw_scores = []
        for i in range(0, len(pairs), batch_size):
            batch = pairs[i : i + batch_size]

            # Check latency budget before each batch
            elapsed_ms = (time.time() - start) * 1000
            if elapsed_ms > max_latency_ms:
                logger.warning(
                    f"Reranking latency budget exceeded ({elapsed_ms:.0f}ms > {max_latency_ms}ms), "
                    f"processed {i}/{len(pairs)} pairs"
                )
                return vector_scores

            # Encode batch
            encodings = _tokenizer.encode_batch([list(pair) for pair in batch])

            # Pad to uniform length and run as single batched inference
            max_len = max(len(e.ids) for e in encodings)
            batch_ids = []
            batch_type_ids = []
            batch_attention = []

            for encoding in encodings:
                pad_len = max_len - len(encoding.ids)
                batch_ids.append(encoding.ids + [0] * pad_len)
                batch_type_ids.append(encoding.type_ids + [0] * pad_len)
                batch_attention.append([1] * len(encoding.ids) + [0] * pad_len)

            outputs = _session.run(
                None,
                {
                    "input_ids": batch_ids,
                    "token_type_ids": batch_type_ids,
                    "attention_mask": batch_attention,
                },
            )
            # outputs[0] shape: [batch_size, 1] — extract logits
            for logit_row in outputs[0]:
                raw_scores.append(_sigmoid(float(logit_row[0])))

        if len(raw_scores) != len(documents):
            logger.warning(
                f"Reranking score count mismatch: {len(raw_scores)} vs {len(documents)}"
            )
            return vector_scores

        # Min-max normalize raw scores to [0, 1]
        min_s = min(raw_scores)
        max_s = max(raw_scores)
        if max_s - min_s > 1e-9:
            norm_scores = [(s - min_s) / (max_s - min_s) for s in raw_scores]
        else:
            norm_scores = [0.5] * len(raw_scores)

        # Alpha-blend: blended = alpha * reranker + (1 - alpha) * vector
        blended = [
            alpha * ns + (1 - alpha) * vs for ns, vs in zip(norm_scores, vector_scores)
        ]

        elapsed_ms = (time.time() - start) * 1000
        logger.debug(
            f"Reranking completed: {len(documents)} docs in {elapsed_ms:.0f}ms"
        )

        return blended

    except Exception as e:
        logger.warning(f"Reranking failed: {e}")
        return vector_scores
