"""Tests for todoist_api module (SDK-based)."""
import json
import os
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

# Ensure the mcp-server directory is importable
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import todoist_api


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_task(**kwargs):
    """Create a mock Task object."""
    defaults = {
        "id": "t1",
        "content": "Test task",
        "description": "",
        "labels": [],
        "priority": 1,
        "order": 0,
        "project_id": "p1",
        "section_id": None,
        "parent_id": None,
        "creator_id": "u1",
        "is_completed": False,
        "url": "https://todoist.com/showTask?id=t1",
        "due": None,
        "duration": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_project(**kwargs):
    """Create a mock Project object."""
    defaults = {
        "id": "p1",
        "name": "Inbox",
        "color": "grey",
        "is_inbox_project": True,
        "is_favorite": False,
        "view_style": "list",
        "parent_id": None,
        "order": 0,
        "url": "https://todoist.com/showProject?id=p1",
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_due(**kwargs):
    """Create a mock Due object."""
    defaults = {
        "date": str(date.today()),
        "string": "today",
        "is_recurring": False,
        "datetime": None,
        "timezone": None,
    }
    defaults.update(kwargs)
    return SimpleNamespace(**defaults)


def make_duration(amount=60, unit="minute"):
    return SimpleNamespace(amount=amount, unit=unit)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_state():
    """Reset module-level singleton state between tests."""
    todoist_api.reset_client()
    yield
    todoist_api.reset_client()


@pytest.fixture
def mock_config(tmp_path):
    """Create a temporary config file with a valid token."""
    config = {"todoist": {"api_token": "test-token-abc123"}}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config))
    with patch.dict(os.environ, {}):
        with patch("todoist_api.os.path.expanduser", return_value=str(config_path)):
            yield config_path


@pytest.fixture
def mock_api(mock_config):
    """Provide a mocked TodoistAPI that's injected into the module.

    We patch _get_api() directly since TodoistAPI is lazily imported
    inside that function — it's not a module-level attribute.
    """
    mock = MagicMock()
    with patch("todoist_api._get_api", return_value=mock):
        yield mock


# ---------------------------------------------------------------------------
# Config & Auth Tests
# ---------------------------------------------------------------------------


class TestConfig:
    def test_get_token_success(self, mock_config):
        token = todoist_api._get_token()
        assert token == "test-token-abc123"

    def test_get_token_missing_config(self, tmp_path):
        missing = str(tmp_path / "nonexistent.json")
        with patch("todoist_api.os.path.expanduser", return_value=missing):
            with pytest.raises(ValueError, match="config not found"):
                todoist_api._get_token()

    def test_get_token_empty_token(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"todoist": {"api_token": ""}}))
        with patch("todoist_api.os.path.expanduser", return_value=str(config_path)):
            with pytest.raises(ValueError, match="No Todoist API token"):
                todoist_api._get_token()

    def test_get_token_no_todoist_section(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({"vault_path": "/tmp"}))
        with patch("todoist_api.os.path.expanduser", return_value=str(config_path)):
            with pytest.raises(ValueError, match="No Todoist API token"):
                todoist_api._get_token()


# ---------------------------------------------------------------------------
# find_tasks Tests
# ---------------------------------------------------------------------------


