"""Tests for extract_observation.py with endpoint-backed persistence."""

import json
import os
import sys
from pathlib import Path

import pytest

# Add hooks-handlers to path for importing
HOOKS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "hooks-handlers")
sys.path.insert(0, HOOKS_DIR)

import extract_observation
from extract_observation import (
    build_ingest_event_id,
    enqueue_ingest_payload,
    normalize_extraction_response,
    normalize_worklog_response,
    parse_all_turns,
    read_watermark,
    replay_ingest_queue,
    write_watermark,
)
from turn_state import capture_user_prompt


def _make_transcript(tmp_path: Path, user_text: str, assistant_text: str) -> str:
    """Create a minimal transcript JSONL with one complete turn."""
    path = tmp_path / "transcript.jsonl"
    lines = [
        json.dumps(
            {
                "type": "user",
                "message": {"content": [{"type": "text", "text": user_text}]},
            }
        ),
        json.dumps(
            {
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": assistant_text}],
                    "usage": {"input_tokens": 200, "output_tokens": 120},
                },
            }
        ),
    ]
    path.write_text("\n".join(lines) + "\n")
    return str(path)


def _read_queue_lines(tmp_path: Path) -> list[str]:
    queue_file = tmp_path / ".jarvis" / "state" / "auto_extract_ingest_queue.jsonl"
    if not queue_file.exists():
        return []
    return [line for line in queue_file.read_text().splitlines() if line.strip()]


def test_watermark_roundtrip(tmp_path, monkeypatch):
    """Watermark writes and reads session offsets correctly."""
    monkeypatch.setattr(extract_observation, "WATERMARK_DIR", tmp_path)
    write_watermark("session-a", 42)
    assert read_watermark("session-a") == 42


def test_parse_all_turns_accumulates_file_paths():
    """parse_all_turns carries forward file paths across turns."""
    indexed_lines = [
        (
            0,
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": [{"type": "text", "text": "Read A"}]},
                }
            ),
        ),
        (
            1,
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "input": {"file_path": "/a.py"},
                            }
                        ],
                        "usage": {},
                    },
                }
            ),
        ),
        (
            2,
            json.dumps(
                {
                    "type": "user",
                    "message": {"content": [{"type": "text", "text": "Read B"}]},
                }
            ),
        ),
        (
            3,
            json.dumps(
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "Read",
                                "input": {"file_path": "/b.py"},
                            }
                        ],
                        "usage": {},
                    },
                }
            ),
        ),
    ]
    turns = parse_all_turns(indexed_lines)
    assert len(turns) == 2
    assert "/a.py" in turns[1]["relevant_files"]
    assert "/b.py" in turns[1]["relevant_files"]


def test_normalize_extraction_response_schemas():
    """Both new and legacy response schemas are accepted."""
    new_schema = {"observations": [{"content": "A", "importance_score": 0.5}]}
    legacy_schema = {"has_observation": True, "content": "B", "importance_score": 0.6}

    new_result = normalize_extraction_response(new_schema)
    legacy_result = normalize_extraction_response(legacy_schema)

    assert len(new_result) == 1
    assert new_result[0]["content"] == "A"
    assert len(legacy_result) == 1
    assert legacy_result[0]["content"] == "B"


def test_normalize_worklog_response_validates_fields():
    """Worklog normalization enforces task_summary and defaults."""
    parsed = {
        "worklog": {
            "task_summary": "  Refactor hook transport  ",
            "workstream": "",
            "activity_type": "invalid",
            "tags": "not-a-list",
        }
    }
    result = normalize_worklog_response(parsed)
    assert len(result) == 1
    assert result[0]["task_summary"] == "Refactor hook transport"
    assert result[0]["workstream"] == "misc"
    assert result[0]["activity_type"] == "other"
    assert result[0]["tags"] == []


def test_build_ingest_event_id_is_deterministic():
    """Same stable fields produce identical ingest_event_id."""
    a = build_ingest_event_id("session-1", 99, "observation", "User prefers concise replies")
    b = build_ingest_event_id("session-1", 99, "observation", "User prefers concise replies")
    c = build_ingest_event_id("session-1", 99, "observation", "Different content")

    assert a == b
    assert a != c


