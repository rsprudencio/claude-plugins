"""Tests for worklog-related helpers in extract_observation.py."""

import os
import sys

# Add hooks-handlers to path for importing
HOOKS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "hooks-handlers")
sys.path.insert(0, HOOKS_DIR)

import extract_observation
from extract_observation import (
    _DEDUP_JACCARD_THRESHOLD,
    _DEDUP_RELEVANCE_THRESHOLD,
    _WORKLOG_ACTIVITY_TYPES,
    _has_jaccard_duplicate,
    discover_workstreams,
    is_duplicate_observation,
    is_duplicate_worklog,
    jaccard_similarity,
    normalize_worklog_response,
    store_worklog,
)


def test_normalize_worklog_response_defaults_and_validation():
    """normalize_worklog_response strips and defaults invalid fields."""
    parsed = {
        "worklog": {
            "task_summary": "  Refactor stop hook transport  ",
            "workstream": "",
            "activity_type": "invalid",
            "tags": "not-a-list",
        }
    }
    result = normalize_worklog_response(parsed)
    assert len(result) == 1
    assert result[0]["task_summary"] == "Refactor stop hook transport"
    assert result[0]["workstream"] == "misc"
    assert result[0]["activity_type"] == "other"
    assert result[0]["tags"] == []


def test_jaccard_similarity_basics():
    """Jaccard helper handles identical, disjoint, and partial overlap."""
    assert jaccard_similarity("hello world", "hello world") == 1.0
    assert jaccard_similarity("hello world", "foo bar") == 0.0
    assert jaccard_similarity("adding docker support", "adding worklog support") == 0.5


def test_has_jaccard_duplicate_thresholds():
    """_has_jaccard_duplicate respects threshold."""
    candidates = ["adding worklog feature to plugin"]
    assert _has_jaccard_duplicate("adding worklog feature", candidates, threshold=0.5)
    assert not _has_jaccard_duplicate("adding worklog feature", candidates, threshold=0.95)


def test_discover_workstreams_uses_hook_context_endpoint(monkeypatch):
    """discover_workstreams now loads from /hook/auto-extract/context."""
    seen_payload = {}

    def _fake_post_json(endpoint_path, payload, timeout_seconds=2.5, mcp_json_path=None):
        seen_payload.update(payload)
        assert endpoint_path == "/hook/auto-extract/context"
        return {
            "success": True,
            "data": {
                "known_workstreams": ["Jarvis Plugin", "VMPulse"],
            },
        }

    monkeypatch.setattr(extract_observation, "post_json", _fake_post_json)
    result = discover_workstreams(limit=17)

    assert seen_payload["workstream_limit"] == 17
    assert result == ["Jarvis Plugin", "VMPulse"]


def test_store_worklog_posts_ingest_payload(monkeypatch):
    """store_worklog sends payload to ingest endpoint and surfaces stored id."""
    captured = {}

    def _fake_post_json(endpoint_path, payload, timeout_seconds=2.5, mcp_json_path=None):
        captured["endpoint"] = endpoint_path
        captured["payload"] = payload
        return {
            "success": True,
            "data": {
                "success": True,
                "observations": [],
                "worklog": {"status": "stored", "id": "worklog::123", "error": ""},
            },
        }

    monkeypatch.setattr(extract_observation, "post_json", _fake_post_json)

    result = store_worklog(
        task_summary="Refactor hook persistence",
        workstream="Jarvis Plugin",
        activity_type="coding",
        tags=["hooks"],
        source_label="auto-extract:stop-hook:worklog",
        project_path="/tmp/project",
        git_branch="main",
        relevant_files=["a.py"],
        session_id="session-1",
        transcript_line=42,
    )

    assert result["success"] is True
    assert result["id"] == "worklog::123"
    assert captured["endpoint"] == "/hook/auto-extract/ingest"
    assert captured["payload"]["worklog"]["task_summary"] == "Refactor hook persistence"
    assert captured["payload"]["context"]["session_id"] == "session-1"


def test_store_worklog_failure_passthrough(monkeypatch):
    """Transport failures bubble up as unsuccessful store result."""
    monkeypatch.setattr(
        extract_observation,
        "post_json",
        lambda *args, **kwargs: {"success": False, "error": "connection refused", "data": None},
    )

    result = store_worklog(
        task_summary="Refactor hook persistence",
        workstream="Jarvis Plugin",
        activity_type="coding",
        tags=[],
        source_label="auto-extract:stop-hook:worklog",
    )
    assert result["success"] is False
    assert "connection refused" in result["error"]


def test_legacy_duplicate_helpers_are_safe_defaults():
    """Deprecated local dedup helpers now return False (server-side dedup)."""
    assert is_duplicate_observation("anything") is False
    assert is_duplicate_worklog("anything", "session-1") is False


def test_worklog_constants():
    """Core constants remain unchanged."""
    assert _DEDUP_JACCARD_THRESHOLD == 0.5
    assert _DEDUP_RELEVANCE_THRESHOLD == 0.95
    assert _WORKLOG_ACTIVITY_TYPES == {
        "coding",
        "debugging",
        "reviewing",
        "configuring",
        "planning",
        "discussing",
        "researching",
        "other",
    }