class TestFindTasks:
    def test_find_all(self, mock_api):
        tasks = [make_task(id="1", content="Task 1")]
        mock_api.get_tasks.return_value = iter([tasks])

        result = todoist_api.find_tasks()
        assert result["success"] is True
        assert result["count"] == 1
        assert result["tasks"][0]["id"] == "1"

    def test_find_by_project(self, mock_api):
        tasks = [make_task(id="1", content="Task 1")]
        mock_api.get_tasks.return_value = iter([tasks])

        result = todoist_api.find_tasks(project_id="proj123")
        assert result["success"] is True
        mock_api.get_tasks.assert_called_once_with(project_id="proj123")

    def test_find_by_inbox(self, mock_api):
        inbox = make_project(id="inbox123", is_inbox_project=True)
        work = make_project(id="p2", name="Work", is_inbox_project=False)
        mock_api.get_projects.return_value = iter([[inbox, work]])
        mock_api.get_tasks.return_value = iter([[make_task()]])

        result = todoist_api.find_tasks(project_id="inbox")
        assert result["success"] is True
        mock_api.get_tasks.assert_called_once_with(project_id="inbox123")

    def test_find_by_labels(self, mock_api):
        tasks = [
            make_task(id="1", content="T1", labels=["jarvis-scheduled"]),
            make_task(id="2", content="T2", labels=["other"]),
        ]
        mock_api.get_tasks.return_value = iter([tasks])

        result = todoist_api.find_tasks(labels=["jarvis-scheduled"])
        assert result["success"] is True
        assert result["count"] == 1
        assert result["tasks"][0]["id"] == "1"

    def test_find_by_search_text(self, mock_api):
        tasks = [
            make_task(id="1", content="Buy groceries", description=""),
            make_task(id="2", content="Call dentist", description=""),
        ]
        mock_api.get_tasks.return_value = iter([tasks])

        result = todoist_api.find_tasks(search_text="groceries")
        assert result["success"] is True
        assert result["count"] == 1
        assert result["tasks"][0]["id"] == "1"

    def test_find_with_limit(self, mock_api):
        tasks = [make_task(id=str(i), content=f"T{i}") for i in range(10)]
        mock_api.get_tasks.return_value = iter([tasks])

        result = todoist_api.find_tasks(limit=3)
        assert result["count"] == 3

    def test_find_by_section(self, mock_api):
        tasks = [make_task(id="1")]
        mock_api.get_tasks.return_value = iter([tasks])

        result = todoist_api.find_tasks(section_id="sec123")
        assert result["success"] is True
        mock_api.get_tasks.assert_called_once_with(section_id="sec123")

    def test_find_with_due_date(self, mock_api):
        due = make_due(date="2026-02-15", string="Feb 15")
        tasks = [make_task(id="1", due=due)]
        mock_api.get_tasks.return_value = iter([tasks])

        result = todoist_api.find_tasks()
        assert result["tasks"][0]["due"]["date"] == "2026-02-15"

    def test_find_with_duration(self, mock_api):
        dur = make_duration(amount=90, unit="minute")
        tasks = [make_task(id="1", duration=dur)]
        mock_api.get_tasks.return_value = iter([tasks])

        result = todoist_api.find_tasks()
        assert result["tasks"][0]["duration"]["amount"] == 90


# ---------------------------------------------------------------------------
# find_tasks_by_date Tests
# ---------------------------------------------------------------------------


class TestFindTasksByDate:
    def test_today_with_overdue(self, mock_api):
        mock_api.filter_tasks.return_value = iter([[]])

        result = todoist_api.find_tasks_by_date(start_date="today")
        assert result["success"] is True
        assert result["filter"] == "overdue | today"

    def test_overdue_only(self, mock_api):
        mock_api.filter_tasks.return_value = iter([[]])

        result = todoist_api.find_tasks_by_date(overdue_option="overdue-only")
        assert result["filter"] == "overdue"

    def test_exclude_overdue(self, mock_api):
        mock_api.filter_tasks.return_value = iter([[]])
        today = date.today()

        result = todoist_api.find_tasks_by_date(
            start_date=today.isoformat(), overdue_option="exclude-overdue"
        )
        assert result["success"] is True
        assert "overdue" not in result["filter"]

    def test_date_range(self, mock_api):
        mock_api.filter_tasks.return_value = iter([[]])

        result = todoist_api.find_tasks_by_date(
            start_date="2026-02-10", days_count=5, overdue_option="exclude-overdue"
        )
        assert result["success"] is True
        assert "due before:" in result["filter"]

    def test_label_filter(self, mock_api):
        tasks = [
            make_task(id="1", labels=["work"]),
            make_task(id="2", labels=[]),
        ]
        mock_api.filter_tasks.return_value = iter([tasks])

        result = todoist_api.find_tasks_by_date(labels=["work"])
        assert result["count"] == 1
        assert result["tasks"][0]["id"] == "1"