def test_replay_success_drains_queue(tmp_path, monkeypatch):
    """Successful replay removes queued payloads."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / ".jarvis"))

    enqueue_ingest_payload({"observations": [{"content": "one"}]})
    enqueue_ingest_payload({"observations": [{"content": "two"}]})

    monkeypatch.setattr(
        extract_observation,
        "post_json",
        lambda *args, **kwargs: {"success": True, "data": {"success": True}},
    )

    replayed, stopped = replay_ingest_queue(batch_limit=10)
    assert replayed == 2
    assert stopped is False
    assert _read_queue_lines(tmp_path) == []


def test_replay_stops_on_failure_and_keeps_remaining(tmp_path, monkeypatch):
    """Replay leaves entries untouched from first failed request onward."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / ".jarvis"))

    enqueue_ingest_payload({"observations": [{"content": "one"}]})
    enqueue_ingest_payload({"observations": [{"content": "two"}]})

    monkeypatch.setattr(
        extract_observation,
        "post_json",
        lambda *args, **kwargs: {"success": False, "error": "down", "data": None},
    )

    replayed, stopped = replay_ingest_queue(batch_limit=10)
    assert replayed == 0
    assert stopped is True
    assert len(_read_queue_lines(tmp_path)) == 2


def test_main_enqueues_payload_on_ingest_failure(tmp_path, monkeypatch):
    """When ingest endpoint fails post-Haiku, payload is queued and watermark advances."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / ".jarvis"))
    monkeypatch.setattr(extract_observation, "WATERMARK_DIR", tmp_path / "sessions")

    transcript = _make_transcript(
        tmp_path,
        user_text="Please help me refactor this extraction flow with stable retries.",
        assistant_text="I updated the hook pipeline and adjusted the persistence logic accordingly.",
    )

    # Context succeeds, ingest fails.
    def _fake_post_json(endpoint_path, payload, timeout_seconds=2.5, mcp_json_path=None):
        if endpoint_path == "/hook/auto-extract/context":
            return {
                "success": True,
                "data": {
                    "auto_extract": {
                        "mode": "background",
                        "min_turn_chars": 10,
                        "max_transcript_lines": 500,
                        "max_observations": 3,
                        "dedup_threshold": 0.95,
                        "debug": False,
                    },
                    "worklog": {"enabled": True, "dedup_threshold": 0.7},
                    "known_workstreams": ["Jarvis Plugin"],
                },
            }
        if endpoint_path == "/hook/auto-extract/ingest":
            return {"success": False, "error": "connection refused", "data": None}
        raise AssertionError(f"Unexpected endpoint: {endpoint_path}")

    monkeypatch.setattr(extract_observation, "post_json", _fake_post_json)
    monkeypatch.setattr(
        extract_observation,
        "call_haiku",
        lambda prompt, mode="background": (
            {
                "observations": [
                    {
                        "content": "User wants deterministic replay-safe ingestion.",
                        "importance_score": 0.6,
                        "tags": ["hooks"],
                        "scope": "project",
                    }
                ],
                "worklog": {
                    "task_summary": "Refactoring hook ingest transport",
                    "workstream": "Jarvis Plugin",
                    "activity_type": "coding",
                    "tags": ["hooks"],
                },
            },
            100,
            50,
            "API",
        ),
    )

    monkeypatch.setattr(
        sys,
        "argv",
        [
            "extract_observation.py",
            "background",
            transcript,
            "session-123",
            str(tmp_path),
            "main",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        extract_observation.main()
    assert exc.value.code == 0

    queue_lines = _read_queue_lines(tmp_path)
    assert len(queue_lines) == 1

    # Watermark should have advanced (post-Haiku persistence failure rule).
    assert read_watermark("session-123") >= 1


def test_main_does_not_advance_watermark_on_haiku_failure(tmp_path, monkeypatch):
    """Haiku failure does not move session watermark."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / ".jarvis"))
    sessions_dir = tmp_path / "sessions"
    monkeypatch.setattr(extract_observation, "WATERMARK_DIR", sessions_dir)

    transcript = _make_transcript(
        tmp_path,
        user_text="Please check this stop-hook flow.",
        assistant_text="I inspected the flow and identified a potential retry issue.",
    )

    monkeypatch.setattr(
        extract_observation,
        "post_json",
        lambda endpoint_path, payload, **kwargs: {
            "success": True,
            "data": {
                "auto_extract": {
                    "mode": "background",
                    "min_turn_chars": 10,
                    "max_transcript_lines": 500,
                    "max_observations": 3,
                    "dedup_threshold": 0.95,
                    "debug": False,
                },
                "worklog": {"enabled": True, "dedup_threshold": 0.7},
                "known_workstreams": [],
            },
        }
        if endpoint_path == "/hook/auto-extract/context"
        else {"success": True, "data": {"success": True}},
    )
    monkeypatch.setattr(extract_observation, "call_haiku", lambda prompt, mode="background": None)

    monkeypatch.setattr(
        sys,
        "argv",
        ["extract_observation.py", "background", transcript, "session-xyz", str(tmp_path), "main"],
    )

    with pytest.raises(SystemExit) as exc:
        extract_observation.main()
    assert exc.value.code == 0
    assert read_watermark("session-xyz") == -1


