"""E2E tests for sync_sweep against real PostgreSQL.

Tests cover: full roundtrip, already-synced skip, dry run, partial sync,
and idempotent re-run.
"""

import os

import psycopg
import pytest

E2E_POSTGRES_URL = os.environ.get("E2E_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(
    not E2E_POSTGRES_URL,
    reason="E2E_POSTGRES_URL not set — skipping e2e tests",
)


def _write_memory(content, skip_secret_scan=True):
    """Write a test memory via content_write and return the ID."""
    from tools.content import content_write

    result = content_write(
        content=content,
        content_type="observation",
        importance_score=0.5,
        skip_secret_scan=skip_secret_scan,
    )
    assert result["success"], f"content_write failed: {result}"
    return result["id"]


def _sync_cfg(enabled, destinations=None):
    """Build a sync config dict."""
    destinations = destinations or ["backup"]
    if not enabled:
        return {"enabled": False}
    return {
        "enabled": True,
        "strategy": "first-match",
        "remotes": {d: {"url": "postgresql://h:5432/db"} for d in destinations},
        "rules": [
            {"name": "all", "match": {}, "action": "route-to",
             "destinations": destinations},
        ],
        "project_groups": {},
    }


def _patch_sync_config(monkeypatch, cfg):
    """Patch get_sync_config in both config module and sync_sweep module.

    sync_sweep uses `from .config import get_sync_config` which binds
    at import time. We must patch the reference where it's used, not
    just where it's defined.
    """
    getter = lambda: cfg
    monkeypatch.setattr("tools.config.get_sync_config", getter)
    # Also patch the imported reference in sync_sweep (if already imported)
    import tools.sync_sweep as sw
    monkeypatch.setattr(sw, "get_sync_config", getter)


class TestSyncSweep:
    """E2E tests for the sync_sweep function."""

    def test_sweep_enqueues_unsynced_memories(self, e2e_config, monkeypatch):
        """Memories written without sync → sweep enqueues them."""
        # Write memories with sync disabled
        _patch_sync_config(monkeypatch, _sync_cfg(enabled=False))
        doc_id1 = _write_memory("Sweep test memory 1")
        doc_id2 = _write_memory("Sweep test memory 2")

        # Enable sync and run sweep
        _patch_sync_config(monkeypatch, _sync_cfg(enabled=True))

        from tools.sync_sweep import sync_sweep
        result = sync_sweep()

        assert result["success"] is True
        assert result["scanned"] >= 2
        assert result["needing_sync"] >= 2
        assert result["enqueued"] >= 2
        assert "backup" in result["by_destination"]
        assert result["failed_count"] == 0

        # Verify queue entries exist
        from tools.schema import _get_pool
        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT memory_id FROM local.sync_queue "
                    "WHERE memory_id IN (%s, %s) AND destination = 'backup'",
                    (doc_id1, doc_id2),
                )
                rows = cur.fetchall()
                queued_ids = {r[0] for r in rows}
                assert doc_id1 in queued_ids
                assert doc_id2 in queued_ids

    def test_sweep_skips_already_synced(self, e2e_config, monkeypatch):
        """Memories with synced_to=['backup'] are not re-enqueued."""
        from tools.schema import _get_pool
        from tools.sync_queue import update_synced_to

        # Write and mark as already synced
        _patch_sync_config(monkeypatch, _sync_cfg(enabled=False))
        doc_id = _write_memory("Already synced memory")

        pool = _get_pool()
        update_synced_to(pool, doc_id, "backup")

        # Enable sync and run sweep
        _patch_sync_config(monkeypatch, _sync_cfg(enabled=True))

        from tools.sync_sweep import sync_sweep
        result = sync_sweep()

        assert result["success"] is True
        # This memory should not be in needing_sync (already has 'backup')
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM local.sync_queue "
                    "WHERE memory_id = %s AND destination = 'backup'",
                    (doc_id,),
                )
                count = cur.fetchone()[0]
                assert count == 0

    def test_sweep_dry_run(self, e2e_config, monkeypatch):
        """Dry run produces correct counts but writes nothing."""
        _patch_sync_config(monkeypatch, _sync_cfg(enabled=False))
        doc_id = _write_memory("Dry run sweep memory")

        _patch_sync_config(monkeypatch, _sync_cfg(enabled=True))

        from tools.sync_sweep import sync_sweep
        result = sync_sweep(dry_run=True)

        assert result["success"] is True
        assert result["scanned"] >= 1
        assert result["needing_sync"] >= 1
        assert result["enqueued"] == 0
        assert result["dry_run"] is True

        # Verify no queue entries
        from tools.schema import _get_pool
        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT count(*) FROM local.sync_queue WHERE memory_id = %s",
                    (doc_id,),
                )
                count = cur.fetchone()[0]
                assert count == 0

    def test_sweep_partial_sync(self, e2e_config, monkeypatch):
        """Memory synced to 'backup' but not 'archive' → only 'archive' enqueued."""
        from tools.schema import _get_pool
        from tools.sync_queue import update_synced_to

        _patch_sync_config(monkeypatch, _sync_cfg(enabled=False))
        doc_id = _write_memory("Partial sync sweep memory")

        pool = _get_pool()
        update_synced_to(pool, doc_id, "backup")

        _patch_sync_config(monkeypatch, _sync_cfg(
            enabled=True, destinations=["backup", "archive"],
        ))

        from tools.sync_sweep import sync_sweep
        result = sync_sweep()

        assert result["success"] is True
        # Only 'archive' should be enqueued (not 'backup')
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT destination FROM local.sync_queue WHERE memory_id = %s",
                    (doc_id,),
                )
                dests = [r[0] for r in cur.fetchall()]
                assert "archive" in dests
                assert "backup" not in dests

    def test_sweep_idempotent(self, e2e_config, monkeypatch):
        """Second sweep run creates 0 new entries (ON CONFLICT handles re-enqueue)."""
        _patch_sync_config(monkeypatch, _sync_cfg(enabled=False))
        doc_id = _write_memory("Idempotent sweep memory")

        _patch_sync_config(monkeypatch, _sync_cfg(enabled=True))

        from tools.sync_sweep import sync_sweep

        # First sweep
        result1 = sync_sweep()
        assert result1["success"]
        assert result1["enqueued"] >= 1

        # Second sweep — same memories, same rules
        result2 = sync_sweep()
        assert result2["success"]
        # enqueued should be 0 because the existing pending entries
        # don't match the ON CONFLICT WHERE clause (status IN ('done', 'dlq'))
        assert result2["enqueued"] == 0