# ---------------------------------------------------------------------------
# add_tasks Tests
# ---------------------------------------------------------------------------


class TestAddTasks:
    def test_add_single(self, mock_api):
        mock_api.add_task.return_value = make_task(id="new1", content="New task")

        result = todoist_api.add_tasks([{"content": "New task"}])
        assert result["success"] is True
        assert result["created_count"] == 1
        assert result["created"][0]["id"] == "new1"

    def test_add_multiple(self, mock_api):
        mock_api.add_task.side_effect = [
            make_task(id=f"new{i}", content=f"Task {i}")
            for i in range(3)
        ]

        tasks = [{"content": f"Task {i}"} for i in range(3)]
        result = todoist_api.add_tasks(tasks)
        assert result["success"] is True
        assert result["created_count"] == 3

    def test_add_with_all_fields(self, mock_api):
        mock_api.add_task.return_value = make_task(id="new1", content="Work task")

        result = todoist_api.add_tasks([{
            "content": "Work task",
            "description": "Details here",
            "dueString": "tomorrow",
            "priority": "p1",
            "labels": ["work"],
            "projectId": "proj1",
        }])
        assert result["success"] is True
        # Verify kwargs passed to SDK
        call_kwargs = mock_api.add_task.call_args[1]
        assert call_kwargs["content"] == "Work task"
        assert call_kwargs["due_string"] == "tomorrow"
        assert call_kwargs["priority"] == 4  # p1 -> 4
        assert call_kwargs["project_id"] == "proj1"

    def test_add_with_duration(self, mock_api):
        mock_api.add_task.return_value = make_task(id="1", content="T")

        todoist_api.add_tasks([{"content": "T", "duration": "2h30m"}])
        call_kwargs = mock_api.add_task.call_args[1]
        assert call_kwargs["duration"] == 150
        assert call_kwargs["duration_unit"] == "minute"

    def test_add_with_duration_minutes_only(self, mock_api):
        mock_api.add_task.return_value = make_task(id="1", content="T")

        todoist_api.add_tasks([{"content": "T", "duration": "90m"}])
        call_kwargs = mock_api.add_task.call_args[1]
        assert call_kwargs["duration"] == 90

    def test_add_with_inbox_resolution(self, mock_api):
        inbox = make_project(id="inbox99", is_inbox_project=True)
        mock_api.get_projects.return_value = iter([[inbox]])
        mock_api.add_task.return_value = make_task(id="1", content="T")

        todoist_api.add_tasks([{"content": "T", "projectId": "inbox"}])
        call_kwargs = mock_api.add_task.call_args[1]
        assert call_kwargs["project_id"] == "inbox99"

    def test_add_partial_failure(self, mock_api):
        mock_api.add_task.side_effect = [
            make_task(id="1", content="OK"),
            Exception("API error"),
        ]

        result = todoist_api.add_tasks([
            {"content": "OK"},
            {"content": "Fail"},
        ])
        assert result["success"] is False
        assert result["created_count"] == 1
        assert result["error_count"] == 1


# ---------------------------------------------------------------------------
# complete_tasks Tests
# ---------------------------------------------------------------------------


class TestCompleteTasks:
    def test_complete_single(self, mock_api):
        mock_api.complete_task.return_value = True

        result = todoist_api.complete_tasks(["task1"])
        assert result["success"] is True
        assert result["completed"] == ["task1"]
        assert result["completed_count"] == 1

    def test_complete_multiple(self, mock_api):
        mock_api.complete_task.return_value = True

        result = todoist_api.complete_tasks(["t1", "t2", "t3"])
        assert result["success"] is True
        assert result["completed_count"] == 3

    def test_complete_not_found(self, mock_api):
        mock_api.complete_task.side_effect = Exception("404 Not Found")

        result = todoist_api.complete_tasks(["bad_id"])
        assert result["success"] is False
        assert len(result["errors"]) == 1
        assert result["errors"][0]["task_id"] == "bad_id"


