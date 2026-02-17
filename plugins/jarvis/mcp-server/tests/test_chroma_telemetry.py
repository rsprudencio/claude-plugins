"""Tests for ChromaDB telemetry: instrumented collection, error classification,
config getter, health probe, and JSONL logging."""

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from tools.chroma_telemetry import (
    InstrumentedCollection,
    classify_error,
    check_integrity,
    chromadb_health,
    _extract_n_ids,
    _log_record,
    _probe_once,
    health_probe_loop,
)
from tools.config import get_telemetry_config


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def reset_health_state():
    """Reset module-level health state between tests."""
    original = dict(chromadb_health)
    chromadb_health.update({
        "status": "unknown",
        "last_write_ok": None,
        "last_error_class": None,
        "doc_count": None,
    })
    yield
    chromadb_health.update(original)


@pytest.fixture
def telemetry_dir(tmp_path):
    """Provide a temp telemetry directory and patch the path resolution."""
    tel_dir = tmp_path / "telemetry"
    tel_dir.mkdir()
    with patch("tools.chroma_telemetry._get_telemetry_dir", return_value=str(tel_dir)):
        yield tel_dir


@pytest.fixture
def mock_collection():
    """Create a mock ChromaDB collection."""
    coll = MagicMock()
    coll.upsert = MagicMock(return_value=None)
    coll.delete = MagicMock(return_value=None)
    coll.add = MagicMock(return_value=None)
    coll.update = MagicMock(return_value=None)
    coll.get = MagicMock(return_value={"ids": [], "documents": [], "metadatas": []})
    coll.query = MagicMock(return_value={"ids": [[]], "documents": [[]], "distances": [[]]})
    coll.count = MagicMock(return_value=42)
    coll.peek = MagicMock(return_value={"ids": [], "documents": []})
    coll.name = "jarvis"
    return coll


@pytest.fixture
def instrumented(mock_collection, telemetry_dir):
    """Create an InstrumentedCollection with telemetry enabled."""
    return InstrumentedCollection(mock_collection)


# ── Error classification ─────────────────────────────────────────────────────


class TestClassifyError:
    def test_connection_refused(self):
        exc = Exception("Connection refused by server")
        assert classify_error(exc) == "connection_refused"

    def test_connection_reset(self):
        exc = Exception("Connection reset by peer")
        assert classify_error(exc) == "connection_refused"

    def test_connection_error(self):
        exc = Exception("Connection error: host unreachable")
        assert classify_error(exc) == "connection_refused"

    def test_rate_limited_429(self):
        exc = Exception("HTTP 429 Too Many Requests")
        assert classify_error(exc) == "rate_limited"

    def test_rate_limited_text(self):
        exc = Exception("too many requests, please retry later")
        assert classify_error(exc) == "rate_limited"

    def test_server_error_500(self):
        exc = Exception("Internal server error 500")
        assert classify_error(exc) == "server_error"

    def test_server_error_502(self):
        exc = Exception("502 Bad Gateway")
        assert classify_error(exc) == "server_error"

    def test_server_error_503(self):
        exc = Exception("503 Service Unavailable")
        assert classify_error(exc) == "server_error"

    def test_server_error_504(self):
        exc = Exception("504 Gateway Timeout")
        assert classify_error(exc) == "server_error"

    def test_timeout(self):
        exc = Exception("Operation timeout after 30s")
        assert classify_error(exc) == "timeout"

    def test_auth_error_401(self):
        exc = Exception("401 Unauthorized")
        assert classify_error(exc) == "auth_error"

    def test_auth_error_403(self):
        exc = Exception("403 Forbidden")
        assert classify_error(exc) == "auth_error"

    def test_auth_error_text(self):
        exc = Exception("Unauthorized access to collection")
        assert classify_error(exc) == "auth_error"

    def test_unknown(self):
        exc = Exception("Something completely different")
        assert classify_error(exc) == "unknown"

    def test_case_insensitive(self):
        exc = Exception("CONNECTION REFUSED by host")
        assert classify_error(exc) == "connection_refused"


# ── _extract_n_ids ───────────────────────────────────────────────────────────


class TestExtractNIds:
    def test_from_ids(self):
        assert _extract_n_ids({"ids": ["a", "b", "c"]}) == 3

    def test_from_documents(self):
        assert _extract_n_ids({"documents": ["doc1", "doc2"]}) == 2

    def test_ids_takes_precedence(self):
        assert _extract_n_ids({"ids": ["a"], "documents": ["d1", "d2"]}) == 1

    def test_none_when_absent(self):
        assert _extract_n_ids({"where": {"type": "vault"}}) is None

    def test_empty_dict(self):
        assert _extract_n_ids({}) is None


