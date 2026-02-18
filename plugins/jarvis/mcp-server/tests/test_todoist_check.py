"""Tests for Todoist cache reader (hook-side todoist_check.py).

Tests cover:
- Cache hit with alerts → XML output
- Cache hit with empty alerts → empty string
- Cache file missing → empty string
- Cache file corrupt → empty string
- max_per_category limiting
- XML escaping (task content with <>&)
- Priority formatting
- Staleness detection
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Add hooks-handlers to path for importing todoist_check module
HOOKS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "hooks-handlers"
)
sys.path.insert(0, HOOKS_DIR)

from todoist_check import get_todoist_alerts, _is_stale, _STALE_THRESHOLD_SECONDS


@pytest.fixture
def temp_cache_dir():
    """Create a temporary directory for cache files."""
    with tempfile.TemporaryDirectory(prefix="jarvis_test_cache_") as tmpdir:
        yield Path(tmpdir)


def _write_cache(path: Path, alerts: dict, synced_at: str = None):
    """Helper to write a cache file for testing."""
    if synced_at is None:
        synced_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
    counts = {k: len(v) for k, v in alerts.items()}
    data = {
        "synced_at": synced_at,
        "inbox_project_id": "111",
        "alerts": alerts,
        "counts": counts,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


class TestGetTodoistAlerts:

    def test_cache_with_alerts(self, temp_cache_dir):
        """Valid cache with alerts should produce XML output."""
        cache_path = temp_cache_dir / "todoist_cache.json"
        alerts = {
            "overdue": [
                {"id": "1", "content": "Buy groceries", "priority": 1, "due": "2026-02-17"}
            ],
            "due_today": [
                {"id": "2", "content": "Team standup", "priority": 4}
            ],
            "inbox_unprocessed": [],
            "scheduled_actions": [],
        }
        _write_cache(cache_path, alerts)

        result = get_todoist_alerts(str(cache_path))
        assert "<todoist-alerts" in result
        assert 'type="overdue"' in result
        assert "Buy groceries" in result
        assert 'type="due_today"' in result
        assert "Team standup" in result
        # Empty categories should be omitted
        assert "inbox_unprocessed" not in result
        assert "scheduled_actions" not in result
        assert "</todoist-alerts>" in result

    def test_cache_with_empty_alerts(self, temp_cache_dir):
        """Cache with all empty categories should return empty string."""
        cache_path = temp_cache_dir / "todoist_cache.json"
        alerts = {
            "overdue": [],
            "due_today": [],
            "inbox_unprocessed": [],
            "scheduled_actions": [],
        }
        _write_cache(cache_path, alerts)

        assert get_todoist_alerts(str(cache_path)) == ""

    def test_cache_file_missing(self, temp_cache_dir):
        """Missing cache file should return empty string."""
        cache_path = temp_cache_dir / "nonexistent.json"
        assert get_todoist_alerts(str(cache_path)) == ""

    def test_cache_file_corrupt(self, temp_cache_dir):
        """Corrupt JSON should return empty string (never crash)."""
        cache_path = temp_cache_dir / "todoist_cache.json"
        cache_path.write_text("not valid json {{{")
        assert get_todoist_alerts(str(cache_path)) == ""

    def test_max_per_category(self, temp_cache_dir):
        """Should limit tasks per category to max_per_category."""
        cache_path = temp_cache_dir / "todoist_cache.json"
        overdue_tasks = [
            {"id": str(i), "content": f"Task {i}", "priority": 4}
            for i in range(10)
        ]
        alerts = {
            "overdue": overdue_tasks,
            "due_today": [],
            "inbox_unprocessed": [],
            "scheduled_actions": [],
        }
        _write_cache(cache_path, alerts)

        result = get_todoist_alerts(str(cache_path), max_per_category=2)
        # count attribute should show total (10)
        assert 'count="10"' in result
        # But only 2 task elements
        assert result.count("<task ") == 2

    def test_xml_escaping(self, temp_cache_dir):
        """Special characters in task content should be XML-escaped."""
        cache_path = temp_cache_dir / "todoist_cache.json"
        alerts = {
            "overdue": [
                {"id": "1", "content": "Fix <bug> & deploy \"release\"", "priority": 4}
            ],
            "due_today": [],
            "inbox_unprocessed": [],
            "scheduled_actions": [],
        }
        _write_cache(cache_path, alerts)

        result = get_todoist_alerts(str(cache_path))
        # Should contain escaped entities
        assert "&lt;bug&gt;" in result
        assert "&amp;" in result

    def test_priority_formatting(self, temp_cache_dir):
        """High priority tasks should show priority attribute."""
        cache_path = temp_cache_dir / "todoist_cache.json"
        alerts = {
            "overdue": [
                {"id": "1", "content": "Urgent", "priority": 1},
                {"id": "2", "content": "Normal", "priority": 4},
            ],
            "due_today": [],
            "inbox_unprocessed": [],
            "scheduled_actions": [],
        }
        _write_cache(cache_path, alerts)

        result = get_todoist_alerts(str(cache_path))
        # p1 (highest) should show priority
        assert 'priority="p1"' in result
        # p4 (normal/default) should NOT show priority attribute
        lines_with_normal = [l for l in result.split("\n") if "Normal" in l]
        assert len(lines_with_normal) == 1
        assert "priority=" not in lines_with_normal[0]

    def test_due_date_in_content(self, temp_cache_dir):
        """Tasks with due dates should show the date in parentheses."""
        cache_path = temp_cache_dir / "todoist_cache.json"
        alerts = {
            "overdue": [
                {"id": "1", "content": "Buy milk", "priority": 4, "due": "2026-02-17"}
            ],
            "due_today": [],
            "inbox_unprocessed": [],
            "scheduled_actions": [],
        }
        _write_cache(cache_path, alerts)

        result = get_todoist_alerts(str(cache_path))
        assert "(due 2026-02-17)" in result


class TestStaleness:

    def test_fresh_cache_not_stale(self):
        """Recent synced_at should not be stale."""
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S")
        assert _is_stale(now) is False

    def test_old_cache_is_stale(self):
        """Cache older than threshold should be stale."""
        from datetime import timedelta

        old = (datetime.now(timezone.utc) - timedelta(seconds=_STALE_THRESHOLD_SECONDS + 60))
        old_str = old.strftime("%Y-%m-%dT%H:%M:%S")
        assert _is_stale(old_str) is True

    def test_stale_cache_returns_empty(self, temp_cache_dir):
        """Stale cache should return empty string from get_todoist_alerts."""
        from datetime import timedelta

        cache_path = temp_cache_dir / "todoist_cache.json"
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=_STALE_THRESHOLD_SECONDS + 120))
        alerts = {
            "overdue": [{"id": "1", "content": "Old task", "priority": 4}],
            "due_today": [],
            "inbox_unprocessed": [],
            "scheduled_actions": [],
        }
        _write_cache(cache_path, alerts, synced_at=old_time.strftime("%Y-%m-%dT%H:%M:%S"))

        assert get_todoist_alerts(str(cache_path)) == ""

    def test_unparseable_timestamp_not_stale(self):
        """If synced_at can't be parsed, don't treat as stale (fail open)."""
        assert _is_stale("not-a-timestamp") is False
