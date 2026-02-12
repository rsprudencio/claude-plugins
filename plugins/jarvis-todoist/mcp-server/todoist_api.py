"""
Todoist REST API client for Jarvis.

Sync httpx client wrapping Todoist REST API v2 and Sync API v9.
Reads API token from ~/.jarvis/config.json → todoist.api_token.
"""
import json
import logging
import os
from datetime import date, timedelta

import httpx

logger = logging.getLogger("jarvis-todoist-api")

REST_BASE = "https://api.todoist.com/rest/v2"
SYNC_URL = "https://api.todoist.com/sync/v9/sync"
TIMEOUT = 30.0

_client: httpx.Client | None = None
_inbox_id: str | None = None


def _get_token() -> str:
    """Read API token from ~/.jarvis/config.json → todoist.api_token."""
    config_path = os.path.expanduser("~/.jarvis/config.json")
    if not os.path.exists(config_path):
        raise ValueError(
            "Jarvis config not found at ~/.jarvis/config.json. "
            "Run /jarvis-settings to configure."
        )
    with open(config_path) as f:
        config = json.load(f)
    token = config.get("todoist", {}).get("api_token", "")
    if not token:
        raise ValueError(
            "No Todoist API token configured. "
            "Add todoist.api_token to ~/.jarvis/config.json. "
            "Get your token at https://app.todoist.com/app/settings/integrations/developer"
        )
    return token


def _get_client() -> httpx.Client:
    """Lazy singleton httpx.Client with bearer token from config."""
    global _client
    if _client is None:
        token = _get_token()
        _client = httpx.Client(
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            timeout=TIMEOUT,
        )
    return _client


def _resolve_inbox_id() -> str:
    """Fetch projects, find is_inbox_project=True, cache for session."""
    global _inbox_id
    if _inbox_id is not None:
        return _inbox_id

    client = _get_client()
    resp = client.get(f"{REST_BASE}/projects")
    resp.raise_for_status()
    projects = resp.json()

    for project in projects:
        if project.get("is_inbox_project"):
            _inbox_id = project["id"]
            return _inbox_id

    raise ValueError("Could not find inbox project in Todoist")


def _handle_error(e: Exception) -> dict:
    """Convert exceptions to standardized error responses."""
    if isinstance(e, httpx.HTTPStatusError):
        status = e.response.status_code
        error_map = {
            401: "Unauthorized - check your API token",
            403: "Forbidden - insufficient permissions",
            404: "Not found",
            429: "Rate limited - too many requests, try again later",
        }
        msg = error_map.get(status, f"HTTP {status}: {e.response.text[:200]}")
        result = {"success": False, "error": msg, "status_code": status}
        if status == 429:
            retry_after = e.response.headers.get("Retry-After")
            if retry_after:
                result["retry_after_seconds"] = int(retry_after)
        return result
    elif isinstance(e, httpx.TimeoutException):
        return {"success": False, "error": "Request timed out"}
    elif isinstance(e, httpx.ConnectError):
        return {"success": False, "error": "Could not connect to Todoist API"}
    elif isinstance(e, httpx.RequestError):
        return {"success": False, "error": f"Request error: {str(e)}"}
    elif isinstance(e, ValueError):
        return {"success": False, "error": str(e)}
    else:
        return {"success": False, "error": f"Unexpected error: {str(e)}"}


def _resolve_project_id(project_id: str | None) -> str | None:
    """Resolve 'inbox' string to real inbox project ID."""
    if project_id and project_id.lower() == "inbox":
        return _resolve_inbox_id()
    return project_id


# ---------------------------------------------------------------------------
# Public API functions
# ---------------------------------------------------------------------------


def find_tasks(
    project_id: str | None = None,
    section_id: str | None = None,
    labels: list[str] | None = None,
    search_text: str | None = None,
    limit: int = 50,
) -> dict:
    """Find tasks by project, labels, or search text."""
    try:
        client = _get_client()
        resolved_project = _resolve_project_id(project_id)

        # Build filter string for the API
        filter_parts = []
        if resolved_project:
            filter_parts.append(f"#project_id:{resolved_project}")
        if section_id:
            filter_parts.append(f"/section_id:{section_id}")
        if labels:
            for label in labels:
                filter_parts.append(f"@{label}")

        # Use /tasks endpoint with optional filter
        params = {}
        if resolved_project and not labels and not search_text:
            # Simple project filter - use project_id param directly
            params["project_id"] = resolved_project
        elif section_id and not labels and not search_text:
            params["section_id"] = section_id
        elif labels and not resolved_project and not search_text:
            # Label filter - use label param
            params["label"] = labels[0] if len(labels) == 1 else labels[0]

        resp = client.get(f"{REST_BASE}/tasks", params=params)
        resp.raise_for_status()
        tasks = resp.json()

        # Client-side label filtering (API label param is single-label only)
        if labels:
            label_set = set(labels)
            tasks = [t for t in tasks if label_set.issubset(set(t.get("labels", [])))]

        if search_text:
            search_lower = search_text.lower()
            tasks = [
                t for t in tasks
                if search_lower in t.get("content", "").lower()
                or search_lower in t.get("description", "").lower()
            ]

        # Apply limit
        tasks = tasks[:limit]

        return {"success": True, "tasks": tasks, "count": len(tasks)}

    except Exception as e:
        return _handle_error(e)


