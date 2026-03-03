"""Tests for jarvis_meta CRUD and model consistency checking."""

import os
import pytest
from unittest.mock import patch


class TestMetaCRUD:
    """Tests for get_meta, set_meta, get_all_meta."""

    def test_set_and_get(self, mock_config):
        """set_meta stores a value, get_meta retrieves it."""
        from tools.schema import set_meta, get_meta

        set_meta("test_key", {"foo": "bar", "num": 42})
        result = get_meta("test_key")
        assert result == {"foo": "bar", "num": 42}

    def test_get_nonexistent(self, mock_config):
        """get_meta returns None for missing keys."""
        from tools.schema import get_meta

        result = get_meta("nonexistent_key")
        assert result is None

    def test_upsert_overwrites(self, mock_config):
        """set_meta overwrites existing values."""
        from tools.schema import set_meta, get_meta

        set_meta("key1", {"version": 1})
        set_meta("key1", {"version": 2, "extra": True})
        result = get_meta("key1")
        assert result == {"version": 2, "extra": True}

    def test_get_all_empty(self, mock_config):
        """get_all_meta returns empty dict when no rows exist."""
        from tools.schema import get_all_meta

        result = get_all_meta()
        assert result == {}

    def test_get_all_populated(self, mock_config):
        """get_all_meta returns all stored key-value pairs."""
        from tools.schema import set_meta, get_all_meta

        set_meta("key_a", {"a": 1})
        set_meta("key_b", {"b": 2})
        result = get_all_meta()
        assert len(result) == 2
        assert result["key_a"] == {"a": 1}
        assert result["key_b"] == {"b": 2}

    def test_complex_jsonb(self, mock_config):
        """set_meta handles nested JSONB values."""
        from tools.schema import set_meta, get_meta

        complex_val = {
            "peers": [
                {"node_id": "node-1", "url": "postgres://host1/jarvis"},
                {"node_id": "node-2", "url": "postgres://host2/jarvis"},
            ],
            "config": {"mode": "bidirectional", "slots": 10},
        }
        set_meta("replication_state", complex_val)
        result = get_meta("replication_state")
        assert result["peers"][0]["node_id"] == "node-1"
        assert result["config"]["slots"] == 10


class TestModelConsistency:
    """Tests for check_model_consistency."""

    def test_first_run_records(self, mock_config):
        """First run records embedding config in jarvis_meta."""
        from tools.schema import check_model_consistency, get_meta

        check_model_consistency()

        stored = get_meta("embedding_config")
        assert stored is not None
        assert stored["dimensions"] == 384
        assert stored["vector_type"] == "halfvec"
        # Model comes from get_embedding_config default
        assert "granite" in stored["model"].lower() or stored["model"] != ""

        schema_ver = get_meta("schema_version")
        assert schema_ver == {"version": 6}

    def test_matching_passes(self, mock_config):
        """Matching config passes without error."""
        from tools.schema import check_model_consistency

        # First run records
        check_model_consistency()
        # Second run validates — should pass
        check_model_consistency()

    def test_model_mismatch_raises(self, mock_config):
        """Mismatched model name raises ModelMismatchError."""
        from tools.schema import check_model_consistency, set_meta, ModelMismatchError

        # Record a different model
        set_meta("embedding_config", {
            "model": "old-model/v1",
            "dimensions": 384,
        })

        with pytest.raises(ModelMismatchError, match="model mismatch"):
            check_model_consistency()

    def test_dimensions_mismatch_raises(self, mock_config):
        """Mismatched dimensions raises ModelMismatchError."""
        from tools.schema import check_model_consistency, set_meta, ModelMismatchError

        # Record with old dimensions (768) which mismatches new default (384)
        set_meta("embedding_config", {
            "model": "ibm-granite/granite-embedding-small-english-r2",
            "dimensions": 768,
        })

        with pytest.raises(ModelMismatchError, match="dimensions mismatch"):
            check_model_consistency()

    def test_skip_env_var(self, mock_config, monkeypatch):
        """JARVIS_SKIP_MODEL_CHECK=1 bypasses the check."""
        from tools.schema import check_model_consistency, set_meta

        # Record a mismatched model
        set_meta("embedding_config", {
            "model": "completely-different-model",
            "dimensions": 999,
        })

        monkeypatch.setenv("JARVIS_SKIP_MODEL_CHECK", "1")
        # Should not raise
        check_model_consistency()

    def test_schema_version_recorded(self, mock_config):
        """First run records schema_version alongside embedding_config."""
        from tools.schema import check_model_consistency, get_meta

        check_model_consistency()
        sv = get_meta("schema_version")
        assert sv is not None
        assert sv["version"] == 6

    def test_vector_type_recorded(self, mock_config):
        """First run records vector_type: halfvec in embedding_config."""
        from tools.schema import check_model_consistency, get_meta

        check_model_consistency()
        stored = get_meta("embedding_config")
        assert stored["vector_type"] == "halfvec"
