"""Background Todoist sync: periodically fetches tasks and writes a local JSON cache.

The sync loop runs as a background asyncio task alongside the MCP server (same
pattern as ``pattern_detection_loop`` and ``health_probe_loop``).  All HTTP
calls use stdlib ``urllib.request`` to avoid adding dependencies.  Blocking I/O
is offloaded via ``asyncio.to_thread``.

The cache file lives at ``~/.jarvis/state/todoist_cache.json`` (in Docker:
``/config/state/todoist_cache.json``).  The ``UserPromptSubmit`` hook reads this
file — guaranteed fast (~3 ms, local file read, no network).
"""

import asyncio
import json
import logging
import os
import tempfile
import urllib.request
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

logger = logging.getLogger("jarvis-core")

_API_BASE = "https://api.todoist.com/api/v1"

# ── Token resolution ─────────────────────────────────────────────────────────


def _get_todoist_token() -> str:
    """Resolve Todoist API token.

    Resolution order (mirrors jarvis-todoist plugin):
    1. ``TODOIST_API_TOKEN`` env var (Docker)
    2. ``~/.jarvis/config.json`` → ``todoist.api_token``

    Returns empty string if no token is available (caller decides how to handle).
    """
    env_token = os.environ.get("TODOIST_API_TOKEN", "").strip()
    if env_token:
        return env_token

    try:
        from .config import get_config

        config = get_config()
        return config.get("todoist", {}).get("api_token", "").strip()
    except Exception:
        return ""


# ── HTTP helpers (stdlib only) ───────────────────────────────────────────────


def _fetch_json(url: str, token: str, timeout: int = 5) -> list | dict:
    """GET a Todoist REST endpoint, return parsed JSON."""
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_tasks(token: str, timeout: int = 5) -> list:
    """Fetch all active tasks from Todoist.

    The v1 API wraps results in ``{"results": [...]}``.
    """
    data = _fetch_json(f"{_API_BASE}/tasks", token, timeout)
    if isinstance(data, dict):
        return data.get("results", [])
    return data  # Fallback for unexpected format


def _fetch_projects(token: str, timeout: int = 5) -> list:
    """Fetch all projects from Todoist.

    The v1 API wraps results in ``{"results": [...]}``.
    """
    data = _fetch_json(f"{_API_BASE}/projects", token, timeout)
    if isinstance(data, dict):
        return data.get("results", [])
    return data  # Fallback for unexpected format


# ── Classification ───────────────────────────────────────────────────────────


def _find_inbox_project_id(projects: list) -> str:
    """Find the inbox project ID from a list of projects.

    The v1 API uses ``inbox_project: true`` (v2 used ``is_inbox_project``).
    Checks both for backward compatibility.
    """
    for p in projects:
        if p.get("inbox_project") or p.get("is_inbox_project"):
            return str(p.get("id", ""))
    return ""


def _parse_due_date(task: dict) -> date | None:
    """Extract the due date from a task's ``due`` field.

    Handles both date-only ("2026-02-18") and datetime
    ("2026-02-18T14:30:00Z" / "2026-02-18T14:30:00") formats.
    """
    due = task.get("due")
    if not due:
        return None
    # Prefer date string (always present when due exists)
    date_str = due.get("date", "")
    if not date_str:
        return None
    try:
        # date_str is either "YYYY-MM-DD" or "YYYY-MM-DDTHH:MM:SS..."
        return date.fromisoformat(date_str[:10])
    except (ValueError, TypeError):
        return None


def _is_timed_overdue(task: dict) -> bool:
    """Check if a task with a datetime due is overdue (past the exact time)."""
    due = task.get("due")
    if not due:
        return False
    dt_str = due.get("datetime")
    if not dt_str:
        return False
    try:
        # Parse ISO datetime, normalize to UTC
        due_dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        return due_dt < datetime.now(timezone.utc)
    except (ValueError, TypeError):
        return False


def _task_summary(task: dict) -> dict:
    """Extract a compact summary of a task for the cache."""
    due = task.get("due")
    summary = {
        "id": str(task.get("id", "")),
        "content": task.get("content", ""),
        "priority": task.get("priority", 4),
    }
    if due:
        summary["due"] = due.get("date", "")
        if due.get("datetime"):
            summary["due_datetime"] = due["datetime"]
    return summary