# ---------------------------------------------------------------------------
# update_tasks Tests
# ---------------------------------------------------------------------------


class TestUpdateTasks:
    def test_update_labels(self, mock_api):
        mock_api.update_task.return_value = make_task(id="t1", labels=["jarvis-ingested"])

        result = todoist_api.update_tasks([{
            "id": "t1",
            "labels": ["jarvis-ingested"],
        }])
        assert result["success"] is True
        assert result["updated_count"] == 1

    def test_update_content(self, mock_api):
        mock_api.update_task.return_value = make_task(id="t1", content="New title")

        result = todoist_api.update_tasks([{"id": "t1", "content": "New title"}])
        assert result["success"] is True
        call_kwargs = mock_api.update_task.call_args[1]
        assert call_kwargs["content"] == "New title"

    def test_update_priority(self, mock_api):
        mock_api.update_task.return_value = make_task(id="t1")

        todoist_api.update_tasks([{"id": "t1", "priority": "p2"}])
        call_kwargs = mock_api.update_task.call_args[1]
        assert call_kwargs["priority"] == 3  # p2 -> 3

    def test_remove_deadline(self, mock_api):
        mock_api.update_task.return_value = make_task(id="t1")

        todoist_api.update_tasks([{"id": "t1", "deadlineDate": "remove"}])
        call_kwargs = mock_api.update_task.call_args[1]
        assert call_kwargs["deadline_date"] is None

    def test_update_partial_failure(self, mock_api):
        mock_api.update_task.side_effect = [
            make_task(id="t1"),
            Exception("API error"),
        ]

        result = todoist_api.update_tasks([
            {"id": "t1", "content": "OK"},
            {"id": "t2", "content": "Fail"},
        ])
        assert result["success"] is False
        assert result["updated_count"] == 1


# ---------------------------------------------------------------------------
# delete_object Tests
# ---------------------------------------------------------------------------


class TestDeleteObject:
    @pytest.mark.parametrize("obj_type", ["task", "project", "section", "comment"])
    def test_delete_types(self, mock_api, obj_type):
        delete_fn = getattr(mock_api, f"delete_{obj_type}")
        delete_fn.return_value = True

        result = todoist_api.delete_object(obj_type, "id123")
        assert result["success"] is True
        assert result["deleted_type"] == obj_type
        delete_fn.assert_called_once_with("id123")

    def test_delete_invalid_type(self, mock_api):
        result = todoist_api.delete_object("invalid", "id123")
        assert result["success"] is False
        assert "Invalid type" in result["error"]

    def test_delete_not_found(self, mock_api):
        mock_api.delete_task.side_effect = Exception("404 Not Found")

        result = todoist_api.delete_object("task", "bad_id")
        assert result["success"] is False
        assert "Not found" in result["error"]


# ---------------------------------------------------------------------------
# find_projects Tests
# ---------------------------------------------------------------------------


class TestFindProjects:
    def test_find_all(self, mock_api):
        projects = [
            make_project(id="p1", name="Work", is_inbox_project=False),
            make_project(id="p2", name="Personal", is_inbox_project=False),
        ]
        mock_api.get_projects.return_value = iter([projects])

        result = todoist_api.find_projects()
        assert result["success"] is True
        assert result["count"] == 2

    def test_find_with_search(self, mock_api):
        projects = [
            make_project(id="p1", name="Work", is_inbox_project=False),
            make_project(id="p2", name="Personal", is_inbox_project=False),
        ]
        mock_api.get_projects.return_value = iter([projects])

        result = todoist_api.find_projects(search="work")
        assert result["success"] is True
        assert result["count"] == 1
        assert result["projects"][0]["name"] == "Work"

    def test_find_case_insensitive(self, mock_api):
        projects = [make_project(id="p1", name="My Project", is_inbox_project=False)]
        mock_api.get_projects.return_value = iter([projects])

        result = todoist_api.find_projects(search="MY PROJECT")
        assert result["count"] == 1


