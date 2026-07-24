"""Cross-encoder reranking for semantic search results.

Supports the legacy local ONNX MiniLM backend and a host-native llama.cpp
backend. The host backend runs BGE reranking on Metal while the Jarvis MCP and
database remain in the container.

Applied to both query_vault() and semantic_context() (per-prompt search).
Latency protected via max_latency_ms config and fail-open vector-score fallback.

The optional local backend needs onnxruntime and tokenizers and downloads its
model with SHA-256 verification for non-container development. The production
Docker image ships neither those dependencies nor local model assets.
"""

import hashlib
import logging
import math
import os
import threading
import time
import urllib.request
from dataclasses import dataclass
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
_host_client = None
_host_client_key = None
_rerank_state = threading.local()


@dataclass
class RerankResult:
    """Full reranker diagnostics alongside the scores used by retrieval."""

    blended_scores: list[float]
    raw_logits: list[float]
    sigmoid_scores: list[float]
    latency_ms: float
    backend: str
    model: str
    applied: bool
    fallback_reason: Optional[str] = None


def clear_last_rerank_result() -> None:
    _rerank_state.result = None


def get_last_rerank_result() -> Optional[RerankResult]:
    return getattr(_rerank_state, "result", None)


def _get_model_dir() -> Path:
    """Return an explicit model directory or the local-development default."""
    configured_dir = os.environ.get("JARVIS_MODEL_DIR")
    if configured_dir:
        p = Path(configured_dir)
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
    global _session, _tokenizer, _init_failed, _host_client, _host_client_key
    with _lock:
        _session = None
        _tokenizer = None
        _init_failed = False
        _host_client = None
        _host_client_key = None


def _get_host_client(config: dict):
    """Return a cached strict llama.cpp client for the configured reranker."""
    global _host_client, _host_client_key
    key = (
        config.get("host_url", "http://host.docker.internal:8752"),
        config.get("model", "BAAI/bge-reranker-v2-m3"),
        config.get("host_token", ""),
        int(config.get("host_timeout_ms", 1500)),
    )
    if _host_client is not None and _host_client_key == key:
        return _host_client
    with _lock:
        if _host_client is not None and _host_client_key == key:
            return _host_client
        from .model_host_client import ModelHostClient

        _host_client = ModelHostClient(
            base_url=key[0],
            model_name=key[1],
            token=key[2],
            timeout_ms=key[3],
        )
        _host_client_key = key
        return _host_client


def _sigmoid(x: float) -> float:
    """Numerically stable sigmoid function."""
    if x >= 0:
        return 1.0 / (1.0 + math.exp(-x))
    else:
        exp_x = math.exp(x)
        return exp_x / (1.0 + exp_x)


def rerank_raw(
    query: str, documents: list[str], config: Optional[dict] = None
) -> dict:
    """Return host-reranker logits and probabilities without normalization.

    Per-query min-max scores are useful for ordering but cannot support an
    absolute "inject nothing" decision. This diagnostic contract preserves the
    BGE logits so labeled production cases can calibrate that decision.

    Raises on unavailable/unsupported backends; production retrieval continues
    to use :func:`rerank`, which fails open to vector scores.
    """
    if config is None:
        config = {}
    if config.get("backend", "onnx") != "host":
        raise RuntimeError("Raw reranker diagnostics require the host backend")
    if not documents:
        return {"logits": [], "probabilities": [], "latency_ms": 0.0}

    started = time.perf_counter()
    logits = _get_host_client(config).rerank(query, documents)
    latency_ms = (time.perf_counter() - started) * 1000
    return {
        "logits": logits,
        "probabilities": [_sigmoid(score) for score in logits],
        "latency_ms": latency_ms,
    }