def find_tasks_by_date(
    start_date: str = "today",
    days_count: int = 1,
    overdue_option: str = "include-overdue",
    labels: list[str] | None = None,
    limit: int = 50,
) -> dict:
    """Find tasks by date range with overdue handling."""
    try:
        client = _get_client()

        # Resolve start date
        if start_date == "today":
            start = date.today()
        else:
            start = date.fromisoformat(start_date)

        end = start + timedelta(days=days_count - 1)

        # Build filter string
        if overdue_option == "overdue-only":
            filter_str = "overdue"
        elif overdue_option == "include-overdue":
            if days_count == 1 and start == date.today():
                filter_str = "overdue | today"
            else:
                filter_str = f"overdue | due before: {end + timedelta(days=1)} & due after: {start - timedelta(days=1)}"
        else:  # exclude-overdue
            if days_count == 1:
                filter_str = f"due: {start}"
            else:
                filter_str = f"due before: {end + timedelta(days=1)} & due after: {start - timedelta(days=1)}"

        params = {"filter": filter_str}
        resp = client.get(f"{REST_BASE}/tasks", params=params)
        resp.raise_for_status()
        tasks = resp.json()

        # Client-side label filter
        if labels:
            label_set = set(labels)
            tasks = [t for t in tasks if label_set.issubset(set(t.get("labels", [])))]

        tasks = tasks[:limit]

        return {
            "success": True,
            "tasks": tasks,
            "count": len(tasks),
            "filter": filter_str,
        }

    except Exception as e:
        return _handle_error(e)


def add_tasks(tasks: list[dict]) -> dict:
    """Create one or more tasks. Each dict may have: content, description,
    dueString, priority, labels, projectId, sectionId, parentId, deadlineDate, duration."""
    try:
        client = _get_client()
        created = []
        errors = []

        for i, task in enumerate(tasks):
            try:
                body = {"content": task["content"]}

                # Map fields to Todoist REST API names
                field_map = {
                    "description": "description",
                    "dueString": "due_string",
                    "priority": "priority",
                    "labels": "labels",
                    "parentId": "parent_id",
                    "sectionId": "section_id",
                    "deadlineDate": "deadline_date",
                    "duration": "duration",
                    "order": "order",
                }
                for src, dst in field_map.items():
                    if src in task and task[src] is not None:
                        body[dst] = task[src]

                # Handle projectId with inbox resolution
                if "projectId" in task and task["projectId"]:
                    body["project_id"] = _resolve_project_id(task["projectId"])

                # Convert priority: p1=4, p2=3, p3=2, p4=1 (Todoist inverts)
                if "priority" in body and isinstance(body["priority"], str):
                    priority_map = {"p1": 4, "p2": 3, "p3": 2, "p4": 1}
                    body["priority"] = priority_map.get(body["priority"], 1)

                # Handle duration format (e.g., "2h", "90m", "2h30m")
                if "duration" in body and isinstance(body["duration"], str):
                    dur_str = body["duration"]
                    minutes = 0
                    if "h" in dur_str:
                        parts = dur_str.split("h")
                        hours_part = parts[0].strip()
                        if "." in hours_part:
                            minutes += int(float(hours_part) * 60)
                        else:
                            minutes += int(hours_part) * 60
                        if len(parts) > 1 and parts[1].strip().rstrip("m"):
                            minutes += int(parts[1].strip().rstrip("m"))
                    elif "m" in dur_str:
                        minutes += int(dur_str.rstrip("m"))
                    if minutes > 0:
                        body["duration"] = minutes
                        body["duration_unit"] = "minute"

                resp = client.post(f"{REST_BASE}/tasks", json=body)
                resp.raise_for_status()
                created.append(resp.json())

            except Exception as e:
                err = _handle_error(e)
                err["task_index"] = i
                err["task_content"] = task.get("content", "unknown")
                errors.append(err)

        result = {
            "success": len(errors) == 0,
            "created": created,
            "created_count": len(created),
        }
        if errors:
            result["errors"] = errors
            result["error_count"] = len(errors)
        return result

    except Exception as e:
        return _handle_error(e)


def complete_tasks(ids: list[str]) -> dict:
    """Complete (close) one or more tasks by ID."""
    try:
        client = _get_client()
        completed = []
        errors = []

        for task_id in ids:
            try:
                resp = client.post(f"{REST_BASE}/tasks/{task_id}/close")
                resp.raise_for_status()
                completed.append(task_id)
            except Exception as e:
                err = _handle_error(e)
                err["task_id"] = task_id
                errors.append(err)

        result = {
            "success": len(errors) == 0,
            "completed": completed,
            "completed_count": len(completed),
        }
        if errors:
            result["errors"] = errors
        return result

    except Exception as e:
        return _handle_error(e)