# ---------------------------------------------------------------------------
# add_projects Tests
# ---------------------------------------------------------------------------


class TestAddProjects:
    def test_add_single(self, mock_api):
        mock_api.add_project.return_value = make_project(id="new1", name="MyProject", is_inbox_project=False)

        result = todoist_api.add_projects([{"name": "MyProject"}])
        assert result["success"] is True
        assert result["created_count"] == 1

    def test_add_multiple(self, mock_api):
        mock_api.add_project.side_effect = [
            make_project(id=f"p{i}", name=f"Proj{i}", is_inbox_project=False)
            for i in range(2)
        ]

        result = todoist_api.add_projects([
            {"name": "Proj0"},
            {"name": "Proj1"},
        ])
        assert result["success"] is True
        assert result["created_count"] == 2

    def test_add_with_parent(self, mock_api):
        mock_api.add_project.return_value = make_project(id="sub1", name="Sub", is_inbox_project=False)

        todoist_api.add_projects([{"name": "Sub", "parentId": "parent1"}])
        call_kwargs = mock_api.add_project.call_args[1]
        assert call_kwargs["parent_id"] == "parent1"

    def test_add_with_view_style(self, mock_api):
        mock_api.add_project.return_value = make_project(id="p1", name="Board", is_inbox_project=False)

        todoist_api.add_projects([{"name": "Board", "viewStyle": "board"}])
        call_kwargs = mock_api.add_project.call_args[1]
        assert call_kwargs["view_style"] == "board"


# ---------------------------------------------------------------------------
# Error Handling Tests
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_401_unauthorized(self, mock_api):
        mock_api.get_tasks.side_effect = Exception("401 Unauthorized")

        result = todoist_api.find_tasks()
        assert result["success"] is False
        assert "Unauthorized" in result["error"]

    def test_403_forbidden(self, mock_api):
        mock_api.get_tasks.side_effect = Exception("403 Forbidden")

        result = todoist_api.find_tasks()
        assert result["success"] is False
        assert "Forbidden" in result["error"]

    def test_404_not_found(self, mock_api):
        mock_api.get_tasks.side_effect = Exception("404 Not Found")

        result = todoist_api.find_tasks()
        assert result["success"] is False
        assert "Not found" in result["error"]

    def test_429_rate_limit(self, mock_api):
        mock_api.get_tasks.side_effect = Exception("429 Too Many Requests")

        result = todoist_api.find_tasks()
        assert result["success"] is False
        assert "Rate limited" in result["error"]

    def test_generic_error(self, mock_api):
        mock_api.get_tasks.side_effect = Exception("Something broke")

        result = todoist_api.find_tasks()
        assert result["success"] is False
        assert "Something broke" in result["error"]

    def test_no_token_returns_error(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({}))
        with patch("todoist_api.os.path.expanduser", return_value=str(config_path)):
            result = todoist_api.find_tasks()
            assert result["success"] is False
            assert "No Todoist API token" in result["error"]


# ---------------------------------------------------------------------------
# Inbox Resolution Tests
# ---------------------------------------------------------------------------


class TestInboxResolution:
    def test_inbox_id_cached(self, mock_api):
        inbox = make_project(id="inbox1", is_inbox_project=True)
        mock_api.get_projects.return_value = iter([[inbox]])
        mock_api.get_tasks.return_value = iter([[make_task()]])

        todoist_api.find_tasks(project_id="inbox")

        # Second call shouldn't fetch projects again (cached)
        mock_api.get_tasks.return_value = iter([[make_task()]])
        todoist_api.find_tasks(project_id="inbox")

        assert mock_api.get_projects.call_count == 1

    def test_inbox_not_found(self, mock_api):
        work = make_project(id="p1", name="Work", is_inbox_project=False)
        mock_api.get_projects.return_value = iter([[work]])

        result = todoist_api.find_tasks(project_id="inbox")
        assert result["success"] is False
        assert "inbox project" in result["error"].lower()

    def test_normal_project_id_not_resolved(self, mock_api):
        mock_api.get_tasks.return_value = iter([[make_task()]])

        todoist_api.find_tasks(project_id="proj123")
        assert mock_api.get_projects.call_count == 0


