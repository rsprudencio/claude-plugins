#!/usr/bin/env python3
"""Verify contracts that may break when Docker dependency versions drift.

Run inside the built production image.  This does not call Todoist or any
external service; it checks that the resolved SDK still accepts every call
shape Jarvis uses and that every shipped ASGI application imports.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import os
import subprocess
import sys
from importlib.metadata import version


TODOIST_CALLS = {
    "get_projects": [()],
    "get_tasks": [
        ((), {"project_id": "project"}),
        ((), {"section_id": "section"}),
        ((), {"label": "label"}),
    ],
    "filter_tasks": [((), {"query": "today"})],
    "add_task": [
        (
            ("content",),
            {
                "description": "description",
                "project_id": "project",
                "section_id": "section",
                "parent_id": "parent",
                "labels": ["label"],
                "priority": 1,
                "due_string": "today",
                "order": 1,
                "duration": 30,
                "duration_unit": "minute",
                "deadline_date": None,
            },
        )
    ],
    "complete_task": [("task",)],
    "update_task": [
        (
            ("task",),
            {
                "content": "content",
                "description": "description",
                "labels": ["label"],
                "priority": 1,
                "due_string": "today",
                "order": 1,
                "duration": 30,
                "duration_unit": "minute",
                "deadline_date": None,
            },
        )
    ],
    "delete_task": [("task",)],
    "delete_project": [("project",)],
    "delete_section": [("section",)],
    "delete_comment": [("comment",)],
}


def _normalize_calls(calls: list) -> list[tuple[tuple, dict]]:
    normalized = []
    for call in calls:
        if len(call) == 2 and isinstance(call[0], tuple) and isinstance(call[1], dict):
            normalized.append(call)
        else:
            normalized.append((tuple(call), {}))
    return normalized


def verify_todoist_surface() -> None:
    from todoist_api_python.api import TodoistAPI

    for method_name, calls in TODOIST_CALLS.items():
        method = getattr(TodoistAPI, method_name, None)
        assert callable(method), f"TodoistAPI.{method_name} is missing"
        signature = inspect.signature(method)
        for args, kwargs in _normalize_calls(calls):
            try:
                signature.bind(object(), *args, **kwargs)
            except TypeError as exc:
                raise AssertionError(
                    f"TodoistAPI.{method_name}{signature} no longer accepts "
                    f"Jarvis call args={args!r}, kwargs={kwargs!r}: {exc}"
                ) from exc


def verify_app_imports() -> None:
    checks = [
        ("/app/jarvis-core", "import http_app; assert callable(http_app.app)"),
        ("/app/jarvis-todoist", "import http_app; assert callable(http_app.app)"),
        ("/app/jarvis-obsidian", "import http_app; assert callable(http_app.app)"),
        ("/app/memory-explorer", "import app; assert callable(app.app)"),
    ]
    for cwd, expression in checks:
        subprocess.run([sys.executable, "-c", expression], cwd=cwd, check=True)


def verify_host_only_image() -> None:
    forbidden_modules = [
        "onnxruntime",
        "sentence_transformers",
        "tokenizers",
        "torch",
        "transformers",
    ]
    present = [name for name in forbidden_modules if importlib.util.find_spec(name)]
    assert not present, f"in-container inference packages found: {present}"
    assert not os.path.exists("/app/models"), "in-container model directory found"


def verify_llm_backend_importable() -> None:
    """`anthropic` must be present so an ANTHROPIC_API_KEY is actually usable.

    The image previously shipped without it, so a key in the container produced
    one failed `import anthropic` per file — hundreds of warnings and zero
    summaries. bin/generate_summaries.py in-container depends on this.
    """
    assert importlib.util.find_spec("anthropic"), (
        "anthropic SDK missing: ANTHROPIC_API_KEY would be unusable and "
        "bin/generate_summaries.py could not run in-container"
    )


def main() -> int:
    import requests  # Direct Jarvis dependency; must be explicit in the image.

    verify_todoist_surface()
    verify_app_imports()
    verify_host_only_image()
    verify_llm_backend_importable()
    print(
        json.dumps(
            {
                "status": "ok",
                "todoist_api_python": version("todoist-api-python"),
                "requests": requests.__version__,
                "todoist_methods": sorted(TODOIST_CALLS),
                "apps": ["core", "todoist", "obsidian", "memory-explorer"],
                "inference": "host-only",
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