def update_tasks(tasks: list[dict]) -> dict:
    """Update one or more tasks. Each dict must have 'id' plus fields to update."""
    try:
        client = _get_client()
        updated = []
        errors = []

        for task in tasks:
            try:
                task_id = task["id"]
                body = {}

                field_map = {
                    "content": "content",
                    "description": "description",
                    "dueString": "due_string",
                    "priority": "priority",
                    "labels": "labels",
                    "parentId": "parent_id",
                    "sectionId": "section_id",
                    "deadlineDate": "deadline_date",
                    "duration": "duration",
                    "order": "order",
                }
                for src, dst in field_map.items():
                    if src in task:
                        body[dst] = task[src]

                if "projectId" in task:
                    body["project_id"] = _resolve_project_id(task["projectId"])

                # Convert priority string to int
                if "priority" in body and isinstance(body["priority"], str):
                    priority_map = {"p1": 4, "p2": 3, "p3": 2, "p4": 1}
                    body["priority"] = priority_map.get(body["priority"], 1)

                # Handle deadlineDate removal
                if body.get("deadline_date") == "remove":
                    body["deadline_date"] = None

                # Handle duration format
                if "duration" in body and isinstance(body["duration"], str):
                    dur_str = body["duration"]
                    minutes = 0
                    if "h" in dur_str:
                        parts = dur_str.split("h")
                        hours_part = parts[0].strip()
                        if "." in hours_part:
                            minutes += int(float(hours_part) * 60)
                        else:
                            minutes += int(hours_part) * 60
                        if len(parts) > 1 and parts[1].strip().rstrip("m"):
                            minutes += int(parts[1].strip().rstrip("m"))
                    elif "m" in dur_str:
                        minutes += int(dur_str.rstrip("m"))
                    if minutes > 0:
                        body["duration"] = minutes
                        body["duration_unit"] = "minute"

                resp = client.post(f"{REST_BASE}/tasks/{task_id}", json=body)
                resp.raise_for_status()
                updated.append(resp.json())

            except Exception as e:
                err = _handle_error(e)
                err["task_id"] = task.get("id", "unknown")
                errors.append(err)

        result = {
            "success": len(errors) == 0,
            "updated": updated,
            "updated_count": len(updated),
        }
        if errors:
            result["errors"] = errors
        return result

    except Exception as e:
        return _handle_error(e)


def delete_object(object_type: str, object_id: str) -> dict:
    """Delete a task, project, section, or comment."""
    try:
        client = _get_client()
        valid_types = {"task": "tasks", "project": "projects", "section": "sections", "comment": "comments"}
        endpoint = valid_types.get(object_type)
        if not endpoint:
            return {"success": False, "error": f"Invalid type '{object_type}'. Valid: {', '.join(valid_types.keys())}"}

        resp = client.delete(f"{REST_BASE}/{endpoint}/{object_id}")
        resp.raise_for_status()
        return {"success": True, "deleted_type": object_type, "deleted_id": object_id}

    except Exception as e:
        return _handle_error(e)


def user_info() -> dict:
    """Get user info via Todoist Sync API v9."""
    try:
        client = _get_client()
        resp = client.post(
            SYNC_URL,
            json={"sync_token": "*", "resource_types": ["user"]},
        )
        resp.raise_for_status()
        data = resp.json()
        user = data.get("user", {})

        return {
            "success": True,
            "user": {
                "id": user.get("id"),
                "full_name": user.get("full_name"),
                "email": user.get("email"),
                "tz_info": user.get("tz_info"),
                "start_day": user.get("start_day"),
                "premium_until": user.get("premium_until"),
            },
        }

    except Exception as e:
        return _handle_error(e)


def find_projects(search: str | None = None) -> dict:
    """List all projects, optionally filter by name."""
    try:
        client = _get_client()
        resp = client.get(f"{REST_BASE}/projects")
        resp.raise_for_status()
        projects = resp.json()

        if search:
            search_lower = search.lower()
            projects = [p for p in projects if search_lower in p.get("name", "").lower()]

        return {"success": True, "projects": projects, "count": len(projects)}

    except Exception as e:
        return _handle_error(e)


def add_projects(projects: list[dict]) -> dict:
    """Create one or more projects. Each dict may have: name, parentId, viewStyle, isFavorite."""
    try:
        client = _get_client()
        created = []
        errors = []

        for i, project in enumerate(projects):
            try:
                body = {"name": project["name"]}

                if "parentId" in project:
                    body["parent_id"] = project["parentId"]
                if "viewStyle" in project:
                    body["view_style"] = project["viewStyle"]
                if "isFavorite" in project:
                    body["is_favorite"] = project["isFavorite"]

                resp = client.post(f"{REST_BASE}/projects", json=body)
                resp.raise_for_status()
                created.append(resp.json())

            except Exception as e:
                err = _handle_error(e)
                err["project_index"] = i
                err["project_name"] = project.get("name", "unknown")
                errors.append(err)

        result = {
            "success": len(errors) == 0,
            "created": created,
            "created_count": len(created),
        }
        if errors:
            result["errors"] = errors
        return result

    except Exception as e:
        return _handle_error(e)


def reset_client():
    """Reset the client and cached state. Used for testing."""
    global _client, _inbox_id
    _client = None
    _inbox_id = None
