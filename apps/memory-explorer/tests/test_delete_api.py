"""Tests for the memory delete endpoint and search status filtering.

Uses FastAPI TestClient with mocked _local_pool and _sources.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Ensure app is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Isolate config to temp directory."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({
        "memory": {"postgres": {"url": "postgresql://test:test@localhost/test"}},
    }))
    from jarvis_common.config import clear_config_cache
    clear_config_cache()
    yield
    clear_config_cache()


def _mock_pool():
    """Create a mock connection pool with cursor support."""
    pool = MagicMock()
    conn = MagicMock()
    cur = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)
    conn.cursor.return_value.__enter__ = MagicMock(return_value=cur)
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    conn.execute = MagicMock(return_value=cur)
    return pool, conn, cur


_LOCAL_SOURCE = {
    "id": "local",
    "label": "Local Memories",
    "type": "local",
    "schema": "local",
    "table": "memories",
    "has_retrieval_count": True,
    "capabilities": ["text", "metadata"],
    "metadata_filters": ["category", "scope", "status"],
}

_OBSIDIAN_SOURCE = {
    "id": "obsidian",
    "label": "Obsidian Vault",
    "type": "local",
    "schema": "obsidian",
    "table": "documents",
    "has_retrieval_count": False,
    "capabilities": ["text"],
    "metadata_filters": ["vault_type", "directory"],
}

_REMOTE_SOURCE = {
    "id": "remote:aurora",
    "label": "Remote: aurora",
    "type": "remote",
    "remote_name": "aurora",
    "schema": "aurora",
    "available": True,
    "capabilities": ["text"],
    "metadata_filters": ["category", "scope", "status"],
}


@pytest.fixture
def mock_sources():
    return {
        "local": _LOCAL_SOURCE,
        "obsidian": _OBSIDIAN_SOURCE,
        "remote:aurora": _REMOTE_SOURCE,
    }


@pytest.fixture
def client(mock_sources):
    pool, conn, cur = _mock_pool()
    with (
        patch("app._local_pool", pool),
        patch("app._sources", mock_sources),
    ):
        import app as app_module
        yield TestClient(app_module.app), pool, conn, cur


# ── Sources API: deletable flag ──────────────────────────────────────────

class TestSourcesDeletable:
    def test_local_is_deletable(self, client):
        tc, *_ = client
        r = tc.get("/api/sources")
        assert r.status_code == 200
        sources = {s["id"]: s for s in r.json()}
        assert sources["local"]["deletable"] is True

    def test_obsidian_not_deletable(self, client):
        tc, *_ = client
        r = tc.get("/api/sources")
        sources = {s["id"]: s for s in r.json()}
        assert sources["obsidian"]["deletable"] is False

    def test_remote_not_deletable(self, client):
        tc, *_ = client
        r = tc.get("/api/sources")
        sources = {s["id"]: s for s in r.json()}
        assert sources["remote:aurora"]["deletable"] is False


# ── Delete endpoint ──────────────────────────────────────────────────────

class TestDeleteMemory:
    def test_delete_success(self, client):
        tc, pool, conn, cur = client
        # Mock: UPDATE returns a row (id, synced_to=[])
        cur.fetchone.return_value = ("obs::test-123", [])
        r = tc.delete("/api/memories/obs::test-123?source=local")
        assert r.status_code == 200
        assert r.json() == {"ok": True, "id": "obs::test-123"}

    def test_delete_with_sync_propagation(self, client):
        tc, pool, conn, cur = client
        cur.fetchone.return_value = ("obs::test-456", ["aurora"])
        with patch("app.enqueue_sync") as mock_sync:
            r = tc.delete("/api/memories/obs::test-456?source=local")
            assert r.status_code == 200
            mock_sync.assert_called_once()
            call_args = mock_sync.call_args
            assert call_args[0][1] == "obs::test-456"
            assert call_args[0][2] == ["aurora"]

    def test_delete_not_found(self, client):
        tc, pool, conn, cur = client
        cur.fetchone.return_value = None
        r = tc.delete("/api/memories/obs::nonexistent?source=local")
        assert r.status_code == 404
        assert "not found" in r.json()["detail"].lower()

    def test_delete_obsidian_forbidden(self, client):
        tc, *_ = client
        r = tc.delete("/api/memories/vault::test?source=obsidian")
        assert r.status_code == 403
        assert "local memories" in r.json()["detail"].lower()

    def test_delete_remote_forbidden(self, client):
        tc, *_ = client
        r = tc.delete("/api/memories/obs::test?source=remote:aurora")
        assert r.status_code == 403

    def test_delete_unknown_source(self, client):
        tc, *_ = client
        r = tc.delete("/api/memories/obs::test?source=nonexistent")
        assert r.status_code == 404
        assert "Source not found" in r.json()["detail"]

    def test_delete_sync_failure_does_not_block(self, client):
        """If enqueue_sync fails, delete still succeeds."""
        tc, pool, conn, cur = client
        cur.fetchone.return_value = ("obs::test-789", ["aurora"])
        with patch("app.enqueue_sync", side_effect=Exception("sync down")):
            r = tc.delete("/api/memories/obs::test-789?source=local")
            assert r.status_code == 200
            assert r.json()["ok"] is True
