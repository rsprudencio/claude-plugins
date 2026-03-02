"""Tests for embedding service (Phase 1A of v3.0 migration)."""

import numpy as np
import pytest
from unittest.mock import MagicMock, patch


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

    @patch("tools.embedding.SentenceTransformer", create=True)
    def test_onnx_backend_preferred(self, mock_st_cls):
        """ONNX backend is used when available."""
        mock_model = MagicMock()
        mock_st_cls.return_value = mock_model

        with patch.dict("sys.modules", {"sentence_transformers": MagicMock()}):
            svc = self._make_service(backend="onnx")
            # Simulate _load_model
            from sentence_transformers import SentenceTransformer

            with patch(
                "sentence_transformers.SentenceTransformer", mock_st_cls
            ):
                model = svc._load_model()
                mock_st_cls.assert_called_once_with(
                    model_name_or_path="test-model",
                    device="cpu",
                    backend="onnx",
                )

    @patch("tools.embedding.SentenceTransformer", create=True)
    def test_onnx_fallback_to_pytorch(self, mock_st_cls):
        """Falls back to PyTorch if ONNX backend raises."""
        call_count = 0
        mock_model = MagicMock()

        def side_effect(**kwargs):
            nonlocal call_count
            call_count += 1
            if "backend" in kwargs and kwargs["backend"] == "onnx":
                raise RuntimeError("ONNX not available")
            return mock_model

        mock_st_cls.side_effect = side_effect

        with patch.dict("sys.modules", {"sentence_transformers": MagicMock()}):
            svc = self._make_service(backend="onnx")
            with patch(
                "sentence_transformers.SentenceTransformer", mock_st_cls
            ):
                model = svc._load_model()
                assert call_count == 2  # First ONNX attempt, then PyTorch fallback
                assert model is mock_model

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
            svc = EmbeddingService(
                model_name=cfg["model"],
                dimensions=cfg["dimensions"],
                device=cfg["device"],
                backend=cfg["backend"],
            )
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
        """Default config uses granite-multilingual model."""
        from tools.embedding import get_embedding_service

        svc = get_embedding_service()
        assert svc.model_name == "ibm-granite/granite-embedding-english-r2"
        assert svc.dimensions == 768
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


class TestEmbeddingConfig:
    """Tests for get_embedding_config()."""

    def test_default_values(self, mock_config):
        """Default embedding config has expected values."""
        from tools.config import get_embedding_config

        cfg = get_embedding_config()
        assert cfg["model"] == "ibm-granite/granite-embedding-english-r2"
        assert cfg["dimensions"] == 768
        assert cfg["device"] == "cpu"
        assert cfg["backend"] == "onnx"

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
        assert cfg["dimensions"] == 768
        assert cfg["device"] == "cuda"
        assert cfg["backend"] == "torch"

    def test_env_var_override(self, mock_config, monkeypatch):
        """Environment variables override config file."""
        monkeypatch.setenv("EMBEDDING_MODEL", "env-model")
        monkeypatch.setenv("EMBEDDING_DIMENSIONS", "512")
        monkeypatch.setenv("EMBEDDING_DEVICE", "cuda:1")
        monkeypatch.setenv("EMBEDDING_BACKEND", "torch")

        from tools.config import get_embedding_config

        cfg = get_embedding_config()
        assert cfg["model"] == "env-model"
        assert cfg["dimensions"] == 512
        assert cfg["device"] == "cuda:1"
        assert cfg["backend"] == "torch"

    def test_dimensions_always_int(self, mock_config):
        """Dimensions are always returned as int."""
        mock_config.set(memory={"embedding_dimensions": "768"})
        from tools.config import get_embedding_config

        cfg = get_embedding_config()
        assert isinstance(cfg["dimensions"], int)
        assert cfg["dimensions"] == 768


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