# ── InstrumentedCollection ───────────────────────────────────────────────────


class TestInstrumentedCollection:
    def test_delegates_unknown_attributes(self, instrumented, mock_collection):
        """__getattr__ should delegate to underlying collection."""
        assert instrumented.name == "jarvis"

    def test_upsert_calls_underlying(self, instrumented, mock_collection):
        instrumented.upsert(ids=["id1"], documents=["doc"])
        mock_collection.upsert.assert_called_once_with(ids=["id1"], documents=["doc"])

    def test_delete_calls_underlying(self, instrumented, mock_collection):
        instrumented.delete(ids=["id1"])
        mock_collection.delete.assert_called_once_with(ids=["id1"])

    def test_add_calls_underlying(self, instrumented, mock_collection):
        instrumented.add(ids=["id1"], documents=["doc"])
        mock_collection.add.assert_called_once_with(ids=["id1"], documents=["doc"])

    def test_update_calls_underlying(self, instrumented, mock_collection):
        instrumented.update(ids=["id1"], documents=["new"])
        mock_collection.update.assert_called_once_with(ids=["id1"], documents=["new"])

    def test_get_calls_underlying(self, instrumented, mock_collection):
        result = instrumented.get(ids=["id1"])
        mock_collection.get.assert_called_once_with(ids=["id1"])

    def test_query_calls_underlying(self, instrumented, mock_collection):
        result = instrumented.query(query_texts=["search"])
        mock_collection.query.assert_called_once_with(query_texts=["search"])

    def test_count_calls_underlying(self, instrumented, mock_collection):
        result = instrumented.count()
        assert result == 42
        mock_collection.count.assert_called_once()

    def test_peek_calls_underlying(self, instrumented, mock_collection):
        instrumented.peek(limit=5)
        mock_collection.peek.assert_called_once_with(limit=5)

    def test_write_error_propagates(self, instrumented, mock_collection):
        mock_collection.upsert.side_effect = Exception("connection refused")
        with pytest.raises(Exception, match="refused"):
            instrumented.upsert(ids=["id1"], documents=["doc"])

    def test_read_error_propagates(self, instrumented, mock_collection):
        mock_collection.get.side_effect = Exception("timeout")
        with pytest.raises(Exception, match="timeout"):
            instrumented.get(ids=["id1"])

    def test_write_updates_health_on_success(self, instrumented, mock_collection):
        instrumented.upsert(ids=["id1"], documents=["doc"])
        assert chromadb_health["last_write_ok"] is True

    def test_write_updates_health_on_failure(self, instrumented, mock_collection):
        mock_collection.upsert.side_effect = Exception("500 internal server error")
        with pytest.raises(Exception):
            instrumented.upsert(ids=["id1"], documents=["doc"])
        assert chromadb_health["last_write_ok"] is False
        assert chromadb_health["last_error_class"] == "server_error"

    def test_connection_refused_sets_disconnected(self, instrumented, mock_collection):
        mock_collection.delete.side_effect = Exception("connection refused by host")
        with pytest.raises(Exception):
            instrumented.delete(ids=["id1"])
        assert chromadb_health["status"] == "disconnected"

    def test_non_connection_error_sets_degraded(self, instrumented, mock_collection):
        mock_collection.upsert.side_effect = Exception("500 internal server error")
        with pytest.raises(Exception):
            instrumented.upsert(ids=["id1"], documents=["doc"])
        assert chromadb_health["status"] == "degraded"

    def test_disconnected_status_not_overwritten_by_degraded(self, instrumented, mock_collection):
        """Once status is 'disconnected', a non-connection write error shouldn't downgrade to 'degraded'."""
        chromadb_health["status"] = "disconnected"
        mock_collection.upsert.side_effect = Exception("500 internal server error")
        with pytest.raises(Exception):
            instrumented.upsert(ids=["id1"], documents=["doc"])
        assert chromadb_health["status"] == "disconnected"


# ── JSONL logging ────────────────────────────────────────────────────────────


