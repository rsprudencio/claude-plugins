"""
Todoist API client for Jarvis.

Uses the official todoist-api-python SDK for API access.
Reads API token from ~/.jarvis/config.json → todoist.api_token.
"""
import json
import logging
import os
from datetime import date, timedelta

import requests

logger = logging.getLogger("jarvis-todoist-api")

_api = None
_inbox_id: str | None = None


def _get_token() -> str:
    """Read API token from env var or config.

    Resolution order:
    1. TODOIST_API_TOKEN env var (for Docker)
    2. ~/.jarvis/config.json (or $JARVIS_HOME/config.json) → todoist.api_token
    """
    # Check env var first (Docker mode)
    env_token = os.environ.get("TODOIST_API_TOKEN")
    if env_token:
        return env_token

    # Fall back to config file
    jarvis_home = os.environ.get("JARVIS_HOME", os.path.expanduser("~/.jarvis"))
    config_path = os.path.join(jarvis_home, "config.json")
    if not os.path.exists(config_path):
        raise ValueError(
            f"Jarvis config not found at {config_path}. "
            "Run /jarvis-settings to configure."
        )
    with open(config_path) as f:
        config = json.load(f)
    token = config.get("todoist", {}).get("api_token", "")
    if not token:
        raise ValueError(
            "No Todoist API token configured. "
            "Set TODOIST_API_TOKEN env var or add todoist.api_token to config. "
            "Get your token at https://app.todoist.com/app/settings/integrations/developer"
        )
    return token


def _get_api():
    """Lazy singleton TodoistAPI client."""
    global _api
    if _api is None:
        from todoist_api_python.api import TodoistAPI
        token = _get_token()
        _api = TodoistAPI(token)
    return _api


def _resolve_inbox_id() -> str:
    """Fetch projects, find is_inbox_project=True, cache for session."""
    global _inbox_id
    if _inbox_id is not None:
        return _inbox_id

    api = _get_api()
    for page in api.get_projects():
        for project in page:
            if project.is_inbox_project:
                _inbox_id = project.id
                return _inbox_id

    raise ValueError("Could not find inbox project in Todoist")


def _handle_error(e: Exception) -> dict:
    """Convert exceptions to standardized error responses."""
    error_str = str(e)
    if isinstance(e, ValueError):
        return {"success": False, "error": error_str}

    # The SDK raises Exception with HTTP status info
    if "401" in error_str:
        return {"success": False, "error": "Unauthorized - check your API token", "status_code": 401}
    elif "403" in error_str:
        return {"success": False, "error": "Forbidden - insufficient permissions", "status_code": 403}
    elif "404" in error_str:
        return {"success": False, "error": "Not found", "status_code": 404}
    elif "429" in error_str:
        return {"success": False, "error": "Rate limited - too many requests, try again later", "status_code": 429}
    elif "410" in error_str:
        return {"success": False, "error": "API endpoint deprecated - update todoist-api-python", "status_code": 410}

    return {"success": False, "error": f"Todoist API error: {error_str}"}


def _resolve_project_id(project_id: str | None) -> str | None:
    """Resolve 'inbox' string to real inbox project ID."""
    if project_id and project_id.lower() == "inbox":
        return _resolve_inbox_id()
    return project_id


def _task_to_dict(task) -> dict:
    """Convert a Task object to a JSON-serializable dict."""
    result = {
        "id": task.id,
        "content": task.content,
        "description": task.description,
        "labels": task.labels,
        "priority": task.priority,
        "order": task.order,
        "project_id": task.project_id,
        "section_id": task.section_id,
        "parent_id": task.parent_id,
        "creator_id": task.creator_id,
        "is_completed": task.is_completed,
        "url": task.url,
    }
    if task.due:
        result["due"] = {
            "date": str(task.due.date),
            "string": task.due.string,
            "is_recurring": task.due.is_recurring,
            "timezone": task.due.timezone,
        }
    else:
        result["due"] = None
    if task.duration:
        result["duration"] = {
            "amount": task.duration.amount,
            "unit": task.duration.unit,
        }
    else:
        result["duration"] = None
    return result