# ---------------------------------------------------------------------------
# user_info Tests
# ---------------------------------------------------------------------------


class TestUserInfo:
    def test_user_info_success(self, mock_config):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "id": "u123",
            "full_name": "Test User",
            "email": "test@example.com",
            "tz_info": {"timezone": "US/Eastern"},
            "start_day": 1,
            "premium_until": None,
        }
        mock_response.raise_for_status = MagicMock()

        with patch("todoist_api.requests.get", return_value=mock_response) as mock_get:
            result = todoist_api.user_info()

        assert result["success"] is True
        assert result["user"]["id"] == "u123"
        assert result["user"]["full_name"] == "Test User"
        assert result["user"]["email"] == "test@example.com"
        mock_get.assert_called_once()
        call_kwargs = mock_get.call_args
        assert "Bearer" in call_kwargs[1]["headers"]["Authorization"]

    def test_user_info_401(self, mock_config):
        mock_response = MagicMock()
        mock_response.raise_for_status.side_effect = Exception("401 Unauthorized")

        with patch("todoist_api.requests.get", return_value=mock_response):
            result = todoist_api.user_info()

        assert result["success"] is False
        assert "Unauthorized" in result["error"]

    def test_user_info_no_token(self, tmp_path):
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps({}))
        with patch("todoist_api.os.path.expanduser", return_value=str(config_path)):
            result = todoist_api.user_info()
            assert result["success"] is False
            assert "No Todoist API token" in result["error"]


# ---------------------------------------------------------------------------
# Helper Tests
# ---------------------------------------------------------------------------


class TestHelpers:
    def test_parse_duration_hours(self):
        assert todoist_api._parse_duration("2h") == (120, "minute")

    def test_parse_duration_minutes(self):
        assert todoist_api._parse_duration("90m") == (90, "minute")

    def test_parse_duration_combined(self):
        assert todoist_api._parse_duration("2h30m") == (150, "minute")

    def test_parse_duration_decimal(self):
        assert todoist_api._parse_duration("1.5h") == (90, "minute")

    def test_task_to_dict(self):
        task = make_task(id="t1", content="Hello", due=make_due(), duration=make_duration(30))
        d = todoist_api._task_to_dict(task)
        assert d["id"] == "t1"
        assert d["content"] == "Hello"
        assert d["due"]["date"] == str(date.today())
        assert d["duration"]["amount"] == 30

    def test_project_to_dict(self):
        proj = make_project(id="p1", name="Work")
        d = todoist_api._project_to_dict(proj)
        assert d["id"] == "p1"
        assert d["name"] == "Work"

    def test_priority_map(self):
        assert todoist_api.PRIORITY_MAP["p1"] == 4
        assert todoist_api.PRIORITY_MAP["p4"] == 1


# ---------------------------------------------------------------------------
# Server Integration Tests
# ---------------------------------------------------------------------------


class TestServerHandlers:
    """Test that server.py correctly maps tool calls to API functions."""

    def test_all_tools_have_handlers(self):
        """Every tool in TOOLS list has a corresponding handler."""
        import server
        tool_names = {t.name for t in server.TOOLS}
        handler_names = set(server.HANDLERS.keys())
        assert tool_names == handler_names

    def test_tool_count(self):
        """Exactly 9 tools defined."""
        import server
        assert len(server.TOOLS) == 9

    def test_handler_unknown_tool(self):
        """Unknown tool name returns error."""
        import server
        handler = server.HANDLERS.get("nonexistent")
        assert handler is None