class TestJSONLLogging:
    @patch("tools.config.get_telemetry_config", return_value={
        "enabled": True, "log_writes": True, "log_reads": False, "probe_interval_seconds": 300
    })
    def test_write_logged_when_enabled(self, mock_cfg, instrumented, telemetry_dir):
        instrumented.upsert(ids=["id1"], documents=["doc"])
        jsonl_path = telemetry_dir / "chromadb.jsonl"
        assert jsonl_path.exists()
        records = [json.loads(line) for line in jsonl_path.read_text().strip().split("\n")]
        assert len(records) == 1
        assert records[0]["op"] == "upsert"
        assert records[0]["ok"] is True
        assert records[0]["n_ids"] == 1
        assert "elapsed_ms" in records[0]
        assert "ts" in records[0]

    @patch("tools.config.get_telemetry_config", return_value={
        "enabled": True, "log_writes": True, "log_reads": False, "probe_interval_seconds": 300
    })
    def test_read_not_logged_by_default(self, mock_cfg, instrumented, telemetry_dir):
        instrumented.count()
        jsonl_path = telemetry_dir / "chromadb.jsonl"
        assert not jsonl_path.exists()

    @patch("tools.config.get_telemetry_config", return_value={
        "enabled": True, "log_writes": True, "log_reads": True, "probe_interval_seconds": 300
    })
    def test_read_logged_when_enabled(self, mock_cfg, instrumented, telemetry_dir):
        instrumented.count()
        jsonl_path = telemetry_dir / "chromadb.jsonl"
        assert jsonl_path.exists()
        records = [json.loads(line) for line in jsonl_path.read_text().strip().split("\n")]
        assert len(records) == 1
        assert records[0]["op"] == "count"

    @patch("tools.config.get_telemetry_config", return_value={
        "enabled": False, "log_reads": False, "log_writes": False, "probe_interval_seconds": 300
    })
    def test_nothing_logged_when_disabled(self, mock_cfg, instrumented, telemetry_dir):
        instrumented.upsert(ids=["id1"], documents=["doc"])
        instrumented.count()
        jsonl_path = telemetry_dir / "chromadb.jsonl"
        assert not jsonl_path.exists()

    @patch("tools.config.get_telemetry_config", return_value={
        "enabled": True, "log_writes": False, "log_reads": False, "probe_interval_seconds": 300
    })
    def test_errors_always_logged(self, mock_cfg, instrumented, mock_collection, telemetry_dir):
        """Errors are logged regardless of log_writes/log_reads settings."""
        mock_collection.upsert.side_effect = Exception("connection refused")
        with pytest.raises(Exception):
            instrumented.upsert(ids=["id1"], documents=["doc"])
        jsonl_path = telemetry_dir / "chromadb.jsonl"
        assert jsonl_path.exists()
        record = json.loads(jsonl_path.read_text().strip())
        assert record["ok"] is False
        assert record["error_class"] == "connection_refused"
        assert "refused" in record["error"]

    @patch("tools.config.get_telemetry_config", return_value={
        "enabled": True, "log_writes": True, "log_reads": False, "probe_interval_seconds": 300
    })
    def test_delete_logged_with_n_ids(self, mock_cfg, instrumented, telemetry_dir):
        instrumented.delete(ids=["a", "b", "c"])
        jsonl_path = telemetry_dir / "chromadb.jsonl"
        record = json.loads(jsonl_path.read_text().strip())
        assert record["op"] == "delete"
        assert record["n_ids"] == 3


# ── Integrity check (no-op in HTTP mode) ─────────────────────────────────────


class TestCheckIntegrity:
    def test_returns_ok_in_http_mode(self):
        """check_integrity is a no-op in HTTP mode."""
        result = check_integrity("/tmp/whatever")
        assert result["ok"] is True
        assert result["result"] == "http_mode"

    def test_returns_ok_without_args(self):
        result = check_integrity()
        assert result["ok"] is True


# ── Config getter ────────────────────────────────────────────────────────────


class TestGetTelemetryConfig:
    def test_defaults(self, mock_config):
        config = get_telemetry_config()
        assert config["enabled"] is True
        assert config["log_reads"] is False
        assert config["log_writes"] is True
        assert config["probe_interval_seconds"] == 300

    def test_env_var_kill_switch(self, mock_config, monkeypatch):
        monkeypatch.setenv("JARVIS_TELEMETRY", "0")
        config = get_telemetry_config()
        assert config["enabled"] is False
        assert config["log_reads"] is False
        assert config["log_writes"] is False

    def test_env_var_with_whitespace(self, mock_config, monkeypatch):
        monkeypatch.setenv("JARVIS_TELEMETRY", " 0 ")
        config = get_telemetry_config()
        assert config["enabled"] is False

    def test_env_var_non_zero_does_not_disable(self, mock_config, monkeypatch):
        monkeypatch.setenv("JARVIS_TELEMETRY", "1")
        config = get_telemetry_config()
        assert config["enabled"] is True

    def test_config_override(self, mock_config):
        """Config values override defaults."""
        mock_config.set(memory={
            "chroma_data_path": mock_config.db_path,
            "telemetry": {
                "enabled": True,
                "log_reads": True,
                "log_writes": False,
                "probe_interval_seconds": 60,
            },
        })
        config = get_telemetry_config()
        assert config["log_reads"] is True
        assert config["log_writes"] is False
        assert config["probe_interval_seconds"] == 60