def _project_to_dict(project) -> dict:
    """Convert a Project object to a JSON-serializable dict."""
    return {
        "id": project.id,
        "name": project.name,
        "color": project.color,
        "is_inbox_project": project.is_inbox_project,
        "is_favorite": project.is_favorite,
        "view_style": project.view_style,
        "parent_id": project.parent_id,
        "order": project.order,
        "url": project.url,
    }


def _parse_duration(dur_str: str) -> tuple[int, str] | None:
    """Parse duration string (e.g., '2h', '90m', '2h30m') to (amount, unit)."""
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
    return (minutes, "minute") if minutes > 0 else None


PRIORITY_MAP = {"p1": 4, "p2": 3, "p3": 2, "p4": 1}


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
        api = _get_api()
        resolved_project = _resolve_project_id(project_id)

        # Build kwargs for get_tasks
        kwargs = {}
        if resolved_project:
            kwargs["project_id"] = resolved_project
        if section_id:
            kwargs["section_id"] = section_id
        if labels and len(labels) == 1:
            kwargs["label"] = labels[0]

        tasks = []
        for page in api.get_tasks(**kwargs):
            tasks.extend(page)
            if len(tasks) >= limit * 2:  # fetch enough for client-side filtering
                break

        # Client-side label filtering (API supports single label only)
        if labels:
            label_set = set(labels)
            tasks = [t for t in tasks if label_set.issubset(set(t.labels))]

        if search_text:
            search_lower = search_text.lower()
            tasks = [
                t for t in tasks
                if search_lower in t.content.lower()
                or search_lower in (t.description or "").lower()
            ]

        tasks = tasks[:limit]
        return {"success": True, "tasks": [_task_to_dict(t) for t in tasks], "count": len(tasks)}

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
        api = _get_api()

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

        tasks = []
        for page in api.filter_tasks(query=filter_str):
            tasks.extend(page)

        # Client-side label filter
        if labels:
            label_set = set(labels)
            tasks = [t for t in tasks if label_set.issubset(set(t.labels))]

        tasks = tasks[:limit]

        return {
            "success": True,
            "tasks": [_task_to_dict(t) for t in tasks],
            "count": len(tasks),
            "filter": filter_str,
        }

    except Exception as e:
        return _handle_error(e)


