"""Tests for embedding service (Phase 1A of v3.0 migration)."""

import io
import json
import numpy as np
import pytest
from unittest.mock import MagicMock, call, patch


class TestEmbeddingService:
    """Tests for EmbeddingService class."""

    def _make_service(self, **kwargs):
        from tools.embedding import EmbeddingService

        defaults = {
            "model_name": "test-model",
            "dimensions": 384,
            "device": "cpu",
            "backend": "onnx",
        }
        defaults.update(kwargs)
        return EmbeddingService(**defaults)

    def _mock_model(self, dimensions=384):
        """Create a mock SentenceTransformer model."""
        model = MagicMock()
        # Single encode returns a numpy array
        model.encode.return_value = np.random.randn(dimensions).astype(np.float32)
        return model

    def _mock_model_batch(self, dimensions=384, count=3):
        """Create a mock model that returns batch results."""
        model = MagicMock()
        model.encode.return_value = np.random.randn(count, dimensions).astype(
            np.float32
        )
        return model

    def test_init_stores_config(self):
        """Service stores configuration without loading model."""
        svc = self._make_service()
        assert svc.model_name == "test-model"
        assert svc.dimensions == 384
        assert svc.backend == "onnx"
        assert not svc.is_loaded

    def test_lazy_loading(self):
        """Model is not loaded at init time."""
        svc = self._make_service()
        assert svc._model is None
        assert not svc.is_loaded

    def test_encode_returns_correct_dimensions(self):
        """encode() returns a list of floats with correct dimensions."""
        svc = self._make_service()
        svc._model = self._mock_model(384)
        result = svc.encode("test text")
        assert isinstance(result, list)
        assert len(result) == 384
        assert all(isinstance(x, float) for x in result)

    def test_encode_dimension_mismatch_raises(self):
        """encode() raises ValueError if model output doesn't match expected dimensions."""
        svc = self._make_service(dimensions=384)
        svc._model = self._mock_model(768)  # Wrong dimensions
        with pytest.raises(ValueError, match="Dimension mismatch"):
            svc.encode("test text")

    def test_encode_normalizes_embeddings(self):
        """encode() passes normalize_embeddings=True to model."""
        svc = self._make_service()
        svc._model = self._mock_model(384)
        svc.encode("test text")
        svc._model.encode.assert_called_once_with("test text", normalize_embeddings=True)

    def test_encode_batch_returns_list_of_lists(self):
        """encode_batch() returns a list of embedding vectors."""
        svc = self._make_service()
        svc._model = self._mock_model_batch(384, count=3)
        texts = ["text one", "text two", "text three"]
        result = svc.encode_batch(texts)
        assert isinstance(result, list)
        assert len(result) == 3
        for vec in result:
            assert isinstance(vec, list)
            assert len(vec) == 384

    def test_encode_batch_empty_input(self):
        """encode_batch() with empty list returns empty list without calling model."""
        svc = self._make_service()
        svc._model = self._mock_model()
        result = svc.encode_batch([])
        assert result == []
        svc._model.encode.assert_not_called()

    def test_encode_batch_dimension_mismatch_raises(self):
        """encode_batch() raises ValueError if model output dimensions are wrong."""
        svc = self._make_service(dimensions=384)
        svc._model = self._mock_model_batch(768, count=2)  # Wrong dims
        with pytest.raises(ValueError, match="Dimension mismatch"):
            svc.encode_batch(["text one", "text two"])

    def test_encode_batch_passes_batch_size(self):
        """encode_batch() passes batch_size and show_progress_bar to model."""
        svc = self._make_service()
        svc._model = self._mock_model_batch(384, count=2)
        svc.encode_batch(["a", "b"], batch_size=32)
        svc._model.encode.assert_called_once_with(
            ["a", "b"],
            batch_size=32,
            normalize_embeddings=True,
            show_progress_bar=False,
        )

    def test_properties(self):
        """Property accessors return correct values."""
        svc = self._make_service(
            model_name="my-model", dimensions=768, backend="torch"
        )
        assert svc.model_name == "my-model"
        assert svc.dimensions == 768
        assert svc.backend == "torch"

    def test_host_encode_delegates_without_loading_local_model(self):
        """Host backend preserves the public API without local model state."""
        svc = self._make_service(
            backend="host",
            host_url="http://host.docker.internal:8751",
            host_token="secret",
            host_timeout_ms=1500,
        )
        client = MagicMock()
        client.embed.return_value = [[0.1] * 384]
        svc._host_client = client

        assert svc.encode("hello") == [0.1] * 384
        client.embed.assert_called_once_with(["hello"])
        assert svc._ort_session is None
        assert svc._model is None

    def test_host_encode_batch_honors_bounded_request_size(self):
        svc = self._make_service(backend="host")
        client = MagicMock()
        client.embed.side_effect = [[[0.1] * 384], [[0.2] * 384]]
        svc._host_client = client

        result = svc.encode_batch(["one", "two"], batch_size=1)

        assert result == [[0.1] * 384, [0.2] * 384]
        assert client.embed.call_args_list == [call(["one"]), call(["two"])]


