"""Tests for Todoist background sync module."""

import asyncio
import json
import os
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from tools.todoist_sync import (
    _get_todoist_token,
    _fetch_tasks,
    _fetch_projects,
    _find_inbox_project_id,
    _parse_due_date,
    _is_timed_overdue,
    _task_summary,
    classify_tasks,
    _write_cache,
    _get_cache_path,
    sync_once,
    todoist_sync_loop,
)


# ── Fixtures ─────────────────────────────────────────────────────────────────


@pytest.fixture
def temp_state_dir():
    """Create a temporary state directory for cache files."""
    with tempfile.TemporaryDirectory(prefix="jarvis_test_state_") as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_projects():
    """Sample Todoist projects list."""
    return [
        {"id": "111", "name": "Inbox", "is_inbox_project": True},
        {"id": "222", "name": "Work", "is_inbox_project": False},
        {"id": "333", "name": "Personal", "is_inbox_project": False},
    ]


@pytest.fixture
def sample_tasks():
    """Sample Todoist tasks for classification testing."""
    today = date.today().isoformat()
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()

    return [
        # Overdue task
        {
            "id": "1",
            "content": "Buy groceries",
            "priority": 3,
            "project_id": "222",
            "labels": [],
            "due": {"date": yesterday},
        },
        # Due today
        {
            "id": "2",
            "content": "Team standup",
            "priority": 4,
            "project_id": "222",
            "labels": [],
            "due": {"date": today},
        },
        # Future task (should not appear in overdue or due_today)
        {
            "id": "3",
            "content": "Future planning",
            "priority": 4,
            "project_id": "222",
            "labels": [],
            "due": {"date": tomorrow},
        },
        # Inbox unprocessed (inbox project, no labels)
        {
            "id": "4",
            "content": "Random thought",
            "priority": 4,
            "project_id": "111",
            "labels": [],
            "due": None,
        },
        # Inbox WITH labels (should NOT be unprocessed)
        {
            "id": "5",
            "content": "Processed inbox item",
            "priority": 4,
            "project_id": "111",
            "labels": ["work"],
            "due": None,
        },
        # Scheduled action
        {
            "id": "6",
            "content": "Weekly review",
            "priority": 2,
            "project_id": "222",
            "labels": ["jarvis-scheduled"],
            "due": {"date": today},
        },
        # No due date, not inbox
        {
            "id": "7",
            "content": "Someday task",
            "priority": 4,
            "project_id": "333",
            "labels": [],
            "due": None,
        },
    ]


# ── Token Resolution ─────────────────────────────────────────────────────────


class TestTokenResolution:

    def test_env_var_priority(self, monkeypatch):
        """TODOIST_API_TOKEN env var should take precedence."""
        monkeypatch.setenv("TODOIST_API_TOKEN", "env-token-123")
        assert _get_todoist_token() == "env-token-123"

    def test_config_fallback(self, monkeypatch):
        """Falls back to config file when env var is not set."""
        monkeypatch.delenv("TODOIST_API_TOKEN", raising=False)
        mock_config = {"todoist": {"api_token": "config-token-456"}}
        monkeypatch.setattr("tools.config.get_config", lambda: mock_config)
        from tools.config import clear_config_cache
        clear_config_cache()
        token = _get_todoist_token()
        assert token == "config-token-456"

    def test_empty_env_var_falls_through(self, monkeypatch):
        """Empty env var should fall through to config."""
        monkeypatch.setenv("TODOIST_API_TOKEN", "")
        mock_config = {"todoist": {"api_token": "config-token"}}
        monkeypatch.setattr("tools.config.get_config", lambda: mock_config)
        from tools.config import clear_config_cache
        clear_config_cache()
        assert _get_todoist_token() == "config-token"

    def test_no_token_returns_empty(self, monkeypatch):
        """Returns empty string when no token is configured anywhere."""
        monkeypatch.delenv("TODOIST_API_TOKEN", raising=False)
        monkeypatch.setattr("tools.config.get_config", lambda: {})
        from tools.config import clear_config_cache
        clear_config_cache()
        assert _get_todoist_token() == ""

    def test_whitespace_env_var_ignored(self, monkeypatch):
        """Whitespace-only env var should be treated as empty."""
        monkeypatch.setenv("TODOIST_API_TOKEN", "   ")
        monkeypatch.setattr("tools.config.get_config", lambda: {})
        from tools.config import clear_config_cache
        clear_config_cache()
        assert _get_todoist_token() == ""


