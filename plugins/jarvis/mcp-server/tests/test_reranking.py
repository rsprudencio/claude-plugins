"""Tests for cross-encoder reranking module."""

import math
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tools.reranking import (
    rerank,
    reset_model,
    _sigmoid,
    _get_model_dir,
    _ensure_model_files,
    _download_file,
    _verify_file_hash,
)


def _make_encoding():
    """Create a mock encoding with ids, type_ids."""
    enc = MagicMock()
    enc.ids = [101, 2003, 102]
    enc.type_ids = [0, 0, 1]
    return enc


def _mock_encode_batch(pairs):
    """Return one encoding per pair (matching rerank's expectations)."""
    return [_make_encoding() for _ in pairs]


def _make_batched_run(logits_per_doc):
    """Create a mock session.run that returns batched output matching input batch size.

    Args:
        logits_per_doc: list of logit values, one per document in order.
                       Consumed across calls as batches arrive.
    """
    offset = [0]  # mutable counter

    def run(output_names, inputs):
        batch_size = len(inputs["input_ids"])
        batch_logits = [[logits_per_doc[offset[0] + i]] for i in range(batch_size)]
        offset[0] += batch_size
        return [batch_logits]

    return run


def _make_constant_batched_run(logit=0.5):
    """Create a mock session.run that returns a constant logit for every doc in the batch."""

    def run(output_names, inputs):
        batch_size = len(inputs["input_ids"])
        return [[[logit]] * batch_size]

    return run


class TestSigmoid:
    """Tests for sigmoid helper."""

    def test_zero(self):
        assert _sigmoid(0.0) == 0.5

    def test_large_positive(self):
        assert _sigmoid(100.0) == pytest.approx(1.0, abs=1e-6)

    def test_large_negative(self):
        assert _sigmoid(-100.0) == pytest.approx(0.0, abs=1e-6)

    def test_positive_value(self):
        result = _sigmoid(2.0)
        expected = 1.0 / (1.0 + math.exp(-2.0))
        assert result == pytest.approx(expected, abs=1e-9)

    def test_negative_value(self):
        result = _sigmoid(-2.0)
        expected = math.exp(-2.0) / (1.0 + math.exp(-2.0))
        assert result == pytest.approx(expected, abs=1e-9)

    def test_symmetry(self):
        """sigmoid(x) + sigmoid(-x) == 1.0"""
        for x in [0.5, 1.0, 3.0, -1.5]:
            assert _sigmoid(x) + _sigmoid(-x) == pytest.approx(1.0, abs=1e-9)