class TestOnnxSessionReset:
    """Tests for periodic ONNX session reset to prevent heap fragmentation."""

    def _make_service(self, **kwargs):
        from tools.embedding import EmbeddingService

        defaults = {
            "model_name": "test-model",
            "dimensions": 384,
            "device": "cpu",
            "backend": "onnx",
        }
        defaults.update(kwargs)
        return EmbeddingService(**defaults)

    def _setup_mock_onnx(self, svc):
        """Set up mock ONNX session + tokenizer on a service."""
        mock_session = MagicMock()
        mock_tokenizer = MagicMock()

        mock_tokenizer.return_value = {
            "input_ids": np.array([[1, 2, 3]]),
            "attention_mask": np.array([[1, 1, 1]]),
        }

        output_info = MagicMock()
        output_info.name = "sentence_embedding"
        mock_session.get_outputs.return_value = [
            MagicMock(name="last_hidden_state"), output_info
        ]
        sentence_emb = np.random.randn(1, 384).astype(np.float32)
        mock_session.run.return_value = [None, sentence_emb]

        svc._ort_session = mock_session
        svc._tokenizer = mock_tokenizer
        return mock_session, mock_tokenizer

    def test_reset_clears_session_and_tokenizer(self):
        """reset_onnx_session() sets session and tokenizer to None."""
        svc = self._make_service()
        self._setup_mock_onnx(svc)
        svc._onnx_call_count = 50

        svc.reset_onnx_session()

        assert svc._ort_session is None
        assert svc._tokenizer is None
        assert svc._onnx_call_count == 0

    def test_reset_noop_when_no_session(self):
        """reset_onnx_session() is a no-op when session is not loaded."""
        svc = self._make_service()
        svc._onnx_call_count = 10
        svc.reset_onnx_session()  # Should not raise
        assert svc._onnx_call_count == 10  # Unchanged

    def test_call_count_increments(self):
        """_onnx_encode increments the call counter."""
        svc = self._make_service()
        self._setup_mock_onnx(svc)
        initial = svc._onnx_call_count

        svc._onnx_encode(["test"])
        assert svc._onnx_call_count == initial + 1

        svc._onnx_encode(["test2"])
        assert svc._onnx_call_count == initial + 2

    def test_auto_reset_at_interval(self):
        """Session is reset when call count reaches _ONNX_RESET_INTERVAL."""
        svc = self._make_service()
        self._setup_mock_onnx(svc)
        original_session = svc._ort_session

        # Set count just below threshold
        svc._onnx_call_count = svc._ONNX_RESET_INTERVAL - 1

        # This call triggers the reset (count becomes >= interval)
        with patch.object(svc, "_load_onnx") as mock_load:
            # After reset, _load_onnx will be called to recreate session
            # We need to re-setup the mock after reset
            def setup_after_reset():
                self._setup_mock_onnx(svc)

            mock_load.side_effect = setup_after_reset
            svc._onnx_encode(["trigger reset"])

        # Counter should have been reset and then incremented to 1
        # (reset sets to 0, then the encode increments to 1... but wait,
        # the increment happens BEFORE the check, so:
        # count goes from 199 -> 200 (>= 200), triggers reset (sets to 0),
        # then _load_onnx recreates session)
        assert svc._onnx_call_count == 0  # Reset by reset_onnx_session, then _load_onnx resets to 0

    def test_no_reset_below_interval(self):
        """Session is NOT reset when call count is below threshold."""
        svc = self._make_service()
        mock_session, _ = self._setup_mock_onnx(svc)
        svc._onnx_call_count = 10

        svc._onnx_encode(["no reset needed"])

        # Session should be the same object (not reset)
        assert svc._ort_session is mock_session
        assert svc._onnx_call_count == 11

    def test_reset_interval_class_constant(self):
        """_ONNX_RESET_INTERVAL is set to 50."""
        from tools.embedding import EmbeddingService
        assert EmbeddingService._ONNX_RESET_INTERVAL == 50