def _blend_scores(raw_scores: list[float], vector_scores: list[float], alpha: float) -> list:
    """Globally normalize and blend cross-encoder and vector scores."""
    min_s = min(raw_scores)
    max_s = max(raw_scores)
    if max_s - min_s > 1e-9:
        norm_scores = [(score - min_s) / (max_s - min_s) for score in raw_scores]
    else:
        norm_scores = [0.5] * len(raw_scores)

    min_v = min(vector_scores)
    max_v = max(vector_scores)
    if max_v - min_v > 1e-9:
        norm_vectors = [
            (score - min_v) / (max_v - min_v) for score in vector_scores
        ]
    else:
        norm_vectors = [0.5] * len(vector_scores)
    return [
        alpha * cross_encoder + (1 - alpha) * vector
        for cross_encoder, vector in zip(norm_scores, norm_vectors)
    ]


def rerank_multi_detailed(
    queries: list[str],
    documents: list[str],
    vector_scores: list[float],
    config: Optional[dict] = None,
) -> RerankResult:
    """Rerank candidates against their best bounded query window.

    Host calls are grouped by identical query text, then every raw BGE score is
    normalized globally so candidates from different prompt windows remain
    comparable. Any contract failure preserves the original vector ranking.
    """
    if config is None:
        config = {}
    backend = str(config.get("backend", "onnx"))
    model = str(config.get("model", ""))
    max_latency_ms = config.get("max_latency_ms", 1500)
    if len(documents) <= 1:
        return RerankResult(list(vector_scores), [], [], 0.0, backend, model, False, "too_few_candidates")
    if len(queries) != len(documents) or len(vector_scores) != len(documents):
        return RerankResult(list(vector_scores), [], [], 0.0, backend, model, False, "length_mismatch")
    if backend != "host":
        return RerankResult(list(vector_scores), [], [], 0.0, backend, model, False, "multi_window_requires_host")

    started = time.perf_counter()
    try:
        grouped: dict[str, list[int]] = {}
        for index, query in enumerate(queries):
            grouped.setdefault(query, []).append(index)
        logits: list[float | None] = [None] * len(documents)
        client = _get_host_client(config)
        for query, indexes in grouped.items():
            # Latency budget: check BETWEEN grouped host calls and fall open for
            # the remaining windows on exhaustion (chained ~1.5s host calls
            # across many windows would otherwise blow the 2.5s hook deadline).
            # Mirrors rerank_detailed's between-batch check + fallback shape.
            elapsed_ms = (time.perf_counter() - started) * 1000
            if elapsed_ms > max_latency_ms:
                logger.warning(
                    "Multi-window reranking latency budget exceeded "
                    "(%.0fms > %sms), falling back to vector scores",
                    elapsed_ms, max_latency_ms,
                )
                return RerankResult(list(vector_scores), [], [], elapsed_ms, backend, model, False, "latency_budget_exceeded")
            scores = client.rerank(query, [documents[index] for index in indexes])
            if len(scores) != len(indexes):
                return RerankResult(list(vector_scores), [], [], (time.perf_counter() - started) * 1000, backend, model, False, "score_count_mismatch")
            for index, score in zip(indexes, scores):
                logits[index] = float(score)
        if any(score is None for score in logits):
            return RerankResult(list(vector_scores), [], [], (time.perf_counter() - started) * 1000, backend, model, False, "missing_scores")
        raw_logits = [float(score) for score in logits]
        sigmoid_scores = [_sigmoid(score) for score in raw_logits]
        blended = _blend_scores(
            sigmoid_scores,
            vector_scores,
            float(config.get("alpha", 0.7)),
        )
        return RerankResult(blended, raw_logits, sigmoid_scores, (time.perf_counter() - started) * 1000, backend, model, True)
    except Exception as exc:
        logger.warning("Multi-window reranking failed: %s", exc)
        return RerankResult(list(vector_scores), [], [], (time.perf_counter() - started) * 1000, backend, model, False, str(exc))


def rerank_multi(
    queries: list[str],
    documents: list[str],
    vector_scores: list[float],
    config: Optional[dict] = None,
) -> list:
    """Compatibility wrapper preserving identity-based fallback detection."""
    result = rerank_multi_detailed(queries, documents, vector_scores, config)
    _rerank_state.result = result
    return result.blended_scores if result.applied else vector_scores