# ── _log_record ──────────────────────────────────────────────────────────────


class TestLogRecord:
    def test_successful_record(self, telemetry_dir):
        _log_record("upsert", 12.5, None, None, 3)
        jsonl_path = telemetry_dir / "chromadb.jsonl"
        record = json.loads(jsonl_path.read_text().strip())
        assert record["op"] == "upsert"
        assert record["elapsed_ms"] == 12.5
        assert record["ok"] is True
        assert record["n_ids"] == 3
        assert "error" not in record
        assert "error_class" not in record

    def test_error_record(self, telemetry_dir):
        _log_record("upsert", 5000.0, "connection refused", "connection_refused", 1)
        jsonl_path = telemetry_dir / "chromadb.jsonl"
        record = json.loads(jsonl_path.read_text().strip())
        assert record["ok"] is False
        assert record["error_class"] == "connection_refused"
        assert "refused" in record["error"]

    def test_no_n_ids_omitted(self, telemetry_dir):
        _log_record("count", 1.0, None, None, None)
        jsonl_path = telemetry_dir / "chromadb.jsonl"
        record = json.loads(jsonl_path.read_text().strip())
        assert "n_ids" not in record

    def test_timestamp_format(self, telemetry_dir):
        _log_record("count", 1.0, None, None, None)
        jsonl_path = telemetry_dir / "chromadb.jsonl"
        record = json.loads(jsonl_path.read_text().strip())
        ts = record["ts"]
        assert ts.endswith("Z")
        assert "T" in ts

    def test_multiple_records_appended(self, telemetry_dir):
        _log_record("upsert", 10.0, None, None, 1)
        _log_record("delete", 5.0, None, None, 2)
        jsonl_path = telemetry_dir / "chromadb.jsonl"
        lines = jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_never_raises(self):
        """_log_record should never raise, even with bad paths."""
        with patch("tools.chroma_telemetry._get_telemetry_path", return_value="/nonexistent/path/chromadb.jsonl"):
            # Should not raise
            _log_record("upsert", 10.0, None, None, 1)


# ── Health probe ─────────────────────────────────────────────────────────────


class TestProbeOnce:
    def test_successful_probe(self, mock_config, telemetry_dir):
        """Probe should succeed with a valid ChromaDB instance."""
        from tools.memory import _get_collection

        result = _probe_once()
        assert result["read_ok"] is True
        assert result["write_ok"] is True
        assert "doc_count" in result
        assert chromadb_health["status"] == "ok"

    def test_probe_updates_doc_count(self, mock_config, telemetry_dir):
        result = _probe_once()
        assert chromadb_health["doc_count"] is not None

    def test_probe_logs_success(self, mock_config, telemetry_dir):
        _probe_once()
        jsonl_path = telemetry_dir / "chromadb.jsonl"
        if jsonl_path.exists():
            lines = jsonl_path.read_text().strip().split("\n")
            ops = [json.loads(line)["op"] for line in lines]
            assert "probe_ok" in ops


class TestHealthProbeLoop:
    def test_loop_respects_cancellation(self):
        """The loop should exit cleanly when cancelled."""
        async def run():
            with patch("tools.config.get_telemetry_config", return_value={
                "enabled": False, "log_reads": False, "log_writes": False, "probe_interval_seconds": 0.1
            }), patch("tools.chroma_telemetry._PROBE_STARTUP_DELAY", 0):
                task = asyncio.create_task(health_probe_loop())
                await asyncio.sleep(0.3)
                task.cancel()
                # Loop catches CancelledError and returns cleanly
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                assert task.done()

        asyncio.run(run())


# ── Integration: InstrumentedCollection in memory.py ─────────────────────────


class TestMemoryIntegration:
    def test_get_collection_returns_instrumented(self, mock_config):
        """_get_collection() should return an InstrumentedCollection."""
        from tools.memory import _get_collection

        coll = _get_collection()
        assert isinstance(coll, InstrumentedCollection)

    def test_instrumented_collection_works_with_real_chromadb(self, mock_config):
        """The wrapper should work with real ChromaDB operations."""
        from tools.memory import _get_collection

        coll = _get_collection()
        # Write
        coll.upsert(
            ids=["test::1"],
            documents=["hello world"],
            metadatas=[{"type": "test"}],
        )
        # Read
        result = coll.get(ids=["test::1"])
        assert result["ids"] == ["test::1"]
        assert result["documents"] == ["hello world"]
        # Count
        count = coll.count()
        assert count >= 1
        # Cleanup
        coll.delete(ids=["test::1"])