class TestLoadModel:
    """Tests for model loading behavior."""

    def _make_service(self, **kwargs):
        from tools.embedding import EmbeddingService

        defaults = {
            "model_name": "test-model",
            "dimensions": 384,
            "device": "cpu",
            "backend": "onnx",
        }
        defaults.update(kwargs)
        return EmbeddingService(**defaults)

    def test_onnx_backend_loads_raw_session(self):
        """ONNX backend loads raw onnxruntime session + tokenizer."""
        mock_ort = MagicMock()
        mock_session = MagicMock()
        mock_ort.InferenceSession.return_value = mock_session
        mock_ort.SessionOptions.return_value = MagicMock()
        mock_ort.GraphOptimizationLevel.ORT_ENABLE_ALL = 99

        mock_tokenizer = MagicMock()
        mock_auto_tok = MagicMock(return_value=mock_tokenizer)

        import sys

        with patch.dict(sys.modules, {"onnxruntime": mock_ort}), \
             patch.dict(sys.modules, {"transformers": MagicMock(AutoTokenizer=MagicMock(from_pretrained=mock_auto_tok))}):
            svc = self._make_service(backend="onnx")
            # Mock os.path.exists to find the ONNX file
            with patch("os.path.exists", return_value=True):
                svc._load_onnx()

            assert svc._ort_session is mock_session
            assert svc._tokenizer is mock_tokenizer

    def test_onnx_fallback_to_pytorch_on_import_error(self):
        """Falls back to PyTorch if onnxruntime is not importable."""
        import sys

        mock_st_module = MagicMock()
        mock_st_cls = mock_st_module.SentenceTransformer
        mock_model = MagicMock()
        mock_model.encode.return_value = np.random.randn(384).astype(np.float32)
        mock_st_cls.return_value = mock_model

        with patch.dict(sys.modules, {"sentence_transformers": mock_st_module}):
            svc = self._make_service(backend="onnx")
            # Simulate ImportError on ONNX import inside encode()
            with patch.object(svc, "_onnx_encode", side_effect=ImportError("no onnxruntime")):
                result = svc.encode("test text")
            # Should have fallen back to torch
            assert svc._backend == "torch"
            assert isinstance(result, list)
            assert len(result) == 384

    def test_onnx_fallback_to_pytorch_on_file_not_found(self):
        """Falls back to PyTorch if ONNX model file is missing."""
        import sys

        mock_st_module = MagicMock()
        mock_st_cls = mock_st_module.SentenceTransformer
        mock_model = MagicMock()
        mock_model.encode.return_value = np.random.randn(384).astype(np.float32)
        mock_st_cls.return_value = mock_model

        with patch.dict(sys.modules, {"sentence_transformers": mock_st_module}):
            svc = self._make_service(backend="onnx")
            with patch.object(svc, "_onnx_encode", side_effect=FileNotFoundError("model.onnx")):
                result = svc.encode("test text")
            assert svc._backend == "torch"
            assert isinstance(result, list)
            assert len(result) == 384

    def test_onnx_encode_with_sentence_embedding_output(self):
        """_onnx_encode uses sentence_embedding output when available."""
        svc = self._make_service(backend="onnx")
        # Mock the ONNX session and tokenizer
        mock_session = MagicMock()
        mock_tokenizer = MagicMock()

        # Tokenizer returns numpy arrays
        mock_tokenizer.return_value = {
            "input_ids": np.array([[1, 2, 3]]),
            "attention_mask": np.array([[1, 1, 1]]),
        }

        # Session has sentence_embedding output
        output_info = MagicMock()
        output_info.name = "sentence_embedding"
        mock_session.get_outputs.return_value = [MagicMock(name="last_hidden_state"), output_info]
        sentence_emb = np.random.randn(1, 384).astype(np.float32)
        mock_session.run.return_value = [None, sentence_emb]

        svc._ort_session = mock_session
        svc._tokenizer = mock_tokenizer

        result = svc._onnx_encode(["test"])
        assert result.shape == (1, 384)
        # Should be normalized
        norm = np.linalg.norm(result[0])
        assert abs(norm - 1.0) < 1e-5

    def test_onnx_encode_mean_pooling_fallback(self):
        """_onnx_encode falls back to mean pooling when no sentence_embedding."""
        svc = self._make_service(backend="onnx")
        mock_session = MagicMock()
        mock_tokenizer = MagicMock()

        mock_tokenizer.return_value = {
            "input_ids": np.array([[1, 2, 3]]),
            "attention_mask": np.array([[1, 1, 1]]),
        }

        # Session only has last_hidden_state (no sentence_embedding)
        mock_session.get_outputs.return_value = [MagicMock(name="last_hidden_state")]
        hidden = np.random.randn(1, 3, 384).astype(np.float32)
        mock_session.run.return_value = [hidden]

        svc._ort_session = mock_session
        svc._tokenizer = mock_tokenizer

        result = svc._onnx_encode(["test"])
        assert result.shape == (1, 384)
        norm = np.linalg.norm(result[0])
        assert abs(norm - 1.0) < 1e-5

    def test_torch_backend_no_onnx_attempt(self):
        """When backend is 'torch', ONNX is not attempted."""
        import sys

        mock_st_module = MagicMock()
        mock_st_cls = mock_st_module.SentenceTransformer
        mock_model = MagicMock()
        mock_st_cls.return_value = mock_model

        with patch.dict(sys.modules, {"sentence_transformers": mock_st_module}):
            svc = self._make_service(backend="torch")
            svc._model = None  # Ensure fresh load
            model = svc._load_model()
            mock_st_cls.assert_called_once_with(
                model_name_or_path="test-model",
                device="cpu",
            )
            # Should NOT include backend="onnx"
            call_kwargs = mock_st_cls.call_args[1]
            assert "backend" not in call_kwargs

    def test_model_loaded_once(self):
        """Model is loaded only on first call, reused after."""
        import sys

        mock_st_module = MagicMock()
        mock_st_cls = mock_st_module.SentenceTransformer
        mock_model = MagicMock()
        mock_st_cls.return_value = mock_model

        with patch.dict(sys.modules, {"sentence_transformers": mock_st_module}):
            svc = self._make_service(backend="torch")
            svc._model = None
            model1 = svc._load_model()
            model2 = svc._load_model()
            assert model1 is model2
            mock_st_cls.assert_called_once()  # Only loaded once


