"""Strict client for host-native llama.cpp retrieval inference."""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from typing import Any


class ModelHostError(RuntimeError):
    """Raised when the model host is unavailable or violates its contract."""


class ModelHostClient:
    def __init__(self, base_url: str, model_name: str,
                 dimensions: int | None = None, token: str = "",
                 timeout_ms: int = 2000) -> None:
        if not base_url:
            raise ValueError("Model host URL must not be empty")
        if not model_name:
            raise ValueError("Model host model name must not be empty")
        if dimensions is not None and dimensions <= 0:
            raise ValueError("Model host dimensions must be positive")
        if timeout_ms <= 0:
            raise ValueError("Model host timeout must be positive")
        self._base_url = base_url.rstrip("/")
        self._model_name = model_name
        self._dimensions = dimensions
        self._token = token
        self._timeout_seconds = timeout_ms / 1000

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        payload = self._request("/v1/embeddings", {
            "model": self._model_name,
            "input": texts,
            "encoding_format": "float",
        })
        self._validate_identity(payload)
        if self._dimensions is None:
            raise ModelHostError("Embedding dimensions were not configured")
        data = payload.get("data")
        if not isinstance(data, list) or len(data) != len(texts):
            raise ModelHostError(
                f"Model host returned an invalid embedding count: expected {len(texts)}"
            )
        result: list[list[float] | None] = [None] * len(texts)
        for fallback_index, item in enumerate(data):
            if not isinstance(item, dict):
                raise ModelHostError("Model host embedding entry must be an object")
            index = item.get("index", fallback_index)
            if not isinstance(index, int) or not 0 <= index < len(texts):
                raise ModelHostError(f"Model host returned invalid embedding index: {index!r}")
            if result[index] is not None:
                raise ModelHostError(f"Model host returned duplicate embedding index: {index}")
            embedding = item.get("embedding")
            if not isinstance(embedding, list) or len(embedding) != self._dimensions:
                raise ModelHostError(
                    f"Model host embedding {index} has invalid dimensions: expected {self._dimensions}"
                )
            if not all(isinstance(v, (int, float)) and math.isfinite(v)
                       for v in embedding):
                raise ModelHostError(
                    f"Model host embedding {index} contains non-finite values"
                )
            result[index] = [float(v) for v in embedding]
        if any(embedding is None for embedding in result):
            raise ModelHostError("Model host omitted an embedding")
        return [embedding for embedding in result if embedding is not None]

    def rerank(self, query: str, documents: list[str]) -> list[float]:
        """Return one raw cross-encoder score per document in input order."""
        if not documents:
            return []
        payload = self._request("/v1/rerank", {
            "model": self._model_name,
            "query": query,
            "documents": documents,
            "top_n": len(documents),
        })
        self._validate_identity(payload)
        results = payload.get("results")
        if not isinstance(results, list) or len(results) != len(documents):
            raise ModelHostError(
                f"Model host returned an invalid reranking count: expected {len(documents)}"
            )
        scores: list[float | None] = [None] * len(documents)
        for item in results:
            if not isinstance(item, dict):
                raise ModelHostError("Model host reranking entry must be an object")
            index = item.get("index")
            if not isinstance(index, int) or not 0 <= index < len(documents):
                raise ModelHostError(f"Model host returned invalid reranking index: {index!r}")
            if scores[index] is not None:
                raise ModelHostError(f"Model host returned duplicate reranking index: {index}")
            score = item.get("relevance_score", item.get("score"))
            if not isinstance(score, (int, float)) or not math.isfinite(score):
                raise ModelHostError(f"Model host returned invalid score for index {index}")
            scores[index] = float(score)
        if any(score is None for score in scores):
            raise ModelHostError("Model host omitted a reranking score")
        return [score for score in scores if score is not None]

    def tokenize(self, text: str, *, with_pieces: bool = False) -> list:
        """Tokenize arbitrary text without consuming an inference context.

        llama.cpp exposes tokenization separately from embeddings/reranking,
        which lets Jarvis split unbounded inputs into bounded inference
        windows without dropping any part of the original text.
        """
        if not isinstance(text, str):
            raise TypeError("text must be a string")
        payload = self._request(
            "/tokenize",
            {
                "content": text,
                "add_special": False,
                "with_pieces": with_pieces,
            },
        )
        tokens = payload.get("tokens")
        if not isinstance(tokens, list):
            raise ModelHostError("Model host returned invalid tokenizer output")
        if with_pieces:
            for token in tokens:
                if not isinstance(token, dict) or not isinstance(token.get("id"), int):
                    raise ModelHostError("Model host returned invalid token pieces")
                piece = token.get("piece")
                if not isinstance(piece, (str, list)):
                    raise ModelHostError("Model host returned an invalid token piece")
                if isinstance(piece, list) and not all(
                    isinstance(value, int) and 0 <= value <= 255 for value in piece
                ):
                    raise ModelHostError("Model host returned invalid token bytes")
        elif not all(isinstance(token, int) for token in tokens):
            raise ModelHostError("Model host returned invalid token IDs")
        return tokens

    def _validate_identity(self, payload: dict[str, Any]) -> None:
        returned_model = payload.get("model")
        if returned_model is not None and returned_model != self._model_name:
            raise ModelHostError(
                f"Model host identity mismatch: expected {self._model_name!r}, got {returned_model!r}"
            )

    def _request(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        headers = {"Content-Type": "application/json"}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        request = urllib.request.Request(
            f"{self._base_url}{path}", data=json.dumps(body).encode("utf-8"),
            headers=headers, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_seconds) as response:
                raw = response.read()
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ModelHostError(f"Model host request failed: {exc}") from exc
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise ModelHostError("Model host returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ModelHostError("Model host response must be a JSON object")
        return payload