# ── Inbox Detection ──────────────────────────────────────────────────────────


class TestInboxDetection:

    def test_finds_inbox_project(self, sample_projects):
        assert _find_inbox_project_id(sample_projects) == "111"

    def test_no_inbox_project(self):
        projects = [{"id": "1", "name": "Work", "is_inbox_project": False}]
        assert _find_inbox_project_id(projects) == ""

    def test_empty_projects_list(self):
        assert _find_inbox_project_id([]) == ""


# ── Due Date Parsing ─────────────────────────────────────────────────────────


class TestDueDateParsing:

    def test_date_only(self):
        task = {"due": {"date": "2026-02-18"}}
        assert _parse_due_date(task) == date(2026, 2, 18)

    def test_datetime_string_truncated_to_date(self):
        task = {"due": {"date": "2026-02-18T14:30:00"}}
        # Should parse the date portion only
        assert _parse_due_date(task) == date(2026, 2, 18)

    def test_no_due(self):
        assert _parse_due_date({"due": None}) is None
        assert _parse_due_date({}) is None

    def test_empty_date_string(self):
        assert _parse_due_date({"due": {"date": ""}}) is None

    def test_invalid_date(self):
        assert _parse_due_date({"due": {"date": "not-a-date"}}) is None


class TestTimedOverdue:

    def test_past_datetime_is_overdue(self):
        past = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        task = {"due": {"datetime": past}}
        assert _is_timed_overdue(task) is True

    def test_future_datetime_not_overdue(self):
        future = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        task = {"due": {"datetime": future}}
        assert _is_timed_overdue(task) is False

    def test_no_datetime_not_overdue(self):
        assert _is_timed_overdue({"due": {"date": "2026-02-18"}}) is False
        assert _is_timed_overdue({"due": None}) is False
        assert _is_timed_overdue({}) is False


# ── Task Summary ─────────────────────────────────────────────────────────────


class TestTaskSummary:

    def test_basic_summary(self):
        task = {"id": "123", "content": "Buy milk", "priority": 2, "due": None}
        summary = _task_summary(task)
        assert summary == {"id": "123", "content": "Buy milk", "priority": 2}

    def test_summary_with_due_date(self):
        task = {
            "id": "456",
            "content": "Review PR",
            "priority": 3,
            "due": {"date": "2026-02-18"},
        }
        summary = _task_summary(task)
        assert summary["due"] == "2026-02-18"

    def test_summary_with_datetime(self):
        task = {
            "id": "789",
            "content": "Meeting",
            "priority": 4,
            "due": {"date": "2026-02-18", "datetime": "2026-02-18T14:00:00Z"},
        }
        summary = _task_summary(task)
        assert summary["due_datetime"] == "2026-02-18T14:00:00Z"


# ── Classification ───────────────────────────────────────────────────────────