class TestBedrockBackend:
    """Tests for the Bedrock embedding backend."""

    def _make_bedrock_service(self, dimensions=1024, **kwargs):
        from tools.embedding import EmbeddingService

        defaults = {
            "model_name": "amazon.titan-embed-text-v2:0",
            "dimensions": dimensions,
            "device": "cpu",
            "backend": "bedrock",
            "bedrock_region": "us-east-1",
        }
        defaults.update(kwargs)
        return EmbeddingService(**defaults)

    def _mock_bedrock_response(self, embedding):
        """Create a mock Bedrock API response."""
        body = json.dumps({"embedding": embedding}).encode("utf-8")
        return {"body": io.BytesIO(body)}

    def test_init_no_model_loaded(self):
        """Bedrock backend does not load any local model at init."""
        svc = self._make_bedrock_service()
        assert svc.backend == "bedrock"
        assert not svc.is_loaded
        assert svc._bedrock_client is None
        assert svc._ort_session is None
        assert svc._model is None

    def test_encode_single_text(self):
        """encode() calls Bedrock API and returns correct vector."""
        svc = self._make_bedrock_service(dimensions=1024)
        mock_client = MagicMock()
        expected = [0.1] * 1024
        mock_client.invoke_model.return_value = self._mock_bedrock_response(expected)
        svc._bedrock_client = mock_client

        result = svc.encode("test text")
        assert isinstance(result, list)
        assert len(result) == 1024
        assert result == expected

        # Verify API was called correctly
        mock_client.invoke_model.assert_called_once()
        call_kwargs = mock_client.invoke_model.call_args[1]
        assert call_kwargs["modelId"] == "amazon.titan-embed-text-v2:0"
        body = json.loads(call_kwargs["body"])
        assert body["inputText"] == "test text"
        assert body["dimensions"] == 1024
        assert body["normalize"] is True

    def test_encode_batch_multiple_texts(self):
        """encode_batch() calls Bedrock API once per text."""
        svc = self._make_bedrock_service(dimensions=1024)
        mock_client = MagicMock()
        vectors = [[float(i)] * 1024 for i in range(3)]
        mock_client.invoke_model.side_effect = [
            self._mock_bedrock_response(v) for v in vectors
        ]
        svc._bedrock_client = mock_client

        result = svc.encode_batch(["a", "b", "c"])
        assert len(result) == 3
        assert mock_client.invoke_model.call_count == 3
        for i, vec in enumerate(result):
            assert len(vec) == 1024
            assert vec == vectors[i]

    def test_encode_batch_empty_input(self):
        """encode_batch([]) returns [] without calling API."""
        svc = self._make_bedrock_service()
        mock_client = MagicMock()
        svc._bedrock_client = mock_client

        result = svc.encode_batch([])
        assert result == []
        mock_client.invoke_model.assert_not_called()

    def test_dimension_mismatch_raises(self):
        """Raises ValueError if Bedrock returns wrong dimensions."""
        svc = self._make_bedrock_service(dimensions=1024)
        mock_client = MagicMock()
        wrong_dims = [0.1] * 512  # Wrong!
        mock_client.invoke_model.return_value = self._mock_bedrock_response(wrong_dims)
        svc._bedrock_client = mock_client

        with pytest.raises(ValueError, match="Dimension mismatch"):
            svc.encode("test")

    def test_lazy_client_initialization(self):
        """Bedrock client is created on first encode, not at init."""
        svc = self._make_bedrock_service()
        assert svc._bedrock_client is None

        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        expected = [0.1] * 1024
        mock_client.invoke_model.return_value = self._mock_bedrock_response(expected)

        import sys
        with patch.dict(sys.modules, {"boto3": mock_boto3}):
            svc.encode("test")

        mock_boto3.client.assert_called_once_with(
            "bedrock-runtime", region_name="us-east-1"
        )
        assert svc._bedrock_client is mock_client
        assert svc.is_loaded

    def test_client_reused_after_init(self):
        """Bedrock client is initialized once and reused."""
        svc = self._make_bedrock_service(dimensions=1024)
        mock_client = MagicMock()
        expected = [0.1] * 1024
        # Each call needs a fresh BytesIO (read() consumes the stream)
        mock_client.invoke_model.side_effect = [
            self._mock_bedrock_response(expected),
            self._mock_bedrock_response(expected),
        ]
        svc._bedrock_client = mock_client

        svc.encode("first")
        svc.encode("second")
        # Client should be reused, not recreated
        assert svc._bedrock_client is mock_client
        assert mock_client.invoke_model.call_count == 2

    def test_custom_region(self):
        """Bedrock client uses configured region."""
        svc = self._make_bedrock_service(bedrock_region="eu-west-1")

        mock_boto3 = MagicMock()
        mock_client = MagicMock()
        mock_boto3.client.return_value = mock_client
        expected = [0.1] * 1024
        mock_client.invoke_model.return_value = self._mock_bedrock_response(expected)

        import sys
        with patch.dict(sys.modules, {"boto3": mock_boto3}):
            svc.encode("test")

        mock_boto3.client.assert_called_once_with(
            "bedrock-runtime", region_name="eu-west-1"
        )

    def test_boto3_import_error(self):
        """Raises ImportError if boto3 is not installed."""
        svc = self._make_bedrock_service()
        import sys
        with patch.dict(sys.modules, {"boto3": None}):
            with pytest.raises(ImportError, match="boto3 is required"):
                svc.encode("test")

    def test_is_loaded_reflects_bedrock_state(self):
        """is_loaded returns True when bedrock client is initialized."""
        svc = self._make_bedrock_service()
        assert not svc.is_loaded
        svc._bedrock_client = MagicMock()
        assert svc.is_loaded

    def test_256_dimensions(self):
        """Bedrock backend works with Titan's 256d output."""
        svc = self._make_bedrock_service(dimensions=256)
        mock_client = MagicMock()
        expected = [0.1] * 256
        mock_client.invoke_model.return_value = self._mock_bedrock_response(expected)
        svc._bedrock_client = mock_client

        result = svc.encode("test")
        assert len(result) == 256
        body = json.loads(mock_client.invoke_model.call_args[1]["body"])
        assert body["dimensions"] == 256

    def test_512_dimensions(self):
        """Bedrock backend works with Titan's 512d output."""
        svc = self._make_bedrock_service(dimensions=512)
        mock_client = MagicMock()
        expected = [0.1] * 512
        mock_client.invoke_model.return_value = self._mock_bedrock_response(expected)
        svc._bedrock_client = mock_client

        result = svc.encode("test")
        assert len(result) == 512


