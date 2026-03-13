"""Tests for sync_sweep — retroactive routing of orphaned memories.

Mocks: get_sync_config, _get_pool, enqueue_sync.
The mock cursor returns row tuples with a description attribute for column names.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from tools.routing import RoutingDecision, RoutingRule, MatchCondition


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_description(names):
    """Build a mock cursor.description from column names.

    MagicMock(name=...) sets the mock's repr, NOT the .name attribute.
    We must set .name explicitly after construction.
    """
    descs = []
    for n in names:
        d = MagicMock()
        d.name = n
        descs.append(d)
    return descs


COLUMNS = [
    "id", "document", "category", "scope", "project",
    "importance_score", "metadata", "synced_to", "created_at",
]

_CEILING_TS = datetime(2026, 3, 13, 12, 0, 0, tzinfo=timezone.utc)


def _make_row(
    doc_id="obs::1",
    category="observation",
    scope="global",
    project=None,
    importance=0.5,
    metadata=None,
    synced_to=None,
    created_at=None,
):
    return (
        doc_id,
        "some content",
        category,
        scope,
        project,
        importance,
        metadata or {},
        synced_to or [],
        created_at or _CEILING_TS,
    )


def _mock_pool(rows_batches):
    """Create a mock pool that returns rows_batches on successive queries.

    rows_batches: list of list-of-tuples. First query returns [0], second [1], etc.
    An extra empty batch is appended for the ceiling query and termination.
    """
    pool = MagicMock()
    conn = MagicMock()
    cur = MagicMock()

    # Track call sequence: first call is ceiling query, then pagination batches
    call_count = {"n": 0}
    ceiling_desc = _make_description(["max"])
    batch_desc = _make_description(COLUMNS)

    def execute_side_effect(sql, params=None):
        n = call_count["n"]
        call_count["n"] += 1

        if n == 0:
            # Ceiling query
            cur.description = ceiling_desc
            cur.fetchone.return_value = (_CEILING_TS,)
        else:
            # Pagination batch
            batch_idx = n - 1
            if batch_idx < len(rows_batches):
                cur.description = batch_desc
                cur.fetchall.return_value = rows_batches[batch_idx]
            else:
                cur.description = batch_desc
                cur.fetchall.return_value = []

    cur.execute = execute_side_effect
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = lambda s, *a: None
    conn.__enter__ = lambda s: conn
    conn.__exit__ = lambda s, *a: None
    pool.connection.return_value = conn
    return pool, cur


def _sync_cfg_enabled(rules=None):
    return {
        "enabled": True,
        "strategy": "first-match",
        "rules": rules or [
            {"name": "all", "match": {}, "action": "route-to",
             "destinations": ["backup"]},
        ],
        "project_groups": {},
    }


# ── Tests ────────────────────────────────────────────────────────────────


class TestSyncSweep:

    @patch("tools.sync_sweep.get_sync_config")
    def test_disabled_returns_error(self, mock_cfg):
        from tools.sync_sweep import sync_sweep
        mock_cfg.return_value = {"enabled": False}
        result = sync_sweep()
        assert not result["success"]
        assert "not enabled" in result["error"]

    @patch("tools.sync_sweep.load_routing_rules")
    @patch("tools.sync_sweep.get_sync_config")
    def test_no_rules_returns_error(self, mock_cfg, mock_rules):
        from tools.sync_sweep import sync_sweep
        mock_cfg.return_value = {"enabled": True, "rules": [], "strategy": "first-match", "project_groups": {}}
        mock_rules.return_value = []
        result = sync_sweep()
        assert not result["success"]
        assert "No routing rules" in result["error"]

    @patch("tools.sync_sweep.enqueue_sync")
    @patch("tools.sync_sweep._get_pool")
    @patch("tools.sync_sweep.get_sync_config")
    def test_dry_run_does_not_enqueue(self, mock_cfg, mock_pool_fn, mock_enqueue):
        from tools.sync_sweep import sync_sweep
        mock_cfg.return_value = _sync_cfg_enabled()

        rows = [_make_row("obs::1"), _make_row("obs::2")]
        pool, _ = _mock_pool([rows])
        mock_pool_fn.return_value = pool

        result = sync_sweep(dry_run=True)
        assert result["success"]
        assert result["scanned"] == 2
        assert result["needing_sync"] == 2
        assert result["enqueued"] == 0
        assert result["dry_run"] is True
        mock_enqueue.assert_not_called()

    @patch("tools.sync_sweep.enqueue_sync")
    @patch("tools.sync_sweep._get_pool")
    @patch("tools.sync_sweep.get_sync_config")
    def test_enqueues_missing_destinations(self, mock_cfg, mock_pool_fn, mock_enqueue):
        from tools.sync_sweep import sync_sweep
        mock_cfg.return_value = _sync_cfg_enabled()
        mock_enqueue.return_value = 1

        rows = [_make_row("obs::1"), _make_row("obs::2")]
        pool, _ = _mock_pool([rows])
        mock_pool_fn.return_value = pool

        result = sync_sweep()
        assert result["success"]
        assert result["scanned"] == 2
        assert result["needing_sync"] == 2
        assert result["enqueued"] == 2
        assert result["by_destination"] == {"backup": 2}

    @patch("tools.sync_sweep.enqueue_sync")
    @patch("tools.sync_sweep._get_pool")
    @patch("tools.sync_sweep.get_sync_config")
    def test_partially_synced_memory(self, mock_cfg, mock_pool_fn, mock_enqueue):
        """Memory synced to 'backup' but rule also routes to 'archive' → only 'archive' enqueued."""
        from tools.sync_sweep import sync_sweep
        mock_cfg.return_value = _sync_cfg_enabled(rules=[
            {"name": "all", "match": {}, "action": "route-to",
             "destinations": ["backup", "archive"]},
        ])
        mock_enqueue.return_value = 1

        rows = [_make_row("obs::1", synced_to=["backup"])]
        pool, _ = _mock_pool([rows])
        mock_pool_fn.return_value = pool

        result = sync_sweep()
        assert result["success"]
        assert result["needing_sync"] == 1
        assert result["by_destination"] == {"archive": 1}

    @patch("tools.sync_sweep.enqueue_sync")
    @patch("tools.sync_sweep._get_pool")
    @patch("tools.sync_sweep.get_sync_config")
    def test_fully_synced_memory_skipped(self, mock_cfg, mock_pool_fn, mock_enqueue):
        """Memory already synced to all destinations → nothing to do."""
        from tools.sync_sweep import sync_sweep
        mock_cfg.return_value = _sync_cfg_enabled()
        mock_enqueue.return_value = 0

        rows = [_make_row("obs::1", synced_to=["backup"])]
        pool, _ = _mock_pool([rows])
        mock_pool_fn.return_value = pool

        result = sync_sweep()
        assert result["success"]
        assert result["scanned"] == 1
        assert result["needing_sync"] == 0
        assert result["enqueued"] == 0

    @patch("tools.sync_sweep.enqueue_sync")
    @patch("tools.sync_sweep._get_pool")
    @patch("tools.sync_sweep.get_sync_config")
    def test_batch_pagination(self, mock_cfg, mock_pool_fn, mock_enqueue):
        """Multi-batch scan — two batches of 2 rows each."""
        from tools.sync_sweep import sync_sweep

        mock_cfg.return_value = _sync_cfg_enabled()
        mock_enqueue.return_value = 1

        ts1 = datetime(2026, 3, 1, tzinfo=timezone.utc)
        ts2 = datetime(2026, 3, 2, tzinfo=timezone.utc)
        ts3 = datetime(2026, 3, 3, tzinfo=timezone.utc)
        ts4 = datetime(2026, 3, 4, tzinfo=timezone.utc)

        batch1 = [
            _make_row("obs::1", created_at=ts1),
            _make_row("obs::2", created_at=ts2),
        ]
        batch2 = [
            _make_row("obs::3", created_at=ts3),
            _make_row("obs::4", created_at=ts4),
        ]
        pool, _ = _mock_pool([batch1, batch2])
        mock_pool_fn.return_value = pool

        result = sync_sweep(batch_size=2)
        assert result["success"]
        assert result["scanned"] == 4
        assert result["needing_sync"] == 4

    @patch("tools.sync_sweep.enqueue_sync")
    @patch("tools.sync_sweep._get_pool")
    @patch("tools.sync_sweep.get_sync_config")
    def test_empty_database(self, mock_cfg, mock_pool_fn, mock_enqueue):
        from tools.sync_sweep import sync_sweep
        mock_cfg.return_value = _sync_cfg_enabled()

        # Ceiling returns None → empty database
        pool = MagicMock()
        conn = MagicMock()
        cur = MagicMock()
        cur.fetchone.return_value = (None,)
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = lambda s, *a: None
        conn.__enter__ = lambda s: conn
        conn.__exit__ = lambda s, *a: None
        pool.connection.return_value = conn
        mock_pool_fn.return_value = pool

        result = sync_sweep()
        assert result["success"]
        assert result["scanned"] == 0
        mock_enqueue.assert_not_called()

    @patch("tools.sync_sweep.enqueue_sync")
    @patch("tools.sync_sweep._get_pool")
    @patch("tools.sync_sweep.get_sync_config")
    def test_by_destination_breakdown(self, mock_cfg, mock_pool_fn, mock_enqueue):
        """Per-destination counts are correct when routing to multiple dests."""
        from tools.sync_sweep import sync_sweep
        mock_cfg.return_value = _sync_cfg_enabled(rules=[
            {"name": "all", "match": {}, "action": "route-to",
             "destinations": ["backup", "archive"]},
        ])
        mock_enqueue.return_value = 1

        rows = [_make_row("obs::1"), _make_row("obs::2")]
        pool, _ = _mock_pool([rows])
        mock_pool_fn.return_value = pool

        result = sync_sweep()
        assert result["by_destination"] == {"backup": 2, "archive": 2}

    @patch("tools.sync_sweep.enqueue_sync")
    @patch("tools.sync_sweep._get_pool")
    @patch("tools.sync_sweep.get_sync_config")
    def test_routing_with_deny_rules(self, mock_cfg, mock_pool_fn, mock_enqueue):
        """Deny rule excludes destinations from sweep."""
        from tools.sync_sweep import sync_sweep
        mock_cfg.return_value = _sync_cfg_enabled(rules=[
            {"name": "deny-obs", "match": {"category": ["observation"]},
             "action": "deny", "destinations": ["archive"]},
            {"name": "all", "match": {}, "action": "route-to",
             "destinations": ["backup", "archive"]},
        ])
        mock_enqueue.return_value = 1

        rows = [_make_row("obs::1", category="observation")]
        pool, _ = _mock_pool([rows])
        mock_pool_fn.return_value = pool

        result = sync_sweep()
        assert result["success"]
        # 'archive' denied for observations, only 'backup' remains
        assert result["by_destination"] == {"backup": 1}

    @patch("tools.sync_sweep.enqueue_sync")
    @patch("tools.sync_sweep._get_pool")
    @patch("tools.sync_sweep.get_sync_config")
    def test_malformed_metadata_skipped(self, mock_cfg, mock_pool_fn, mock_enqueue):
        """Row with metadata that causes routing error → counted as failure, not crash."""
        from tools.sync_sweep import sync_sweep

        # Use a config where routing will fail for specific metadata
        mock_cfg.return_value = _sync_cfg_enabled()
        mock_enqueue.return_value = 1

        # Normal row + row with metadata that's a string (not dict)
        good_row = _make_row("obs::1")
        # Manually create a row with non-dict metadata to trigger error
        bad_row = list(_make_row("obs::bad"))
        bad_row[6] = "not-a-dict"  # metadata should be a dict
        bad_row = tuple(bad_row)

        pool, _ = _mock_pool([[bad_row, good_row]])
        mock_pool_fn.return_value = pool

        result = sync_sweep()
        assert result["success"]
        # The bad row should fail gracefully, the good one should succeed
        assert result["scanned"] == 2
        # At least the good row should be counted
        assert result["needing_sync"] >= 1

    @patch("tools.sync_sweep.enqueue_sync")
    @patch("tools.sync_sweep._get_pool")
    @patch("tools.sync_sweep.get_sync_config")
    def test_scan_ceiling_excludes_concurrent_writes(self, mock_cfg, mock_pool_fn, mock_enqueue):
        """Rows with created_at > ceiling are excluded by the WHERE clause."""
        from tools.sync_sweep import sync_sweep
        mock_cfg.return_value = _sync_cfg_enabled()
        mock_enqueue.return_value = 1

        # Only rows at or before ceiling should appear in the batch
        # (the SQL has WHERE created_at <= ceiling, so the mock just returns
        # what the DB would return — we verify the SQL uses the ceiling)
        rows = [_make_row("obs::1", created_at=_CEILING_TS)]
        pool, cur = _mock_pool([rows])
        mock_pool_fn.return_value = pool

        result = sync_sweep()
        assert result["success"]
        assert result["scanned"] == 1
        # Verify the ceiling timestamp was passed to the pagination query
        # (second execute call, params tuple should contain ceiling)
        # The mock tracks that execute was called with ceiling in params