class TestClassifyTasks:

    def test_full_classification(self, sample_tasks):
        result = classify_tasks(sample_tasks, "111")

        # Overdue: task 1 (yesterday's date)
        assert len(result["overdue"]) == 1
        assert result["overdue"][0]["id"] == "1"

        # Due today: task 2 and task 6 (both today)
        due_today_ids = {t["id"] for t in result["due_today"]}
        assert "2" in due_today_ids
        assert "6" in due_today_ids

        # Inbox unprocessed: task 4 only (task 5 has labels)
        assert len(result["inbox_unprocessed"]) == 1
        assert result["inbox_unprocessed"][0]["id"] == "4"

        # Scheduled actions: task 6
        assert len(result["scheduled_actions"]) == 1
        assert result["scheduled_actions"][0]["id"] == "6"

    def test_empty_tasks(self):
        result = classify_tasks([], "111")
        assert all(len(v) == 0 for v in result.values())

    def test_no_inbox_id(self, sample_tasks):
        """With no inbox ID, no tasks should be classified as inbox_unprocessed."""
        result = classify_tasks(sample_tasks, "")
        assert len(result["inbox_unprocessed"]) == 0

    def test_timed_overdue_today(self):
        """A task due today but with a past datetime should be overdue, not due_today."""
        past_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        tasks = [
            {
                "id": "1",
                "content": "Past meeting",
                "priority": 4,
                "project_id": "222",
                "labels": [],
                "due": {
                    "date": date.today().isoformat(),
                    "datetime": past_time,
                },
            }
        ]
        result = classify_tasks(tasks, "111")
        assert len(result["overdue"]) == 1
        assert len(result["due_today"]) == 0

    def test_task_in_multiple_categories(self):
        """A scheduled task that's overdue should appear in both categories."""
        yesterday = (date.today() - timedelta(days=1)).isoformat()
        tasks = [
            {
                "id": "1",
                "content": "Overdue scheduled",
                "priority": 2,
                "project_id": "222",
                "labels": ["jarvis-scheduled"],
                "due": {"date": yesterday},
            }
        ]
        result = classify_tasks(tasks, "111")
        assert len(result["overdue"]) == 1
        assert len(result["scheduled_actions"]) == 1


# ── Cache I/O ────────────────────────────────────────────────────────────────


class TestCacheIO:

    def test_write_and_read_cache(self, temp_state_dir):
        cache_path = temp_state_dir / "todoist_cache.json"
        classified = {
            "overdue": [{"id": "1", "content": "Test", "priority": 3}],
            "due_today": [],
            "inbox_unprocessed": [],
            "scheduled_actions": [],
        }
        _write_cache(cache_path, classified, "123")

        assert cache_path.exists()
        with open(cache_path) as f:
            data = json.load(f)

        assert data["inbox_project_id"] == "123"
        assert data["counts"]["overdue"] == 1
        assert data["counts"]["due_today"] == 0
        assert len(data["alerts"]["overdue"]) == 1
        assert "synced_at" in data

    def test_atomic_write_creates_parent(self, temp_state_dir):
        """Cache write should create parent directories."""
        nested_path = temp_state_dir / "nested" / "deep" / "cache.json"
        # _write_cache creates parents
        _write_cache(nested_path, {"overdue": [], "due_today": [], "inbox_unprocessed": [], "scheduled_actions": []}, "")
        assert nested_path.exists()

    def test_overwrite_existing_cache(self, temp_state_dir):
        cache_path = temp_state_dir / "todoist_cache.json"
        # First write
        _write_cache(cache_path, {"overdue": [{"id": "1"}], "due_today": [], "inbox_unprocessed": [], "scheduled_actions": []}, "")
        # Overwrite
        _write_cache(cache_path, {"overdue": [], "due_today": [], "inbox_unprocessed": [], "scheduled_actions": []}, "")
        with open(cache_path) as f:
            data = json.load(f)
        assert data["counts"]["overdue"] == 0


# ── Cache Path ───────────────────────────────────────────────────────────────


class TestCachePath:

    def test_default_path(self, monkeypatch):
        monkeypatch.delenv("JARVIS_HOME", raising=False)
        path = _get_cache_path()
        assert path == Path.home() / ".jarvis" / "state" / "todoist_cache.json"

    def test_jarvis_home_override(self, monkeypatch):
        monkeypatch.setenv("JARVIS_HOME", "/custom/jarvis")
        path = _get_cache_path()
        assert path == Path("/custom/jarvis/state/todoist_cache.json")


# ── Sync Pipeline ────────────────────────────────────────────────────────────


