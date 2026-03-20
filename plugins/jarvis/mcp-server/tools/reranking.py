"""Cross-encoder reranking for semantic search results.

Uses an ONNX-quantized ms-marco-MiniLM-L-6-v2 model to rescore (query, document)
pairs after initial vector search. This second-stage reranking dramatically improves
precision for queries where vector similarity alone falls short (negation, specificity,
context collapse).

Applied to both query_vault() and semantic_context() (per-prompt search).
Latency protected via max_latency_ms config (default 1000ms).

Dependencies: onnxruntime, tokenizers (~5MB).
Model: ~92MB ONNX, baked into Docker image at /app/models/cross-encoder/.
Falls back to download with SHA-256 verification for local dev.
"""

import hashlib
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

# SHA-256 hashes for integrity verification (HF commit c5ee24cb)
_MODEL_HASHES = {
    "model.onnx": "5d3e70fd0c9ff14b9b5169a51e957b7a9c74897afd0a35ce4bd318150c1d4d4a",
    "tokenizer.json": "d241a60d5e8f04cc1b2b3e9ef7a4921b27bf526d9f6050ab90f9267a1f9e5c66",
}

# Thread-safe singleton state
_lock = threading.Lock()
_session = None  # onnxruntime.InferenceSession
_tokenizer = None  # tokenizers.Tokenizer
_init_failed = False  # Sticky failure flag


def _get_model_dir() -> Path:
    """Return the model directory. Prefers baked-in JARVIS_MODEL_DIR (Docker),
    falls back to JARVIS_HOME/models/cross-encoder (local dev)."""
    baked_dir = os.environ.get("JARVIS_MODEL_DIR")
    if baked_dir:
        p = Path(baked_dir)
        if p.is_dir():
            return p
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


def _verify_file_hash(filepath: Path, expected_hash: str) -> bool:
    """Verify SHA-256 hash of a file. Returns True if match, False otherwise."""
    sha256 = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest() == expected_hash


def _ensure_model_files(model_dir: Path) -> None:
    """Download model files if not already present, with SHA-256 verification."""
    for filename, url in _MODEL_FILES.items():
        filepath = model_dir / filename
        if not filepath.exists():
            logger.info(f"Downloading {filename} for cross-encoder reranking...")
            _download_file(url, filepath)
            # Verify hash after download
            expected = _MODEL_HASHES.get(filename)
            if expected and not _verify_file_hash(filepath, expected):
                filepath.unlink(missing_ok=True)
                raise RuntimeError(
                    f"Hash mismatch for {filename}: file may be corrupted or tampered"
                )


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

        # Min-max normalize vector scores to [0, 1] — removes cross-schema
        # bias from different scoring formulas (e.g. vault _compute_relevance
        # saturates at 1.0 while core compute_blended_score produces lower values).
        min_v = min(vector_scores)
        max_v = max(vector_scores)
        if max_v - min_v > 1e-9:
            norm_vscores = [(v - min_v) / (max_v - min_v) for v in vector_scores]
        else:
            norm_vscores = [0.5] * len(vector_scores)

        # Alpha-blend: both sides normalized to [0, 1] for fair comparison
        blended = [
            alpha * ns + (1 - alpha) * nv
            for ns, nv in zip(norm_scores, norm_vscores)
        ]

        elapsed_ms = (time.time() - start) * 1000
        logger.debug(
            f"Reranking completed: {len(documents)} docs in {elapsed_ms:.0f}ms"
        )

        return blended

    except Exception as e:
        logger.warning(f"Reranking failed: {e}")
        return vector_scores