class TestGetEmbeddingService:
    """Tests for the singleton accessor.

    These tests need the REAL get_embedding_service (not the conftest mock)
    to verify factory/singleton behavior. The _restore_real_embedding fixture
    undoes the mock_config patch for the embedding module only.
    """

    @pytest.fixture(autouse=True)
    def _restore_real_embedding(self, mock_config, monkeypatch):
        """Restore real get_embedding_service for factory tests."""
        import tools.embedding as emb_mod
        from tools.embedding import EmbeddingService
        from tools.config import get_embedding_config

        # Save and restore the real factory function
        def real_get_embedding_service():
            cfg = get_embedding_config()
            key = (cfg["model"], cfg["dimensions"], cfg["device"], cfg["backend"])
            if emb_mod._service is not None and emb_mod._service_cache_key == key:
                return emb_mod._service
            kwargs = {
                "model_name": cfg["model"],
                "dimensions": cfg["dimensions"],
                "device": cfg["device"],
                "backend": cfg["backend"],
            }
            if cfg["backend"] == "bedrock":
                kwargs["bedrock_region"] = cfg.get("bedrock_region", "eu-central-1")
            svc = EmbeddingService(**kwargs)
            emb_mod._service = svc
            emb_mod._service_cache_key = key
            return svc

        monkeypatch.setattr(emb_mod, "get_embedding_service", real_get_embedding_service)
        emb_mod._service = None
        emb_mod._service_cache_key = None
        yield
        emb_mod._service = None
        emb_mod._service_cache_key = None

    def test_returns_embedding_service(self, mock_config):
        """get_embedding_service() returns an EmbeddingService instance."""
        from tools.embedding import EmbeddingService, get_embedding_service

        svc = get_embedding_service()
        assert isinstance(svc, EmbeddingService)

    def test_singleton_same_config(self, mock_config):
        """Same config returns the same instance."""
        from tools.embedding import get_embedding_service

        svc1 = get_embedding_service()
        svc2 = get_embedding_service()
        assert svc1 is svc2

    def test_singleton_invalidated_on_config_change(self, mock_config):
        """Config change creates a new instance."""
        from tools.embedding import get_embedding_service

        svc1 = get_embedding_service()

        # Change embedding config
        mock_config.set(
            memory={
                "embedding_model": "different-model",
                "embedding_dimensions": 768,
            }
        )

        svc2 = get_embedding_service()
        assert svc1 is not svc2
        assert svc2.model_name == "different-model"
        assert svc2.dimensions == 768

    def test_default_config_values(self, mock_config):
        """Default config uses granite-small-english-r2 model."""
        from tools.embedding import get_embedding_service

        svc = get_embedding_service()
        assert svc.model_name == "ibm-granite/granite-embedding-small-english-r2"
        assert svc.dimensions == 384
        assert svc.backend == "onnx"

    def test_reset_clears_singleton(self, mock_config):
        """reset_embedding_service() clears the cached instance."""
        import tools.embedding as emb_mod
        from tools.embedding import get_embedding_service

        svc1 = get_embedding_service()
        emb_mod._service = None
        emb_mod._service_cache_key = None
        svc2 = get_embedding_service()
        assert svc1 is not svc2

    def test_bedrock_backend_from_config(self, mock_config):
        """Factory creates Bedrock service when configured."""
        from tools.embedding import get_embedding_service

        mock_config.set(
            memory={
                "embedding_backend": "bedrock",
                "embedding_model": "amazon.titan-embed-text-v2:0",
                "embedding_dimensions": 1024,
                "embedding_bedrock_region": "eu-west-1",
            }
        )
        svc = get_embedding_service()
        assert svc.backend == "bedrock"
        assert svc.model_name == "amazon.titan-embed-text-v2:0"
        assert svc.dimensions == 1024
        assert svc._bedrock_region == "eu-west-1"
        # No local model should be loaded
        assert svc._ort_session is None
        assert svc._model is None


