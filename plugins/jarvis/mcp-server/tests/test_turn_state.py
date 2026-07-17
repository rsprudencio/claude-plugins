"""Harness fixture tests for normalized passive-extraction turn state."""

import json
import os
import sys
from pathlib import Path


HOOKS_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "hooks-handlers")
sys.path.insert(0, HOOKS_DIR)

from extract_observation import parse_all_turns
from turn_state import capture_user_prompt, complete_stop_turn


FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_codex_fixtures_form_normalized_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / ".jarvis"))
    prompt = _fixture("codex_user_prompt_submit.json")
    stop = _fixture("codex_stop.json")

    assert capture_user_prompt(prompt) is True
    turns, last_sequence, first_user = complete_stop_turn(stop)

    assert last_sequence == 0
    assert first_user == prompt["prompt"]
    assert turns == [
        {
            "user_text": prompt["prompt"],
            "assistant_text": stop["last_assistant_message"],
            "tool_names": [],
            "token_usage": "240 in, 90 out",
            "relevant_files": [],
            "start_line_idx": 0,
            "end_line_idx": 0,
        }
    ]


def test_normalized_turn_watermark_allows_retry_then_suppresses(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / ".jarvis"))
    assert capture_user_prompt(_fixture("codex_user_prompt_submit.json"))

    retry_turns, _, _ = complete_stop_turn(_fixture("codex_stop.json"), 0)
    processed_turns, _, _ = complete_stop_turn(_fixture("codex_stop.json"), 1)

    assert len(retry_turns) == 1
    assert processed_turns == []


def test_stable_codex_turn_id_deduplicates_prompt_hook_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / ".jarvis"))
    prompt = _fixture("codex_user_prompt_submit.json")
    assert capture_user_prompt(prompt)
    assert capture_user_prompt(prompt)

    turns, last_sequence, _ = complete_stop_turn(_fixture("codex_stop.json"))

    assert len(turns) == 1
    assert last_sequence == 0


def test_claude_transcript_fixture_retains_rich_turn_metadata():
    lines = (FIXTURES / "claude_transcript.jsonl").read_text(encoding="utf-8").splitlines()
    turns = parse_all_turns(list(enumerate(lines)))

    assert len(turns) == 1
    assert turns[0]["user_text"].startswith("Preserve Claude")
    assert turns[0]["assistant_text"].startswith("The Claude transcript")
    assert turns[0]["tool_names"] == ["Read"]
    assert turns[0]["relevant_files"] == ["/workspace/jarvis/hook.py"]
    assert turns[0]["token_usage"] == "180 in, 75 out"