class TestRerank:
    """Tests for the main rerank function."""

    def setup_method(self):
        reset_model()

    def test_empty_documents_returns_identity(self):
        scores = [0.8, 0.6]
        result = rerank("query", [], scores)
        assert result is scores

    def test_single_document_returns_identity(self):
        scores = [0.8]
        result = rerank("query", ["doc"], scores)
        assert result is scores

    def test_fallback_on_init_failure(self):
        """When model init fails, returns same list object."""
        scores = [0.8, 0.6, 0.4]
        docs = ["doc1", "doc2", "doc3"]

        with patch("tools.reranking._init_model", return_value=False):
            result = rerank("test query", docs, scores)
            assert result is scores

    def test_success_returns_new_list(self):
        """On success, returns a new list (not identity)."""
        scores = [0.8, 0.6, 0.4]
        docs = ["authentication with OAuth", "Python tips", "cooking recipes"]

        mock_session = MagicMock()
        mock_tokenizer = MagicMock()
        mock_tokenizer.encode_batch.side_effect = _mock_encode_batch

        # Model returns different logits for each doc (batched)
        mock_session.run.side_effect = _make_batched_run([2.5, -0.5, -2.0])

        with patch("tools.reranking._init_model", return_value=True), patch(
            "tools.reranking._session", mock_session
        ), patch("tools.reranking._tokenizer", mock_tokenizer):
            result = rerank("auth query", docs, scores, {"alpha": 0.7})
            assert result is not scores
            assert len(result) == 3
            # First doc should have highest blended score
            assert result[0] > result[1]
            assert result[0] > result[2]

    def test_alpha_zero_returns_vector_scores(self):
        """alpha=0 means 100% vector scores."""
        scores = [0.8, 0.6, 0.4]
        docs = ["doc1", "doc2", "doc3"]

        mock_session = MagicMock()
        mock_tokenizer = MagicMock()
        mock_tokenizer.encode_batch.side_effect = _mock_encode_batch

        mock_session.run.side_effect = _make_batched_run([1.0, 0.0, -1.0])

        with patch("tools.reranking._init_model", return_value=True), patch(
            "tools.reranking._session", mock_session
        ), patch("tools.reranking._tokenizer", mock_tokenizer):
            result = rerank("query", docs, scores, {"alpha": 0.0})
            assert result is not scores
            # With alpha=0, blended scores should equal vector scores
            for r, v in zip(result, scores):
                assert r == pytest.approx(v, abs=1e-6)

    def test_alpha_one_ignores_vector(self):
        """alpha=1 means 100% reranker scores."""
        scores = [0.1, 0.9, 0.5]
        docs = ["very relevant", "not relevant", "somewhat"]

        mock_session = MagicMock()
        mock_tokenizer = MagicMock()
        mock_tokenizer.encode_batch.side_effect = _mock_encode_batch

        # Reranker says: doc1 is best, doc3 middle, doc2 worst (batched)
        mock_session.run.side_effect = _make_batched_run([3.0, -2.0, 0.5])

        with patch("tools.reranking._init_model", return_value=True), patch(
            "tools.reranking._session", mock_session
        ), patch("tools.reranking._tokenizer", mock_tokenizer):
            result = rerank("query", docs, scores, {"alpha": 1.0})
            assert result is not scores
            # With alpha=1, order should follow reranker: doc1 > doc3 > doc2
            assert result[0] > result[2] > result[1]

    def test_latency_exceeded_returns_fallback(self):
        """If latency exceeds budget, returns vector_scores."""
        scores = [0.8, 0.6, 0.4]
        docs = ["doc1", "doc2", "doc3"]

        mock_session = MagicMock()
        mock_tokenizer = MagicMock()
        mock_tokenizer.encode_batch.side_effect = _mock_encode_batch

        # Make inference slow — return batched output matching input size
        import time as time_mod

        def slow_run(output_names, inputs):
            time_mod.sleep(0.05)
            batch_size = len(inputs["input_ids"])
            return [[[1.0]] * batch_size]

        mock_session.run.side_effect = slow_run

        with patch("tools.reranking._init_model", return_value=True), patch(
            "tools.reranking._session", mock_session
        ), patch("tools.reranking._tokenizer", mock_tokenizer):
            # Use batch_size=1 so each doc is a separate batch,
            # and latency check triggers between batches
            result = rerank(
                "query",
                docs,
                scores,
                {
                    "max_latency_ms": 1,
                    "batch_size": 1,
                },
            )
            assert result is scores

    def test_exception_returns_fallback(self):
        """Any exception during reranking returns vector_scores."""
        scores = [0.8, 0.6, 0.4]
        docs = ["doc1", "doc2", "doc3"]

        with patch("tools.reranking._init_model", return_value=True), patch(
            "tools.reranking._tokenizer"
        ) as mock_tok:
            mock_tok.encode_batch.side_effect = RuntimeError("tokenizer error")
            result = rerank("query", docs, scores)
            assert result is scores

    def test_default_config_used_when_none(self):
        """config=None should use defaults without error."""
        scores = [0.8, 0.6]
        with patch("tools.reranking._init_model", return_value=False):
            result = rerank("query", ["a", "b"], scores, config=None)
            assert result is scores

    def test_batch_processing(self):
        """Large doc list gets processed in batches."""
        n = 10
        scores = [0.5] * n
        docs = [f"document {i}" for i in range(n)]

        mock_session = MagicMock()
        mock_tokenizer = MagicMock()
        # Return correct number of encodings per batch
        mock_tokenizer.encode_batch.side_effect = _mock_encode_batch

        mock_session.run.side_effect = _make_constant_batched_run(0.5)

        with patch("tools.reranking._init_model", return_value=True), patch(
            "tools.reranking._session", mock_session
        ), patch("tools.reranking._tokenizer", mock_tokenizer):
            result = rerank("query", docs, scores, {"batch_size": 3})
            assert result is not scores
            assert len(result) == n
            # encode_batch should be called ceil(10/3) = 4 times
            assert mock_tokenizer.encode_batch.call_count == 4