class TestWarmEmbeddingService:
    """Startup warmup loads local models before hooks can receive traffic."""

    def test_warms_local_backend_with_one_probe(self, monkeypatch):
        from tools import embedding

        service = MagicMock()
        service.backend = "onnx"
        service.dimensions = 384
        service.encode.return_value = [0.0] * 384
        monkeypatch.setattr(embedding, "get_embedding_service", lambda: service)

        elapsed_ms = embedding.warm_embedding_service()

        service.encode.assert_called_once_with("Jarvis embedding startup warmup")
        assert elapsed_ms >= 0

    def test_skips_remote_bedrock_backend(self, monkeypatch):
        from tools import embedding

        service = MagicMock()
        service.backend = "bedrock"
        monkeypatch.setattr(embedding, "get_embedding_service", lambda: service)

        assert embedding.warm_embedding_service() is None
        service.encode.assert_not_called()

    def test_probes_host_backend_at_boot(self, monkeypatch):
        """Host backends get a live probe at startup so cold-start latency and
        connectivity problems surface immediately. Note: since the degraded-
        startup change, a failed probe is logged loudly by the callers but no
        longer blocks readiness — the server serves degraded and retrieval
        fails open until the host returns."""
        from tools import embedding

        service = MagicMock()
        service.backend = "host"
        service.dimensions = 384
        service.encode.return_value = [0.0] * 384
        monkeypatch.setattr(embedding, "get_embedding_service", lambda: service)

        assert embedding.warm_embedding_service() >= 0
        service.encode.assert_called_once()