def test_main_extracts_codex_turn_without_transcript(tmp_path, monkeypatch):
    """Codex Stop input is paired with saved prompt state without transcript parsing."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / ".jarvis"))
    monkeypatch.setattr(extract_observation, "WATERMARK_DIR", tmp_path / "sessions")
    prompt_text = "Please keep Codex passive extraction independent of transcript internals."
    assistant_text = (
        "Implemented a normalized turn journal and retained Claude transcript parsing "
        "as a richer fallback path."
    )
    assert capture_user_prompt(
        {
            "session_id": "codex-session",
            "turn_id": "turn-1",
            "prompt": prompt_text,
            "cwd": str(tmp_path),
        }
    )
    stop_input = {
        "session_id": "codex-session",
        "turn_id": "turn-1",
        "last_assistant_message": assistant_text,
    }
    monkeypatch.setenv("JARVIS_HOOK_INPUT", json.dumps(stop_input))

    ingested = []

    def _fake_post_json(endpoint_path, payload, **kwargs):
        if endpoint_path == "/hook/auto-extract/context":
            return {
                "success": True,
                "data": {
                    "auto_extract": {"min_turn_chars": 10, "max_observations": 3},
                    "worklog": {"enabled": False},
                    "known_workstreams": [],
                },
            }
        if endpoint_path == "/hook/auto-extract/ingest":
            ingested.append(payload)
            return {
                "success": True,
                "data": {"observations": [{"status": "stored", "id": "obs-1"}]},
            }
        raise AssertionError(f"Unexpected endpoint: {endpoint_path}")

    monkeypatch.setattr(extract_observation, "post_json", _fake_post_json)

    def _fake_call_haiku(extraction_prompt, mode="background"):
        assert prompt_text in extraction_prompt
        assert assistant_text in extraction_prompt
        return (
            {
                "observations": [
                    {
                        "content": "Codex extraction uses normalized hook turn state.",
                        "importance_score": 0.6,
                        "tags": ["codex", "hooks"],
                        "scope": "project",
                    }
                ]
            },
            100,
            40,
            "API",
        )

    monkeypatch.setattr(extract_observation, "call_haiku", _fake_call_haiku)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "extract_observation.py",
            "background",
            "-",
            "codex-session",
            str(tmp_path),
            "main",
        ],
    )

    extract_observation.main()

    assert len(ingested) == 1
    assert ingested[0]["context"]["transcript_line"] == 0
    assert read_watermark("codex-session.normalized-turns") == 0
    assert read_watermark("codex-session") == -1