class TestSyncOnce:

    def test_no_token_returns_failure(self, monkeypatch):
        monkeypatch.delenv("TODOIST_API_TOKEN", raising=False)
        monkeypatch.setattr("tools.config.get_config", lambda: {})
        from tools.config import clear_config_cache
        clear_config_cache()

        result = sync_once({})
        assert result["success"] is False
        assert result["reason"] == "no_token"

    def test_successful_sync(self, monkeypatch, temp_state_dir, sample_tasks, sample_projects):
        monkeypatch.setenv("TODOIST_API_TOKEN", "test-token")
        monkeypatch.setenv("JARVIS_HOME", str(temp_state_dir))

        with patch("tools.todoist_sync._fetch_tasks", return_value=sample_tasks), \
             patch("tools.todoist_sync._fetch_projects", return_value=sample_projects):
            result = sync_once({"api_timeout_seconds": 5})

        assert result["success"] is True
        assert result["counts"]["overdue"] == 1
        # Verify cache file was written
        cache_path = temp_state_dir / "state" / "todoist_cache.json"
        assert cache_path.exists()

    def test_api_error_propagates(self, monkeypatch):
        monkeypatch.setenv("TODOIST_API_TOKEN", "test-token")
        with patch("tools.todoist_sync._fetch_tasks", side_effect=Exception("API error")):
            with pytest.raises(Exception, match="API error"):
                sync_once({"api_timeout_seconds": 5})


# ── Config Getter ────────────────────────────────────────────────────────────


class TestConfig:

    def test_defaults(self, monkeypatch):
        monkeypatch.setattr("tools.config.get_config", lambda: {})
        from tools.config import clear_config_cache, get_todoist_prompt_alerts_config
        clear_config_cache()

        config = get_todoist_prompt_alerts_config()
        assert config["enabled"] is False
        assert config["sync_interval_seconds"] == 900
        assert config["max_per_category"] == 3
        assert config["api_timeout_seconds"] == 5
        assert config["debug"] is False

    def test_overrides(self, monkeypatch):
        mock_config = {
            "todoist": {
                "prompt_alerts": {
                    "enabled": True,
                    "sync_interval_seconds": 300,
                }
            }
        }
        monkeypatch.setattr("tools.config.get_config", lambda: mock_config)
        from tools.config import clear_config_cache, get_todoist_prompt_alerts_config
        clear_config_cache()

        config = get_todoist_prompt_alerts_config()
        assert config["enabled"] is True
        assert config["sync_interval_seconds"] == 300
        # Defaults still present for unset keys
        assert config["max_per_category"] == 3


# ── Background Loop ──────────────────────────────────────────────────────────


class TestSyncLoop:

    def test_loop_exits_when_disabled(self, monkeypatch):
        """Loop should exit early when feature is disabled."""
        monkeypatch.setattr(
            "tools.config.get_todoist_prompt_alerts_config",
            lambda: {"enabled": False},
        )
        # Patch startup delay to 0
        monkeypatch.setattr("tools.todoist_sync._STARTUP_DELAY", 0)

        # Should complete without hanging
        asyncio.run(asyncio.wait_for(todoist_sync_loop(), timeout=2))

    def test_loop_exits_when_no_token(self, monkeypatch):
        """Loop should exit early when no token is available."""
        monkeypatch.setattr(
            "tools.config.get_todoist_prompt_alerts_config",
            lambda: {"enabled": True},
        )
        monkeypatch.delenv("TODOIST_API_TOKEN", raising=False)
        monkeypatch.setattr("tools.config.get_config", lambda: {})
        from tools.config import clear_config_cache
        clear_config_cache()
        monkeypatch.setattr("tools.todoist_sync._STARTUP_DELAY", 0)

        asyncio.run(asyncio.wait_for(todoist_sync_loop(), timeout=2))


# ── Background Task Registry ─────────────────────────────────────────────────


class TestBackgroundTaskRegistry:

    def test_todoist_sync_in_background_tasks(self):
        """get_background_tasks() should include todoist_sync_loop."""
        import server

        tasks = server.get_background_tasks()
        # At least 3 tasks: pattern_detection, todoist_sync, sync_worker
        # (+ pull sync loops when sync is enabled with remotes)
        assert len(tasks) >= 3
        # Each should be a coroutine
        for t in tasks:
            assert asyncio.iscoroutine(t)
            t.close()  # Clean up coroutines