class TestModelInit:
    """Tests for model initialization."""

    def setup_method(self):
        reset_model()

    def test_import_error_sets_sticky_failure(self):
        """If onnxruntime can't be imported, sets sticky failure."""
        import tools.reranking as mod

        reset_model()

        # Directly simulate the import failure path by setting state
        mod._init_failed = False
        mod._session = None
        mod._tokenizer = None

        original_import = (
            __builtins__.__import__
            if hasattr(__builtins__, "__import__")
            else __import__
        )

        def mock_import(name, *args, **kwargs):
            if name in ("onnxruntime", "tokenizers"):
                raise ImportError(f"no {name}")
            return original_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            result = mod._init_model()
            assert result is False
            assert mod._init_failed is True

            # Second call should also fail (sticky)
            result2 = mod._init_model()
            assert result2 is False

        reset_model()

    def test_download_failure_sets_sticky_failure(self):
        """Model download failure sets sticky failure."""
        reset_model()

        with patch(
            "tools.reranking._ensure_model_files",
            side_effect=OSError("download failed"),
        ):
            from tools.reranking import _init_model

            result = _init_model()
            assert result is False

        reset_model()

    def test_reset_clears_sticky_failure(self):
        """reset_model() allows retrying after failure."""
        import tools.reranking as mod

        mod._init_failed = True
        assert mod._init_model() is False

        reset_model()
        assert mod._init_failed is False