def add_tasks(tasks: list[dict]) -> dict:
    """Create one or more tasks. Each dict may have: content, description,
    dueString, priority, labels, projectId, sectionId, parentId, deadlineDate, duration."""
    try:
        api = _get_api()
        created = []
        errors = []

        for i, task in enumerate(tasks):
            try:
                kwargs = {"content": task["content"]}

                if task.get("description"):
                    kwargs["description"] = task["description"]
                if task.get("dueString"):
                    kwargs["due_string"] = task["dueString"]
                if task.get("labels"):
                    kwargs["labels"] = task["labels"]
                if task.get("parentId"):
                    kwargs["parent_id"] = task["parentId"]
                if task.get("sectionId"):
                    kwargs["section_id"] = task["sectionId"]
                if task.get("order") is not None:
                    kwargs["order"] = task["order"]

                # Handle projectId with inbox resolution
                if task.get("projectId"):
                    kwargs["project_id"] = _resolve_project_id(task["projectId"])

                # Convert priority: p1=4, p2=3, p3=2, p4=1
                if task.get("priority"):
                    p = task["priority"]
                    kwargs["priority"] = PRIORITY_MAP.get(p, p) if isinstance(p, str) else p

                # Handle deadlineDate
                if task.get("deadlineDate"):
                    kwargs["deadline_date"] = date.fromisoformat(task["deadlineDate"])

                # Handle duration format (e.g., "2h", "90m", "2h30m")
                if task.get("duration"):
                    if isinstance(task["duration"], str):
                        parsed = _parse_duration(task["duration"])
                        if parsed:
                            kwargs["duration"] = parsed[0]
                            kwargs["duration_unit"] = parsed[1]
                    else:
                        kwargs["duration"] = task["duration"]
                        kwargs["duration_unit"] = "minute"

                result = api.add_task(**kwargs)
                created.append(_task_to_dict(result))

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
        api = _get_api()
        completed = []
        errors = []

        for task_id in ids:
            try:
                api.complete_task(task_id)
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
        api = _get_api()
        updated = []
        errors = []

        for task in tasks:
            try:
                task_id = task["id"]
                kwargs = {}

                if "content" in task:
                    kwargs["content"] = task["content"]
                if "description" in task:
                    kwargs["description"] = task["description"]
                if "dueString" in task:
                    kwargs["due_string"] = task["dueString"]
                if "labels" in task:
                    kwargs["labels"] = task["labels"]
                if "order" in task:
                    kwargs["order"] = task["order"]

                # Convert priority string to int
                if "priority" in task:
                    p = task["priority"]
                    kwargs["priority"] = PRIORITY_MAP.get(p, p) if isinstance(p, str) else p

                # Handle deadlineDate removal
                if "deadlineDate" in task:
                    dd = task["deadlineDate"]
                    if dd == "remove":
                        kwargs["deadline_date"] = None
                    else:
                        kwargs["deadline_date"] = date.fromisoformat(dd)

                # Handle duration
                if "duration" in task:
                    if isinstance(task["duration"], str):
                        parsed = _parse_duration(task["duration"])
                        if parsed:
                            kwargs["duration"] = parsed[0]
                            kwargs["duration_unit"] = parsed[1]
                    else:
                        kwargs["duration"] = task["duration"]
                        kwargs["duration_unit"] = "minute"

                result = api.update_task(task_id, **kwargs)
                updated.append(_task_to_dict(result))

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
        api = _get_api()
        valid_types = {"task", "project", "section", "comment"}
        if object_type not in valid_types:
            return {"success": False, "error": f"Invalid type '{object_type}'. Valid: {', '.join(sorted(valid_types))}"}

        delete_map = {
            "task": api.delete_task,
            "project": api.delete_project,
            "section": api.delete_section,
            "comment": api.delete_comment,
        }
        delete_map[object_type](object_id)
        return {"success": True, "deleted_type": object_type, "deleted_id": object_id}

    except Exception as e:
        return _handle_error(e)


def user_info() -> dict:
    """Get user info via Todoist API.

    The SDK doesn't expose a user_info method, so we call the
    /api/v1/user endpoint directly using requests.
    """
    try:
        token = _get_token()
        response = requests.get(
            "https://api.todoist.com/api/v1/user",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        response.raise_for_status()
        user = response.json()

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
        api = _get_api()
        projects = []
        for page in api.get_projects():
            projects.extend(page)

        if search:
            search_lower = search.lower()
            projects = [p for p in projects if search_lower in p.name.lower()]

        return {
            "success": True,
            "projects": [_project_to_dict(p) for p in projects],
            "count": len(projects),
        }

    except Exception as e:
        return _handle_error(e)


def add_projects(projects: list[dict]) -> dict:
    """Create one or more projects. Each dict may have: name, parentId, viewStyle, isFavorite."""
    try:
        api = _get_api()
        created = []
        errors = []

        for i, project in enumerate(projects):
            try:
                kwargs = {"name": project["name"]}

                if "parentId" in project:
                    kwargs["parent_id"] = project["parentId"]
                if "viewStyle" in project:
                    kwargs["view_style"] = project["viewStyle"]
                if "isFavorite" in project:
                    kwargs["is_favorite"] = project["isFavorite"]

                result = api.add_project(**kwargs)
                created.append(_project_to_dict(result))

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
    global _api, _inbox_id
    _api = None
    _inbox_id = None
