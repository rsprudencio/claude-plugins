"""Tests for todoist_api module."""
import json
import os
from datetime import date, timedelta
from unittest.mock import MagicMock, patch, mock_open

import pytest
import httpx

# Ensure the mcp-server directory is importable
import sys
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import todoist_api


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
def mock_client(mock_config):
    """Provide a mocked httpx.Client that's injected into the module."""
    mock = MagicMock(spec=httpx.Client)
    with patch("todoist_api.httpx.Client", return_value=mock):
        yield mock


def make_response(data, status_code=200):
    """Create a mock httpx.Response."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json.return_value = data
    resp.text = json.dumps(data)
    resp.headers = {}
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            f"HTTP {status_code}",
            request=MagicMock(),
            response=resp,
        )
    else:
        resp.raise_for_status.return_value = None
    return resp


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

    def test_client_sets_auth_header(self, mock_config):
        with patch("todoist_api.httpx.Client") as MockClient:
            client = todoist_api._get_client()
            call_kwargs = MockClient.call_args[1]
            assert call_kwargs["headers"]["Authorization"] == "Bearer test-token-abc123"

    def test_client_singleton(self, mock_config):
        with patch("todoist_api.httpx.Client") as MockClient:
            c1 = todoist_api._get_client()
            c2 = todoist_api._get_client()
            assert MockClient.call_count == 1


# ---------------------------------------------------------------------------
# find_tasks Tests
# ---------------------------------------------------------------------------


class TestFindTasks:
    def test_find_all(self, mock_client):
        tasks = [{"id": "1", "content": "Task 1", "labels": []}]
        mock_client.get.return_value = make_response(tasks)

        result = todoist_api.find_tasks()
        assert result["success"] is True
        assert result["count"] == 1
        assert result["tasks"] == tasks

    def test_find_by_project(self, mock_client):
        tasks = [{"id": "1", "content": "Task 1", "labels": []}]
        mock_client.get.return_value = make_response(tasks)

        result = todoist_api.find_tasks(project_id="proj123")
        assert result["success"] is True
        mock_client.get.assert_called_once()
        call_kwargs = mock_client.get.call_args
        assert call_kwargs[1]["params"]["project_id"] == "proj123"

    def test_find_by_inbox(self, mock_client):
        projects = [
            {"id": "inbox123", "name": "Inbox", "is_inbox_project": True},
            {"id": "proj1", "name": "Work", "is_inbox_project": False},
        ]
        tasks = [{"id": "1", "content": "Inbox task", "labels": []}]
        mock_client.get.side_effect = [
            make_response(projects),  # inbox resolution
            make_response(tasks),     # actual query
        ]

        result = todoist_api.find_tasks(project_id="inbox")
        assert result["success"] is True
        assert result["count"] == 1

    def test_find_by_labels(self, mock_client):
        tasks = [
            {"id": "1", "content": "T1", "labels": ["jarvis-scheduled"]},
            {"id": "2", "content": "T2", "labels": ["other"]},
        ]
        mock_client.get.return_value = make_response(tasks)

        result = todoist_api.find_tasks(labels=["jarvis-scheduled"])
        assert result["success"] is True
        # Only task with the label
        assert result["count"] == 1
        assert result["tasks"][0]["id"] == "1"

    def test_find_by_search_text(self, mock_client):
        tasks = [
            {"id": "1", "content": "Buy groceries", "description": "", "labels": []},
            {"id": "2", "content": "Call dentist", "description": "", "labels": []},
        ]
        mock_client.get.return_value = make_response(tasks)

        result = todoist_api.find_tasks(search_text="groceries")
        assert result["success"] is True
        assert result["count"] == 1
        assert result["tasks"][0]["id"] == "1"

    def test_find_with_limit(self, mock_client):
        tasks = [{"id": str(i), "content": f"T{i}", "labels": []} for i in range(10)]
        mock_client.get.return_value = make_response(tasks)

        result = todoist_api.find_tasks(limit=3)
        assert result["count"] == 3

    def test_find_by_section(self, mock_client):
        tasks = [{"id": "1", "content": "Task", "labels": []}]
        mock_client.get.return_value = make_response(tasks)

        result = todoist_api.find_tasks(section_id="sec123")
        assert result["success"] is True
        call_kwargs = mock_client.get.call_args
        assert call_kwargs[1]["params"]["section_id"] == "sec123"


# ---------------------------------------------------------------------------
# find_tasks_by_date Tests
# ---------------------------------------------------------------------------


class TestFindTasksByDate:
    def test_today_with_overdue(self, mock_client):
        tasks = [{"id": "1", "content": "Due today", "labels": []}]
        mock_client.get.return_value = make_response(tasks)

        result = todoist_api.find_tasks_by_date(start_date="today")
        assert result["success"] is True
        assert result["filter"] == "overdue | today"

    def test_overdue_only(self, mock_client):
        mock_client.get.return_value = make_response([])

        result = todoist_api.find_tasks_by_date(overdue_option="overdue-only")
        assert result["filter"] == "overdue"

    def test_exclude_overdue(self, mock_client):
        mock_client.get.return_value = make_response([])
        today = date.today()

        result = todoist_api.find_tasks_by_date(
            start_date=today.isoformat(), overdue_option="exclude-overdue"
        )
        assert result["success"] is True
        assert "overdue" not in result["filter"]

    def test_date_range(self, mock_client):
        mock_client.get.return_value = make_response([])

        result = todoist_api.find_tasks_by_date(
            start_date="2026-02-10", days_count=5, overdue_option="exclude-overdue"
        )
        assert result["success"] is True
        assert "due before:" in result["filter"]

    def test_label_filter(self, mock_client):
        tasks = [
            {"id": "1", "content": "T1", "labels": ["work"]},
            {"id": "2", "content": "T2", "labels": []},
        ]
        mock_client.get.return_value = make_response(tasks)

        result = todoist_api.find_tasks_by_date(labels=["work"])
        assert result["count"] == 1
        assert result["tasks"][0]["id"] == "1"


# ---------------------------------------------------------------------------
# add_tasks Tests
# ---------------------------------------------------------------------------


class TestAddTasks:
    def test_add_single(self, mock_client):
        created = {"id": "new1", "content": "New task"}
        mock_client.post.return_value = make_response(created)

        result = todoist_api.add_tasks([{"content": "New task"}])
        assert result["success"] is True
        assert result["created_count"] == 1
        assert result["created"][0]["id"] == "new1"

    def test_add_multiple(self, mock_client):
        mock_client.post.side_effect = [
            make_response({"id": f"new{i}", "content": f"Task {i}"})
            for i in range(3)
        ]

        tasks = [{"content": f"Task {i}"} for i in range(3)]
        result = todoist_api.add_tasks(tasks)
        assert result["success"] is True
        assert result["created_count"] == 3

    def test_add_with_all_fields(self, mock_client):
        created = {"id": "new1", "content": "Work task"}
        mock_client.post.return_value = make_response(created)

        result = todoist_api.add_tasks([{
            "content": "Work task",
            "description": "Details here",
            "dueString": "tomorrow",
            "priority": "p1",
            "labels": ["work"],
            "projectId": "proj1",
        }])
        assert result["success"] is True
        # Verify the POST body was constructed correctly
        call_body = mock_client.post.call_args[1]["json"]
        assert call_body["content"] == "Work task"
        assert call_body["due_string"] == "tomorrow"
        assert call_body["priority"] == 4  # p1 -> 4
        assert call_body["project_id"] == "proj1"

    def test_add_with_duration(self, mock_client):
        mock_client.post.return_value = make_response({"id": "1", "content": "T"})

        todoist_api.add_tasks([{"content": "T", "duration": "2h30m"}])
        call_body = mock_client.post.call_args[1]["json"]
        assert call_body["duration"] == 150
        assert call_body["duration_unit"] == "minute"

    def test_add_with_duration_minutes_only(self, mock_client):
        mock_client.post.return_value = make_response({"id": "1", "content": "T"})

        todoist_api.add_tasks([{"content": "T", "duration": "90m"}])
        call_body = mock_client.post.call_args[1]["json"]
        assert call_body["duration"] == 90

    def test_add_with_inbox_resolution(self, mock_client):
        projects = [{"id": "inbox99", "name": "Inbox", "is_inbox_project": True}]
        mock_client.get.return_value = make_response(projects)  # inbox resolution
        mock_client.post.return_value = make_response({"id": "1", "content": "T"})

        todoist_api.add_tasks([{"content": "T", "projectId": "inbox"}])
        call_body = mock_client.post.call_args[1]["json"]
        assert call_body["project_id"] == "inbox99"

    def test_add_partial_failure(self, mock_client):
        mock_client.post.side_effect = [
            make_response({"id": "1", "content": "OK"}),
            make_response({"error": "bad"}, status_code=400),
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
    def test_complete_single(self, mock_client):
        mock_client.post.return_value = make_response(None, 204)
        # 204 doesn't raise
        mock_client.post.return_value.raise_for_status.return_value = None

        result = todoist_api.complete_tasks(["task1"])
        assert result["success"] is True
        assert result["completed"] == ["task1"]
        assert result["completed_count"] == 1

    def test_complete_multiple(self, mock_client):
        resp = make_response(None, 204)
        resp.raise_for_status.return_value = None
        mock_client.post.return_value = resp

        result = todoist_api.complete_tasks(["t1", "t2", "t3"])
        assert result["success"] is True
        assert result["completed_count"] == 3

    def test_complete_not_found(self, mock_client):
        mock_client.post.return_value = make_response({"error": "not found"}, status_code=404)

        result = todoist_api.complete_tasks(["bad_id"])
        assert result["success"] is False
        assert len(result["errors"]) == 1
        assert result["errors"][0]["task_id"] == "bad_id"


# ---------------------------------------------------------------------------
# update_tasks Tests
# ---------------------------------------------------------------------------


class TestUpdateTasks:
    def test_update_labels(self, mock_client):
        updated = {"id": "t1", "labels": ["jarvis-ingested"]}
        mock_client.post.return_value = make_response(updated)

        result = todoist_api.update_tasks([{
            "id": "t1",
            "labels": ["jarvis-ingested"],
        }])
        assert result["success"] is True
        assert result["updated_count"] == 1

    def test_update_content(self, mock_client):
        mock_client.post.return_value = make_response({"id": "t1", "content": "New title"})

        result = todoist_api.update_tasks([{"id": "t1", "content": "New title"}])
        assert result["success"] is True
        call_body = mock_client.post.call_args[1]["json"]
        assert call_body["content"] == "New title"

    def test_update_priority(self, mock_client):
        mock_client.post.return_value = make_response({"id": "t1"})

        todoist_api.update_tasks([{"id": "t1", "priority": "p2"}])
        call_body = mock_client.post.call_args[1]["json"]
        assert call_body["priority"] == 3  # p2 -> 3

    def test_remove_deadline(self, mock_client):
        mock_client.post.return_value = make_response({"id": "t1"})

        todoist_api.update_tasks([{"id": "t1", "deadlineDate": "remove"}])
        call_body = mock_client.post.call_args[1]["json"]
        assert call_body["deadline_date"] is None

    def test_update_partial_failure(self, mock_client):
        mock_client.post.side_effect = [
            make_response({"id": "t1"}),
            make_response({"error": "bad"}, status_code=400),
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
    @pytest.mark.parametrize("obj_type,endpoint", [
        ("task", "tasks"),
        ("project", "projects"),
        ("section", "sections"),
        ("comment", "comments"),
    ])
    def test_delete_types(self, mock_client, obj_type, endpoint):
        resp = make_response(None, 204)
        resp.raise_for_status.return_value = None
        mock_client.delete.return_value = resp

        result = todoist_api.delete_object(obj_type, "id123")
        assert result["success"] is True
        assert result["deleted_type"] == obj_type
        mock_client.delete.assert_called_once_with(
            f"{todoist_api.REST_BASE}/{endpoint}/id123"
        )

    def test_delete_invalid_type(self, mock_client):
        result = todoist_api.delete_object("invalid", "id123")
        assert result["success"] is False
        assert "Invalid type" in result["error"]

    def test_delete_not_found(self, mock_client):
        mock_client.delete.return_value = make_response({"error": "not found"}, status_code=404)

        result = todoist_api.delete_object("task", "bad_id")
        assert result["success"] is False
        assert result["status_code"] == 404


# ---------------------------------------------------------------------------
# user_info Tests
# ---------------------------------------------------------------------------


class TestUserInfo:
    def test_user_info_success(self, mock_client):
        sync_response = {
            "user": {
                "id": 12345,
                "full_name": "Test User",
                "email": "test@example.com",
                "tz_info": {"timezone": "US/Eastern"},
                "start_day": 1,
                "premium_until": None,
            }
        }
        mock_client.post.return_value = make_response(sync_response)

        result = todoist_api.user_info()
        assert result["success"] is True
        assert result["user"]["full_name"] == "Test User"
        assert result["user"]["email"] == "test@example.com"

    def test_user_info_calls_sync_api(self, mock_client):
        mock_client.post.return_value = make_response({"user": {}})

        todoist_api.user_info()
        call_args = mock_client.post.call_args
        assert todoist_api.SYNC_URL in call_args[0]
        body = call_args[1]["json"]
        assert body["resource_types"] == ["user"]


# ---------------------------------------------------------------------------
# find_projects Tests
# ---------------------------------------------------------------------------


class TestFindProjects:
    def test_find_all(self, mock_client):
        projects = [
            {"id": "p1", "name": "Work"},
            {"id": "p2", "name": "Personal"},
        ]
        mock_client.get.return_value = make_response(projects)

        result = todoist_api.find_projects()
        assert result["success"] is True
        assert result["count"] == 2

    def test_find_with_search(self, mock_client):
        projects = [
            {"id": "p1", "name": "Work"},
            {"id": "p2", "name": "Personal"},
        ]
        mock_client.get.return_value = make_response(projects)

        result = todoist_api.find_projects(search="work")
        assert result["success"] is True
        assert result["count"] == 1
        assert result["projects"][0]["name"] == "Work"

    def test_find_case_insensitive(self, mock_client):
        projects = [{"id": "p1", "name": "My Project"}]
        mock_client.get.return_value = make_response(projects)

        result = todoist_api.find_projects(search="MY PROJECT")
        assert result["count"] == 1


# ---------------------------------------------------------------------------
# add_projects Tests
# ---------------------------------------------------------------------------


class TestAddProjects:
    def test_add_single(self, mock_client):
        mock_client.post.return_value = make_response({"id": "new1", "name": "MyProject"})

        result = todoist_api.add_projects([{"name": "MyProject"}])
        assert result["success"] is True
        assert result["created_count"] == 1

    def test_add_multiple(self, mock_client):
        mock_client.post.side_effect = [
            make_response({"id": f"p{i}", "name": f"Proj{i}"})
            for i in range(2)
        ]

        result = todoist_api.add_projects([
            {"name": "Proj0"},
            {"name": "Proj1"},
        ])
        assert result["success"] is True
        assert result["created_count"] == 2

    def test_add_with_parent(self, mock_client):
        mock_client.post.return_value = make_response({"id": "sub1", "name": "Sub"})

        todoist_api.add_projects([{"name": "Sub", "parentId": "parent1"}])
        call_body = mock_client.post.call_args[1]["json"]
        assert call_body["parent_id"] == "parent1"

    def test_add_with_view_style(self, mock_client):
        mock_client.post.return_value = make_response({"id": "p1", "name": "Board"})

        todoist_api.add_projects([{"name": "Board", "viewStyle": "board"}])
        call_body = mock_client.post.call_args[1]["json"]
        assert call_body["view_style"] == "board"


# ---------------------------------------------------------------------------
# Error Handling Tests
# ---------------------------------------------------------------------------


class TestErrorHandling:
    def test_401_unauthorized(self, mock_client):
        mock_client.get.return_value = make_response({"error": "unauthorized"}, status_code=401)

        result = todoist_api.find_tasks()
        assert result["success"] is False
        assert "Unauthorized" in result["error"]
        assert result["status_code"] == 401

    def test_403_forbidden(self, mock_client):
        mock_client.get.return_value = make_response({"error": "forbidden"}, status_code=403)

        result = todoist_api.find_tasks()
        assert result["success"] is False
        assert "Forbidden" in result["error"]

    def test_404_not_found(self, mock_client):
        mock_client.get.return_value = make_response({"error": "not found"}, status_code=404)

        result = todoist_api.find_tasks()
        assert result["success"] is False
        assert "Not found" in result["error"]

    def test_429_rate_limit(self, mock_client):
        resp = make_response({"error": "rate limited"}, status_code=429)
        resp.headers = {"Retry-After": "30"}
        mock_client.get.return_value = resp

        result = todoist_api.find_tasks()
        assert result["success"] is False
        assert "Rate limited" in result["error"]
        assert result["retry_after_seconds"] == 30

    def test_500_server_error(self, mock_client):
        mock_client.get.return_value = make_response({"error": "server error"}, status_code=500)

        result = todoist_api.find_tasks()
        assert result["success"] is False
        assert "500" in result["error"]

    def test_timeout(self, mock_client):
        mock_client.get.side_effect = httpx.TimeoutException("timed out")

        result = todoist_api.find_tasks()
        assert result["success"] is False
        assert "timed out" in result["error"]

    def test_connection_error(self, mock_client):
        mock_client.get.side_effect = httpx.ConnectError("connection refused")

        result = todoist_api.find_tasks()
        assert result["success"] is False
        assert "Could not connect" in result["error"]

    def test_generic_request_error(self, mock_client):
        mock_client.get.side_effect = httpx.RequestError("something broke")

        result = todoist_api.find_tasks()
        assert result["success"] is False
        assert "Request error" in result["error"]

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
    def test_inbox_id_cached(self, mock_client):
        projects = [{"id": "inbox1", "name": "Inbox", "is_inbox_project": True}]
        tasks = [{"id": "t1", "content": "Task", "labels": []}]
        mock_client.get.side_effect = [
            make_response(projects),  # first call: resolve inbox
            make_response(tasks),     # first query
            make_response(tasks),     # second query (should NOT resolve inbox again)
        ]

        todoist_api.find_tasks(project_id="inbox")
        todoist_api.find_tasks(project_id="inbox")

        # Projects should only be fetched once (cached)
        get_calls = mock_client.get.call_args_list
        project_calls = [c for c in get_calls if "projects" in str(c)]
        assert len(project_calls) == 1

    def test_inbox_not_found(self, mock_client):
        projects = [{"id": "p1", "name": "Work", "is_inbox_project": False}]
        mock_client.get.return_value = make_response(projects)

        result = todoist_api.find_tasks(project_id="inbox")
        assert result["success"] is False
        assert "inbox project" in result["error"].lower()

    def test_normal_project_id_not_resolved(self, mock_client):
        tasks = [{"id": "t1", "content": "Task", "labels": []}]
        mock_client.get.return_value = make_response(tasks)

        todoist_api.find_tasks(project_id="proj123")
        # Should NOT fetch projects - just use the ID directly
        assert mock_client.get.call_count == 1


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
