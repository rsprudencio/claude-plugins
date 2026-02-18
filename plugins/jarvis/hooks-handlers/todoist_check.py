"""Todoist cache reader for the UserPromptSubmit hook.

Reads the local ``todoist_cache.json`` written by the background sync loop
and formats alerts as XML for injection into Claude's context.

Stdlib only — no MCP server imports.  Designed to be fast (~3ms) and
never fail (returns empty string on any error).
"""

import json
import time
import xml.sax.saxutils as saxutils
from pathlib import Path

# Cache staleness: if synced_at is older than 2x the default interval (20 min),
# treat cache as stale and skip injection.
_STALE_THRESHOLD_SECONDS = 1200

# Priority mapping: Todoist uses 1=highest, 4=lowest (normal)
_PRIORITY_MAP = {1: "p1", 2: "p2", 3: "p3", 4: "p4"}


def get_todoist_alerts(cache_path: str, max_per_category: int = 3) -> str:
    """Read Todoist cache and return XML string for context injection.

    Args:
        cache_path: Absolute path to ``todoist_cache.json``.
        max_per_category: Maximum tasks to show per alert category.

    Returns:
        XML string like ``<todoist-alerts>...</todoist-alerts>``, or empty
        string if cache is missing, stale, corrupt, or has no alerts.
    """
    try:
        path = Path(cache_path)
        if not path.is_file():
            return ""

        with open(path) as f:
            cache = json.load(f)

        # Check staleness
        synced_at = cache.get("synced_at", "")
        if synced_at and _is_stale(synced_at):
            return ""

        alerts = cache.get("alerts", {})
        if not alerts:
            return ""

        return _format_alerts(alerts, synced_at, max_per_category)

    except Exception:
        return ""


def _is_stale(synced_at: str) -> bool:
    """Check if the cache is too old to use."""
    try:
        from datetime import datetime, timezone

        synced = datetime.strptime(synced_at, "%Y-%m-%dT%H:%M:%S").replace(
            tzinfo=timezone.utc
        )
        age_seconds = (datetime.now(timezone.utc) - synced).total_seconds()
        return age_seconds > _STALE_THRESHOLD_SECONDS
    except Exception:
        return False  # If we can't parse, assume it's fine


def _format_alerts(alerts: dict, synced_at: str, max_per_category: int) -> str:
    """Format classified alerts as XML."""
    # Category display order and labels
    categories = [
        ("overdue", "overdue"),
        ("due_today", "due_today"),
        ("inbox_unprocessed", "inbox_unprocessed"),
        ("scheduled_actions", "scheduled_actions"),
    ]

    parts = []
    for key, label in categories:
        tasks = alerts.get(key, [])
        if not tasks:
            continue

        total_count = len(tasks)
        display_tasks = tasks[:max_per_category]
        task_lines = []
        for t in display_tasks:
            attrs = [f'id="{saxutils.escape(str(t.get("id", "")))}"']
            priority = t.get("priority", 4)
            if priority < 4:  # Only show non-default priority
                attrs.append(f'priority="{_PRIORITY_MAP.get(priority, "p4")}"')
            content = saxutils.escape(t.get("content", ""))
            due = t.get("due", "")
            if due:
                content += f" (due {saxutils.escape(due)})"
            task_lines.append(f'  <task {" ".join(attrs)}>{content}</task>')

        header = f'<alert type="{label}" count="{total_count}">'
        parts.append(header)
        parts.extend(task_lines)
        parts.append("</alert>")

    if not parts:
        return ""

    header = f'<todoist-alerts synced="{saxutils.escape(synced_at)}">'
    return "\n".join([header] + parts + ["</todoist-alerts>"])