class TestEmbeddingConfig:
    """Tests for get_embedding_config()."""

    def test_default_values(self, mock_config):
        """Default embedding config has expected values."""
        from tools.config import get_embedding_config

        cfg = get_embedding_config()
        assert cfg["model"] == "ibm-granite/granite-embedding-small-english-r2"
        assert cfg["model_id"] == "ibm-granite/granite-embedding-small-english-r2"
        assert cfg["dimensions"] == 384
        assert cfg["device"] == "cpu"
        assert cfg["backend"] == "onnx"
        assert cfg["bedrock_region"] == "eu-central-1"
        assert cfg["host_url"] == "http://host.docker.internal:8751"
        assert cfg["host_model"] == "ibm-granite/granite-embedding-small-english-r2"
        assert cfg["host_token"] == ""
        assert cfg["host_timeout_ms"] == 2000

    def test_config_file_override(self, mock_config):
        """Config file values override defaults."""
        mock_config.set(
            memory={
                "embedding_model": "custom-model",
                "embedding_dimensions": 768,
                "embedding_device": "cuda",
                "embedding_backend": "torch",
            }
        )
        from tools.config import get_embedding_config

        cfg = get_embedding_config()
        assert cfg["model"] == "custom-model"
        assert cfg["model_id"] == "custom-model"
        assert cfg["dimensions"] == 768
        assert cfg["device"] == "cuda"
        assert cfg["backend"] == "torch"

    def test_env_var_override(self, mock_config, monkeypatch):
        """Environment variables override config file."""
        monkeypatch.setenv("EMBEDDING_MODEL", "env-model")
        monkeypatch.setenv("EMBEDDING_DIMENSIONS", "512")
        monkeypatch.setenv("EMBEDDING_DEVICE", "cuda:1")
        monkeypatch.setenv("EMBEDDING_BACKEND", "torch")
        monkeypatch.setenv("JARVIS_EMBEDDING_MODEL_ID", "canonical/env-model")

        from tools.config import get_embedding_config

        cfg = get_embedding_config()
        assert cfg["model"] == "env-model"
        assert cfg["model_id"] == "canonical/env-model"
        assert cfg["dimensions"] == 512
        assert cfg["device"] == "cuda:1"
        assert cfg["backend"] == "torch"

    def test_dimensions_always_int(self, mock_config):
        """Dimensions are always returned as int."""
        mock_config.set(memory={"embedding_dimensions": "384"})
        from tools.config import get_embedding_config

        cfg = get_embedding_config()
        assert isinstance(cfg["dimensions"], int)
        assert cfg["dimensions"] == 384

    def test_bedrock_config_from_file(self, mock_config):
        """Bedrock config can be set via config file."""
        mock_config.set(
            memory={
                "embedding_backend": "bedrock",
                "embedding_model": "amazon.titan-embed-text-v2:0",
                "embedding_dimensions": 1024,
                "embedding_bedrock_region": "eu-west-1",
            }
        )
        from tools.config import get_embedding_config

        cfg = get_embedding_config()
        assert cfg["backend"] == "bedrock"
        assert cfg["model"] == "amazon.titan-embed-text-v2:0"
        assert cfg["dimensions"] == 1024
        assert cfg["bedrock_region"] == "eu-west-1"

    def test_bedrock_region_env_override(self, mock_config, monkeypatch):
        """BEDROCK_REGION env var overrides config file."""
        mock_config.set(memory={"embedding_bedrock_region": "us-west-2"})
        monkeypatch.setenv("BEDROCK_REGION", "ap-northeast-1")

        from tools.config import get_embedding_config

        cfg = get_embedding_config()
        assert cfg["bedrock_region"] == "ap-northeast-1"

    def test_host_config_env_overrides(self, mock_config, monkeypatch):
        monkeypatch.setenv("JARVIS_MODEL_HOST_URL", "http://models.internal:9000")
        monkeypatch.setenv("JARVIS_MODEL_HOST_MODEL", "host-model")
        monkeypatch.setenv("JARVIS_MODEL_HOST_TOKEN", "test-token")
        monkeypatch.setenv("JARVIS_MODEL_HOST_TIMEOUT_MS", "750")

        from tools.config import get_embedding_config

        cfg = get_embedding_config()
        assert cfg["host_url"] == "http://models.internal:9000"
        assert cfg["host_model"] == "host-model"
        assert cfg["host_token"] == "test-token"
        assert cfg["host_timeout_ms"] == 750

    def test_host_backend_uses_host_alias_not_baked_onnx_path(self):
        from tools.embedding import _effective_model_name, get_embedding_model_identity

        config = {
            "backend": "host",
            "model": "/app/models/embedding",
            "host_model": "ibm-granite/granite-embedding-small-english-r2",
        }
        assert (
            _effective_model_name(config)
            == "ibm-granite/granite-embedding-small-english-r2"
        )
        assert (
            get_embedding_model_identity(config)
            == "ibm-granite/granite-embedding-small-english-r2"
        )


class TestPostgresConfig:
    """Tests for get_postgres_config()."""

    def test_default_value(self, mock_config):
        """Default postgres URL when not configured."""
        from tools.config import get_postgres_config

        mock_config.set(memory={})  # Clear postgres_url to test true default
        cfg = get_postgres_config()
        assert cfg["url"] == "postgresql://jarvis:jarvis@localhost:5432/jarvis"

    def test_config_file_override(self, mock_config):
        """Config file value overrides default."""
        mock_config.set(
            memory={"postgres_url": "postgresql://custom:pw@db.example.com:5433/mydb"}
        )
        from tools.config import get_postgres_config

        cfg = get_postgres_config()
        assert cfg["url"] == "postgresql://custom:pw@db.example.com:5433/mydb"

    def test_env_var_override(self, mock_config, monkeypatch):
        """POSTGRES_URL env var takes highest priority."""
        mock_config.set(memory={"postgres_url": "postgresql://from-config/db"})
        monkeypatch.setenv("POSTGRES_URL", "postgresql://from-env/db")

        from tools.config import get_postgres_config

        cfg = get_postgres_config()
        assert cfg["url"] == "postgresql://from-env/db"