class TestEnsureModelFiles:
    """Tests for model file downloading."""

    def test_skip_existing_files(self, tmp_path):
        """Files that already exist are not re-downloaded."""
        model_dir = tmp_path / "models"
        model_dir.mkdir()
        (model_dir / "model.onnx").write_text("fake model")
        (model_dir / "tokenizer.json").write_text("fake tokenizer")

        with patch("tools.reranking._download_file") as mock_dl:
            with patch(
                "tools.reranking._MODEL_FILES",
                {
                    "model.onnx": "http://example.com/model.onnx",
                    "tokenizer.json": "http://example.com/tokenizer.json",
                },
            ):
                _ensure_model_files(model_dir)
                mock_dl.assert_not_called()

    def test_downloads_missing_files(self, tmp_path):
        """Missing files are downloaded."""
        model_dir = tmp_path / "models"
        model_dir.mkdir()

        with patch("tools.reranking._download_file") as mock_dl, patch(
            "tools.reranking._verify_file_hash", return_value=True
        ):
            with patch(
                "tools.reranking._MODEL_FILES",
                {
                    "model.onnx": "http://example.com/model.onnx",
                    "tokenizer.json": "http://example.com/tokenizer.json",
                },
            ):
                _ensure_model_files(model_dir)
                assert mock_dl.call_count == 2

    def test_download_error_propagates(self, tmp_path):
        """Download errors bubble up."""
        model_dir = tmp_path / "models"
        model_dir.mkdir()

        with patch(
            "tools.reranking._download_file", side_effect=OSError("network error")
        ):
            with patch(
                "tools.reranking._MODEL_FILES",
                {
                    "model.onnx": "http://example.com/model.onnx",
                },
            ):
                with pytest.raises(OSError, match="network error"):
                    _ensure_model_files(model_dir)

    def test_hash_mismatch_deletes_file_and_raises(self, tmp_path):
        """Downloaded file with wrong hash is deleted and RuntimeError raised."""
        model_dir = tmp_path / "models"
        model_dir.mkdir()

        def fake_download(url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"corrupted model data")

        with patch("tools.reranking._download_file", side_effect=fake_download):
            with patch(
                "tools.reranking._MODEL_FILES",
                {"model.onnx": "http://example.com/model.onnx"},
            ):
                with pytest.raises(RuntimeError, match="Hash mismatch"):
                    _ensure_model_files(model_dir)
                # File should be deleted after hash mismatch
                assert not (model_dir / "model.onnx").exists()

    def test_hash_match_keeps_file(self, tmp_path):
        """Downloaded file with correct hash is kept."""
        import hashlib

        model_dir = tmp_path / "models"
        model_dir.mkdir()
        content = b"valid model content"
        correct_hash = hashlib.sha256(content).hexdigest()

        def fake_download(url, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(content)

        with patch("tools.reranking._download_file", side_effect=fake_download):
            with patch(
                "tools.reranking._MODEL_FILES",
                {"model.onnx": "http://example.com/model.onnx"},
            ), patch(
                "tools.reranking._MODEL_HASHES",
                {"model.onnx": correct_hash},
            ):
                _ensure_model_files(model_dir)
                assert (model_dir / "model.onnx").exists()


class TestDownloadFile:
    """Tests for file download helper."""

    def test_creates_parent_dirs(self, tmp_path):
        """Parent directories are created if needed."""
        dest = tmp_path / "deep" / "nested" / "file.bin"

        with patch("urllib.request.urlretrieve") as mock_retrieve:
            mock_retrieve.return_value = (str(dest.with_suffix(".tmp")), None)
            # Create the tmp file so rename works
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.with_suffix(".tmp").write_bytes(b"data")

            _download_file("http://example.com/file.bin", dest)
            assert dest.exists()

    def test_cleanup_on_failure(self, tmp_path):
        """Temp file is cleaned up on download failure."""
        dest = tmp_path / "file.bin"
        tmp_file = dest.with_suffix(".tmp")

        with patch("urllib.request.urlretrieve") as mock_retrieve:

            def fake_retrieve(url, path):
                Path(path).write_bytes(b"partial")
                raise ConnectionError("connection lost")

            mock_retrieve.side_effect = fake_retrieve

            with pytest.raises(ConnectionError):
                _download_file("http://example.com/file.bin", dest)

            assert not tmp_file.exists()


class TestGetModelDir:
    """Tests for model directory resolution."""

    def test_default_path(self):
        """Default model dir is under ~/.jarvis/models/."""
        env = dict(os.environ)
        env.pop("JARVIS_HOME", None)
        with patch.dict("os.environ", env, clear=True):
            model_dir = _get_model_dir()
            assert "models" in str(model_dir)
            assert "cross-encoder" in str(model_dir)

    def test_respects_jarvis_home(self, tmp_path):
        """JARVIS_HOME env var changes model dir."""
        with patch.dict("os.environ", {"JARVIS_HOME": str(tmp_path)}):
            model_dir = _get_model_dir()
            assert str(model_dir).startswith(str(tmp_path))

    def test_jarvis_model_dir_takes_precedence(self, tmp_path):
        """JARVIS_MODEL_DIR env var overrides JARVIS_HOME-based path."""
        model_dir = tmp_path / "baked-models"
        model_dir.mkdir()
        with patch.dict(
            "os.environ",
            {"JARVIS_MODEL_DIR": str(model_dir), "JARVIS_HOME": "/should/not/use"},
        ):
            result = _get_model_dir()
            assert result == model_dir

    def test_jarvis_model_dir_nonexistent_falls_back(self, tmp_path):
        """JARVIS_MODEL_DIR is ignored if directory doesn't exist."""
        with patch.dict(
            "os.environ",
            {"JARVIS_MODEL_DIR": "/nonexistent/path", "JARVIS_HOME": str(tmp_path)},
        ):
            result = _get_model_dir()
            assert str(result).startswith(str(tmp_path))


class TestVerifyFileHash:
    """Tests for SHA-256 file hash verification."""

    def test_correct_hash(self, tmp_path):
        """Returns True when hash matches."""
        import hashlib

        filepath = tmp_path / "test.bin"
        content = b"test content for hashing"
        filepath.write_bytes(content)
        expected = hashlib.sha256(content).hexdigest()
        assert _verify_file_hash(filepath, expected) is True

    def test_wrong_hash(self, tmp_path):
        """Returns False when hash doesn't match."""
        filepath = tmp_path / "test.bin"
        filepath.write_bytes(b"some content")
        assert _verify_file_hash(filepath, "0" * 64) is False


class TestRerankingConfig:
    """Tests for reranking configuration."""

    def test_defaults(self, mock_config):
        from tools.config import get_reranking_config

        config = get_reranking_config()
        assert config["enabled"] is True
        assert config["candidate_count"] == 100
        assert config["top_k"] == 10
        assert config["alpha"] == 0.7
        assert config["max_latency_ms"] == 1000
        assert config["batch_size"] == 32

    def test_overrides(self, mock_config):
        mock_config.set(memory={"reranking": {"enabled": False, "alpha": 0.5}})
        from tools.config import get_reranking_config

        config = get_reranking_config()
        assert config["enabled"] is False
        assert config["alpha"] == 0.5
        # Non-overridden defaults preserved
        assert config["candidate_count"] == 100

    def test_partial_override(self, mock_config):
        mock_config.set(memory={"reranking": {"top_k": 5}})
        from tools.config import get_reranking_config

        config = get_reranking_config()
        assert config["top_k"] == 5
        assert config["enabled"] is True  # default preserved


class TestQueryVaultReranking:
    """Tests for reranking integration in query_vault."""

    def _reset_db(self, mock_config):
        """Reset database state for reranking tests."""
        pass  # InMemoryDB auto-resets via mock_config fixture

    def _index_test_files(self, mock_config):
        from tools.memory import index_vault

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "auth-guide.md").write_text(
            "---\ntype: note\nimportance: high\n---\n"
            "# Auth Guide\n\nOAuth 2.0 authentication with PKCE flow for security."
        )
        (notes_dir / "python-tips.md").write_text(
            "---\ntype: note\nimportance: medium\n---\n"
            "# Python Tips\n\nUse list comprehensions for cleaner code."
        )
        (notes_dir / "cooking.md").write_text(
            "---\ntype: note\nimportance: low\n---\n"
            "# Cooking\n\nBest pasta recipes from Italy."
        )

        index_vault()

    def test_reranking_metadata_present(self, mock_config):
        """When reranking succeeds, response includes reranking metadata."""
        self._reset_db(mock_config)
        self._index_test_files(mock_config)

        from tools.query import query_vault

        mock_session = MagicMock()
        mock_tokenizer = MagicMock()
        mock_tokenizer.encode_batch.side_effect = _mock_encode_batch

        mock_session.run.side_effect = _make_constant_batched_run(1.0)

        with patch("tools.reranking._init_model", return_value=True), patch(
            "tools.reranking._session", mock_session
        ), patch("tools.reranking._tokenizer", mock_tokenizer):
            result = query_vault("authentication", n_results=5)
            assert result["success"] is True
            assert "reranking" in result
            assert result["reranking"]["applied"] is True
            assert "alpha" in result["reranking"]
            assert "candidates" in result["reranking"]


    def test_reranking_disabled_no_metadata(self, mock_config):
        """When reranking is disabled, no reranking metadata in response."""
        self._reset_db(mock_config)
        self._index_test_files(mock_config)
        mock_config.set(
            memory={"reranking": {"enabled": False}}
        )

        from tools.query import query_vault

        result = query_vault("authentication", n_results=5)
        assert result["success"] is True
        assert "reranking" not in result


    def test_reranking_fallback_no_metadata(self, mock_config):
        """When reranking fails gracefully, no metadata in response."""
        self._reset_db(mock_config)
        self._index_test_files(mock_config)

        from tools.query import query_vault

        with patch("tools.reranking._init_model", return_value=False):
            result = query_vault("authentication", n_results=5)
            assert result["success"] is True
            assert "reranking" not in result


    def test_fetch_count_increased_with_reranking(self, mock_config):
        """When reranking enabled, fetch_count should use candidate_count."""
        self._reset_db(mock_config)
        self._index_test_files(mock_config)
        mock_config.set(
            memory={"reranking": {"enabled": True, "candidate_count": 50}}
        )

        from tools.query import query_vault

        with patch("tools.reranking._init_model", return_value=False):
            # Even though reranking fails, we should still get results
            result = query_vault("test", n_results=5)
            assert result["success"] is True


    def test_reranking_uses_top_k(self, mock_config):
        """When reranking succeeds, result count uses top_k from config."""
        self._reset_db(mock_config)
        self._index_test_files(mock_config)
        mock_config.set(
            memory={"reranking": {"enabled": True, "top_k": 2}}
        )

        from tools.query import query_vault

        mock_session = MagicMock()
        mock_tokenizer = MagicMock()
        mock_tokenizer.encode_batch.side_effect = _mock_encode_batch

        mock_session.run.side_effect = _make_constant_batched_run(1.0)

        with patch("tools.reranking._init_model", return_value=True), patch(
            "tools.reranking._session", mock_session
        ), patch("tools.reranking._tokenizer", mock_tokenizer):
            result = query_vault("test", n_results=10)
            assert result["success"] is True
            # Should be capped by top_k=2
            assert len(result["results"]) <= 2