def rerank_detailed(
    query: str, documents: list, vector_scores: list, config: Optional[dict] = None
) -> RerankResult:
    """Rerank documents using cross-encoder model.

    Obtains raw cross-encoder scores from local ONNX or host llama.cpp,
    applies sigmoid + min-max normalization, then alpha-blends with vector
    scores.

    Returns vector_scores identity on any failure (graceful fallback).
    Returns a NEW list on success. Caller can use identity check
    (result is not vector_scores) to detect whether reranking was applied.

    Args:
        query: Search query string
        documents: List of document texts
        vector_scores: List of vector similarity scores (same length as documents)
        config: Reranking config dict (backend, alpha, latency, connection)

    Returns:
        List of blended scores (new list on success, same object on fallback)
    """
    if config is None:
        config = {}

    alpha = config.get("alpha", 0.7)
    max_latency_ms = config.get("max_latency_ms", 1500)
    batch_size = config.get("batch_size", 32)
    backend = config.get("backend", "onnx")
    model = str(config.get("model", ""))

    if not documents or len(documents) <= 1:
        return RerankResult(list(vector_scores), [], [], 0.0, str(backend), model, False, "too_few_candidates")

    start = time.time()

    try:
        if backend == "host":
            raw_logits = [float(score) for score in _get_host_client(config).rerank(query, documents)]
            raw_scores = [_sigmoid(score) for score in raw_logits]
        elif backend == "onnx":
            if not _init_model():
                return RerankResult(list(vector_scores), [], [], (time.time() - start) * 1000, str(backend), model, False, "model_unavailable")

            # Tokenize all (query, document) pairs
            pairs = [(query, doc) for doc in documents]
            raw_scores = []
            raw_logits = []
            for i in range(0, len(pairs), batch_size):
                batch = pairs[i : i + batch_size]

                # Check latency budget before each batch
                elapsed_ms = (time.time() - start) * 1000
                if elapsed_ms > max_latency_ms:
                    logger.warning(
                        "Reranking latency budget exceeded "
                        f"({elapsed_ms:.0f}ms > {max_latency_ms}ms), "
                        f"processed {i}/{len(pairs)} pairs"
                    )
                    return RerankResult(list(vector_scores), raw_logits, raw_scores, elapsed_ms, str(backend), model, False, "latency_budget_exceeded")

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
                    logit = float(logit_row[0])
                    raw_logits.append(logit)
                    raw_scores.append(_sigmoid(logit))
        else:
            logger.warning("Unknown reranking backend %r; using vector scores", backend)
            return RerankResult(list(vector_scores), [], [], (time.time() - start) * 1000, str(backend), model, False, "unknown_backend")

        elapsed_ms = (time.time() - start) * 1000
        if elapsed_ms > max_latency_ms:
            logger.warning(
                "Reranking latency budget exceeded "
                f"({elapsed_ms:.0f}ms > {max_latency_ms}ms)"
            )
            return RerankResult(list(vector_scores), raw_logits, raw_scores, elapsed_ms, str(backend), model, False, "latency_budget_exceeded")

        if len(raw_scores) != len(documents):
            logger.warning(
                f"Reranking score count mismatch: {len(raw_scores)} vs {len(documents)}"
            )
            return RerankResult(list(vector_scores), raw_logits, raw_scores, elapsed_ms, str(backend), model, False, "score_count_mismatch")

        # Alpha-blend after normalizing both scales across the same candidates.
        blended = _blend_scores(raw_scores, vector_scores, alpha)

        logger.debug(
            "Reranking completed with %s: %d docs in %.0fms",
            backend,
            len(documents),
            elapsed_ms,
        )

        return RerankResult(blended, raw_logits, raw_scores, elapsed_ms, str(backend), model, True)

    except Exception as e:
        logger.warning(f"Reranking failed: {e}")
        return RerankResult(list(vector_scores), [], [], (time.time() - start) * 1000, str(backend), model, False, str(e))


def rerank(
    query: str, documents: list, vector_scores: list, config: Optional[dict] = None
) -> list:
    """Compatibility wrapper preserving identity-based fallback detection."""
    result = rerank_detailed(query, documents, vector_scores, config)
    _rerank_state.result = result
    return result.blended_scores if result.applied else vector_scores