def classify_tasks(tasks: list, inbox_project_id: str) -> dict:
    """Split tasks into 4 alert buckets.

    Categories:
    - ``overdue``: due date < today, or datetime < now for timed tasks
    - ``due_today``: due date == today (excluding already-overdue timed tasks)
    - ``inbox_unprocessed``: in inbox project AND has no labels (raw captures)
    - ``scheduled_actions``: has the ``jarvis-scheduled`` label

    A task can appear in multiple categories (e.g., overdue AND scheduled).
    """
    today = date.today()
    overdue = []
    due_today = []
    inbox_unprocessed = []
    scheduled_actions = []

    for task in tasks:
        labels = task.get("labels", [])
        due_date = _parse_due_date(task)

        # Scheduled actions (label-based, independent of due status)
        if "jarvis-scheduled" in labels:
            scheduled_actions.append(_task_summary(task))

        # Inbox unprocessed: in inbox project, no labels
        if (
            inbox_project_id
            and str(task.get("project_id", "")) == inbox_project_id
            and not labels
        ):
            inbox_unprocessed.append(_task_summary(task))

        # Due classification
        if due_date is not None:
            if due_date < today:
                overdue.append(_task_summary(task))
            elif due_date == today:
                # For timed tasks, check if the exact time has passed
                if _is_timed_overdue(task):
                    overdue.append(_task_summary(task))
                else:
                    due_today.append(_task_summary(task))

    return {
        "overdue": overdue,
        "due_today": due_today,
        "inbox_unprocessed": inbox_unprocessed,
        "scheduled_actions": scheduled_actions,
    }


# ── Cache I/O ────────────────────────────────────────────────────────────────


def _get_cache_path() -> Path:
    """Resolve the cache file path."""
    jarvis_home = os.environ.get("JARVIS_HOME", str(Path.home() / ".jarvis"))
    return Path(jarvis_home) / "state" / "todoist_cache.json"


def _write_cache(cache_path: Path, classified: dict, inbox_project_id: str):
    """Atomically write the cache file (tempfile + os.replace)."""
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    counts = {k: len(v) for k, v in classified.items()}
    payload = {
        "synced_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S"),
        "inbox_project_id": inbox_project_id,
        "alerts": classified,
        "counts": counts,
    }
    # Atomic write: write to temp file in same directory, then replace
    fd, tmp_path = tempfile.mkstemp(
        dir=str(cache_path.parent), suffix=".tmp", prefix="todoist_cache_"
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(payload, f, indent=2)
        os.replace(tmp_path, str(cache_path))
    except Exception:
        # Clean up temp file on failure
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


# ── Sync pipeline ────────────────────────────────────────────────────────────


def sync_once(config: dict) -> dict:
    """Run a single sync iteration: fetch → classify → cache.

    Args:
        config: Dict with keys from ``get_todoist_prompt_alerts_config()``.

    Returns:
        Summary dict with counts and status.
    """
    token = _get_todoist_token()
    if not token:
        return {"success": False, "reason": "no_token"}

    timeout = config.get("api_timeout_seconds", 5)

    tasks = _fetch_tasks(token, timeout)
    projects = _fetch_projects(token, timeout)
    inbox_id = _find_inbox_project_id(projects)

    classified = classify_tasks(tasks, inbox_id)
    cache_path = _get_cache_path()
    _write_cache(cache_path, classified, inbox_id)

    counts = {k: len(v) for k, v in classified.items()}
    total = sum(counts.values())
    return {"success": True, "counts": counts, "total_alerts": total}


# ── Background loop ─────────────────────────────────────────────────────────

_STARTUP_DELAY = 60  # seconds — wait for server to settle


async def todoist_sync_loop():
    """Background loop that periodically syncs Todoist tasks to local cache.

    Runs alongside the MCP server via ``asyncio.gather()``.  Each sync is
    offloaded to a thread since HTTP calls are blocking.

    If no token is configured, logs once and sleeps forever (no log spam).
    If the API fails, keeps the old cache and retries next interval.
    """
    from .config import get_todoist_prompt_alerts_config

    await asyncio.sleep(_STARTUP_DELAY)

    # Check if feature is enabled and token exists
    config = get_todoist_prompt_alerts_config()
    if not config.get("enabled", False):
        logger.debug("Todoist prompt alerts disabled, sync loop exiting")
        return

    token = _get_todoist_token()
    if not token:
        logger.info("Todoist prompt alerts enabled but no API token configured, sync loop exiting")
        return

    logger.info("Todoist sync loop started (interval=%ds)", config.get("sync_interval_seconds", 900))

    while True:
        try:
            config = get_todoist_prompt_alerts_config()
            if not config.get("enabled", False):
                logger.debug("Todoist prompt alerts disabled mid-run, stopping")
                return

            result = await asyncio.to_thread(sync_once, config)
            if result.get("success"):
                total = result.get("total_alerts", 0)
                if total > 0:
                    logger.info("Todoist sync: %s", result.get("counts"))
                else:
                    logger.debug("Todoist sync: no alerts")
            else:
                logger.debug("Todoist sync skipped: %s", result.get("reason"))

        except Exception:
            logger.exception("Error in Todoist sync loop (will retry)")

        config = get_todoist_prompt_alerts_config()
        await asyncio.sleep(config.get("sync_interval_seconds", 900))
