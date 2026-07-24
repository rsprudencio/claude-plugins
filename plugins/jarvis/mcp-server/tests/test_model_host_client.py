"""Tests for the strict llama.cpp retrieval client."""

from unittest.mock import patch

import pytest

from tools.model_host_client import ModelHostClient, ModelHostError


class TestModelHostClient:
    def test_embedding_uses_openai_contract_and_restores_index_order(self):
        client = ModelHostClient("http://models:8751", "granite", dimensions=3)
        payload = {
            "model": "granite",
            "data": [
                {"index": 1, "embedding": [0.0, 1.0, 0.0]},
                {"index": 0, "embedding": [1.0, 0.0, 0.0]},
            ],
        }
        with patch.object(client, "_request", return_value=payload) as request:
            result = client.embed(["one", "two"])

        assert result == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
        request.assert_called_once_with(
            "/v1/embeddings",
            {
                "model": "granite",
                "input": ["one", "two"],
                "encoding_format": "float",
            },
        )

    def test_embedding_rejects_bad_dimensions(self):
        client = ModelHostClient("http://models:8751", "granite", dimensions=3)
        with patch.object(
            client,
            "_request",
            return_value={"model": "granite", "data": [{"index": 0, "embedding": [1.0]}]},
        ):
            with pytest.raises(ModelHostError, match="invalid dimensions"):
                client.embed(["one"])

    def test_rerank_restores_original_document_order(self):
        client = ModelHostClient("http://models:8752", "bge")
        payload = {
            "model": "bge",
            "results": [
                {"index": 1, "relevance_score": 4.5},
                {"index": 0, "relevance_score": -2.0},
            ],
        }
        with patch.object(client, "_request", return_value=payload) as request:
            result = client.rerank("query", ["first", "second"])

        assert result == [-2.0, 4.5]
        request.assert_called_once_with(
            "/v1/rerank",
            {
                "model": "bge",
                "query": "query",
                "documents": ["first", "second"],
                "top_n": 2,
            },
        )

    def test_identity_mismatch_fails_closed(self):
        client = ModelHostClient("http://models:8752", "bge")
        with patch.object(
            client,
            "_request",
            return_value={"model": "other", "results": []},
        ):
            with pytest.raises(ModelHostError, match="identity mismatch"):
                client.rerank("query", ["document"])

    def test_duplicate_reranking_index_is_rejected(self):
        client = ModelHostClient("http://models:8752", "bge")
        payload = {
            "model": "bge",
            "results": [
                {"index": 0, "relevance_score": 1.0},
                {"index": 0, "relevance_score": 0.5},
            ],
        }
        with patch.object(client, "_request", return_value=payload):
            with pytest.raises(ModelHostError, match="duplicate"):
                client.rerank("query", ["one", "two"])

    def test_tokenize_preserves_text_and_byte_pieces(self):
        client = ModelHostClient("http://models:8751", "granite", dimensions=3)
        tokens = [
            {"id": 1, "piece": "hello"},
            {"id": 2, "piece": [240, 159]},
        ]
        with patch.object(client, "_request", return_value={"tokens": tokens}) as request:
            result = client.tokenize("hello🚀", with_pieces=True)

        assert result == tokens
        request.assert_called_once_with(
            "/tokenize",
            {
                "content": "hello🚀",
                "add_special": False,
                "with_pieces": True,
            },
        )
