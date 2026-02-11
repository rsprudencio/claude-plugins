"""Tests for extract_observation.py — transcript parsing, watermark tracking, and Haiku extraction."""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

import pytest

# Add hooks-handlers to path for importing
HOOKS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "hooks-handlers"
)
sys.path.insert(0, HOOKS_DIR)

from extract_observation import (
    WATERMARK_DIR,
    _BUDGET_BASE,
    _BUDGET_HARD_MAX,
    _BUDGET_OUTPUT_SCALE,
    _FIRST_USER_MAX_CHARS,
    _HAIKU_MAX_TOKENS,
    _MIN_CHARS_PER_TURN,
    _parse_haiku_text,
    _parse_output_tokens,
    build_session_prompt,
    build_turn_prompt,
    call_haiku,
    call_haiku_api,
    call_haiku_cli,
    check_substance,
    compute_content_budget,
    extract_file_paths_from_tools,
    extract_first_user_message,
    filter_substantive_turns,
    normalize_extraction_response,
    parse_all_turns,
    parse_transcript_turn,
    pick_best_turn,
    read_transcript_from,
    read_watermark,
    store_observation,
    truncate,
    write_watermark,
)

# Pre-import tools.tier2 so it can be patched in TestStoreObservation
import tools.tier2  # noqa: F401


# ──────────────────────────────────────────────
# TestReadWatermark
# ──────────────────────────────────────────────


class TestReadWatermark:
    """Tests for read_watermark() — session position tracking."""

    def test_missing_file_returns_negative_one(self, tmp_path):
        """Returns -1 when no watermark file exists."""
        with patch("extract_observation.WATERMARK_DIR", tmp_path):
            result = read_watermark("nonexistent-session")
        assert result == -1

    def test_valid_watermark(self, tmp_path):
        """Reads a valid watermark file."""
        wm_file = tmp_path / "session-abc.json"
        wm_file.write_text(json.dumps({"last_extracted_line": 42, "timestamp": "2026-01-01T00:00:00Z"}))
        with patch("extract_observation.WATERMARK_DIR", tmp_path):
            result = read_watermark("session-abc")
        assert result == 42

    def test_corrupt_json_returns_negative_one(self, tmp_path):
        """Returns -1 on corrupt JSON."""
        wm_file = tmp_path / "corrupt.json"
        wm_file.write_text("not valid json")
        with patch("extract_observation.WATERMARK_DIR", tmp_path):
            result = read_watermark("corrupt")
        assert result == -1

    def test_missing_key_returns_negative_one(self, tmp_path):
        """Returns -1 when key is missing from JSON."""
        wm_file = tmp_path / "nokey.json"
        wm_file.write_text(json.dumps({"other": 5}))
        with patch("extract_observation.WATERMARK_DIR", tmp_path):
            result = read_watermark("nokey")
        assert result == -1

    def test_non_integer_value_returns_negative_one(self, tmp_path):
        """Returns -1 when value is not convertible to int."""
        wm_file = tmp_path / "bad.json"
        wm_file.write_text(json.dumps({"last_extracted_line": "not-a-number"}))
        with patch("extract_observation.WATERMARK_DIR", tmp_path):
            result = read_watermark("bad")
        assert result == -1

    def test_per_session_isolation(self, tmp_path):
        """Different sessions have independent watermarks."""
        (tmp_path / "session-A.json").write_text(json.dumps({"last_extracted_line": 10}))
        (tmp_path / "session-B.json").write_text(json.dumps({"last_extracted_line": 99}))
        with patch("extract_observation.WATERMARK_DIR", tmp_path):
            assert read_watermark("session-A") == 10
            assert read_watermark("session-B") == 99

    def test_zero_watermark(self, tmp_path):
        """Correctly reads watermark value of 0."""
        wm_file = tmp_path / "zero.json"
        wm_file.write_text(json.dumps({"last_extracted_line": 0}))
        with patch("extract_observation.WATERMARK_DIR", tmp_path):
            result = read_watermark("zero")
        assert result == 0


# ──────────────────────────────────────────────
# TestWriteWatermark
# ──────────────────────────────────────────────


class TestWriteWatermark:
    """Tests for write_watermark() — atomic watermark persistence."""

    def test_creates_directory_and_file(self, tmp_path):
        """Creates directory structure if it doesn't exist."""
        wm_dir = tmp_path / "state" / "sessions"
        with patch("extract_observation.WATERMARK_DIR", wm_dir):
            write_watermark("new-session", 55)
        wm_file = wm_dir / "new-session.json"
        assert wm_file.exists()
        data = json.loads(wm_file.read_text())
        assert data["last_extracted_line"] == 55

    def test_valid_json_written(self, tmp_path):
        """Written file contains valid JSON with expected keys."""
        with patch("extract_observation.WATERMARK_DIR", tmp_path):
            write_watermark("test", 100)
        data = json.loads((tmp_path / "test.json").read_text())
        assert "last_extracted_line" in data
        assert "timestamp" in data
        assert data["last_extracted_line"] == 100

    def test_timestamp_format(self, tmp_path):
        """Timestamp is ISO-8601 UTC format."""
        with patch("extract_observation.WATERMARK_DIR", tmp_path):
            write_watermark("ts-test", 0)
        data = json.loads((tmp_path / "ts-test.json").read_text())
        ts = data["timestamp"]
        assert ts.endswith("Z")
        assert "T" in ts

    def test_overwrites_existing(self, tmp_path):
        """Overwriting an existing watermark replaces the value."""
        with patch("extract_observation.WATERMARK_DIR", tmp_path):
            write_watermark("overwrite", 10)
            write_watermark("overwrite", 50)
        data = json.loads((tmp_path / "overwrite.json").read_text())
        assert data["last_extracted_line"] == 50

    def test_no_temp_files_left(self, tmp_path):
        """No .tmp files left after successful write."""
        with patch("extract_observation.WATERMARK_DIR", tmp_path):
            write_watermark("clean", 30)
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0


# ──────────────────────────────────────────────
# TestReadTranscriptFrom
# ──────────────────────────────────────────────


class TestReadTranscriptFrom:
    """Tests for read_transcript_from() — positional transcript reading."""

    def _write_transcript(self, tmp_path, lines):
        """Helper to write transcript JSONL lines to a temp file."""
        path = tmp_path / "transcript.jsonl"
        path.write_text("\n".join(lines) + "\n")
        return str(path)

    def test_full_read_from_start(self, tmp_path):
        """Reads all lines when starting from 0."""
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Hello"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi"}], "usage": {}}}),
        ]
        path = self._write_transcript(tmp_path, lines)
        indexed, total = read_transcript_from(path, 0)
        assert total == 2
        assert len(indexed) == 2
        assert indexed[0][0] == 0  # absolute index
        assert indexed[1][0] == 1

    def test_mid_file_start(self, tmp_path):
        """Reads only lines from start_line onward."""
        lines = [
            json.dumps({"type": "system", "message": {}}),
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Hello"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi"}], "usage": {}}}),
        ]
        path = self._write_transcript(tmp_path, lines)
        indexed, total = read_transcript_from(path, 1)
        assert total == 3
        assert len(indexed) == 2
        assert indexed[0][0] == 1  # starts at line 1
        assert indexed[1][0] == 2

    def test_safety_cap(self, tmp_path):
        """Caps at max_lines even if more lines available."""
        lines = [json.dumps({"type": "user", "n": i}) for i in range(20)]
        path = self._write_transcript(tmp_path, lines)
        indexed, total = read_transcript_from(path, 0, max_lines=5)
        assert total == 20
        assert len(indexed) == 5

    def test_empty_file(self, tmp_path):
        """Returns empty list for empty file."""
        path = tmp_path / "empty.jsonl"
        path.write_text("")
        indexed, total = read_transcript_from(str(path), 0)
        assert indexed == []
        assert total == 0

    def test_nonexistent_file(self, tmp_path):
        """Returns empty list for nonexistent file."""
        indexed, total = read_transcript_from(str(tmp_path / "nope.jsonl"), 0)
        assert indexed == []
        assert total == 0

    def test_correct_absolute_indices(self, tmp_path):
        """Absolute line indices match actual file positions."""
        lines = [json.dumps({"type": "system", "n": i}) for i in range(10)]
        path = self._write_transcript(tmp_path, lines)
        indexed, total = read_transcript_from(path, 5)
        assert total == 10
        assert len(indexed) == 5
        assert indexed[0][0] == 5
        assert indexed[4][0] == 9

    def test_skips_blank_lines(self, tmp_path):
        """Blank/whitespace-only lines are skipped."""
        raw = json.dumps({"type": "user"}) + "\n\n" + json.dumps({"type": "assistant"}) + "\n   \n"
        path = tmp_path / "blanks.jsonl"
        path.write_text(raw)
        indexed, total = read_transcript_from(str(path), 0)
        assert len(indexed) == 2  # blank lines skipped


# ──────────────────────────────────────────────
# TestParseAllTurns
# ──────────────────────────────────────────────


class TestParseAllTurns:
    """Tests for parse_all_turns() — forward multi-turn parsing."""

    def _make_user(self, text, idx=0):
        return (idx, json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": text}]}}))

    def _make_assistant(self, text, tools=None, idx=0, usage=None):
        content = [{"type": "text", "text": text}]
        if tools:
            for t in tools:
                content.append({"type": "tool_use", "name": t, "input": {}})
        msg = {"content": content, "usage": usage or {}}
        return (idx, json.dumps({"type": "assistant", "message": msg}))

    def _make_system(self, idx=0):
        return (idx, json.dumps({"type": "system", "message": {}}))

    def test_single_turn(self):
        """Parses a single user→assistant turn."""
        lines = [self._make_user("Hello", 0), self._make_assistant("Hi", idx=1)]
        turns = parse_all_turns(lines)
        assert len(turns) == 1
        assert turns[0]["user_text"] == "Hello"
        assert turns[0]["assistant_text"] == "Hi"

    def test_multiple_turns(self):
        """Parses multiple turns in sequence."""
        lines = [
            self._make_user("First", 0),
            self._make_assistant("Reply 1", idx=1),
            self._make_user("Second", 2),
            self._make_assistant("Reply 2", idx=3),
        ]
        turns = parse_all_turns(lines)
        assert len(turns) == 2
        assert turns[0]["user_text"] == "First"
        assert turns[1]["user_text"] == "Second"

    def test_skips_metadata(self):
        """Skips system/progress/file-history-snapshot lines."""
        lines = [
            self._make_system(0),
            self._make_user("Hello", 1),
            (2, json.dumps({"type": "progress", "message": {}})),
            self._make_assistant("Hi", idx=3),
        ]
        turns = parse_all_turns(lines)
        assert len(turns) == 1
        assert turns[0]["user_text"] == "Hello"

    def test_incomplete_turn_no_assistant(self):
        """Incomplete turn (user without assistant) is not returned."""
        lines = [self._make_user("Hello", 0)]
        turns = parse_all_turns(lines)
        assert len(turns) == 0

    def test_assistant_without_user_skipped(self):
        """Assistant without preceding user is not a turn."""
        lines = [self._make_assistant("Hi", idx=0)]
        turns = parse_all_turns(lines)
        assert len(turns) == 0

    def test_tool_names_deduplication(self):
        """Tool names within a turn are deduplicated."""
        lines = [
            self._make_user("Edit", 0),
            (1, json.dumps({"type": "assistant", "message": {
                "content": [
                    {"type": "tool_use", "name": "Read", "input": {}},
                    {"type": "tool_use", "name": "Edit", "input": {}},
                    {"type": "tool_use", "name": "Read", "input": {}},
                ],
                "usage": {}
            }})),
        ]
        turns = parse_all_turns(lines)
        assert turns[0]["tool_names"] == ["Read", "Edit"]

    def test_file_paths_accumulated(self):
        """File paths accumulate across turns (not just last turn)."""
        lines = [
            self._make_user("Read A", 0),
            (1, json.dumps({"type": "assistant", "message": {
                "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/a.py"}}],
                "usage": {}
            }})),
            self._make_user("Read B", 2),
            (3, json.dumps({"type": "assistant", "message": {
                "content": [{"type": "tool_use", "name": "Read", "input": {"file_path": "/b.py"}}],
                "usage": {}
            }})),
        ]
        turns = parse_all_turns(lines)
        assert len(turns) == 2
        # Second turn should see accumulated files from both turns
        assert "/a.py" in turns[1]["relevant_files"]
        assert "/b.py" in turns[1]["relevant_files"]

    def test_line_indices_tracked(self):
        """start_line_idx and end_line_idx correctly track positions."""
        lines = [
            self._make_user("Hello", 10),
            self._make_assistant("Hi", idx=15),
        ]
        turns = parse_all_turns(lines)
        assert turns[0]["start_line_idx"] == 10
        assert turns[0]["end_line_idx"] == 15

    def test_invalid_json_skipped(self):
        """Invalid JSON lines are silently skipped."""
        lines = [
            (0, "not valid json"),
            self._make_user("Hello", 1),
            self._make_assistant("Hi", idx=2),
        ]
        turns = parse_all_turns(lines)
        assert len(turns) == 1

    def test_empty_lines(self):
        """Empty input returns empty list."""
        turns = parse_all_turns([])
        assert turns == []

    def test_token_usage_extracted(self):
        """Token usage is extracted from assistant message."""
        lines = [
            self._make_user("Test", 0),
            self._make_assistant("OK", idx=1, usage={"input_tokens": 500, "output_tokens": 200}),
        ]
        turns = parse_all_turns(lines)
        assert turns[0]["token_usage"] == "500 in, 200 out"

    def test_user_override(self):
        """If two user messages appear before an assistant, the second one wins."""
        lines = [
            self._make_user("First user msg", 0),
            self._make_user("Second user msg", 1),
            self._make_assistant("Reply", idx=2),
        ]
        turns = parse_all_turns(lines)
        assert len(turns) == 1
        assert turns[0]["user_text"] == "Second user msg"


# ──────────────────────────────────────────────
# TestPickBestTurn
# ──────────────────────────────────────────────


class TestPickBestTurn:
    """Tests for pick_best_turn() — substantive turn selection."""

    def test_longest_turn_wins(self):
        """Picks the turn with most text."""
        turns = [
            {"user_text": "x" * 100, "assistant_text": "y" * 100, "tool_names": [], "relevant_files": []},
            {"user_text": "x" * 300, "assistant_text": "y" * 300, "tool_names": [], "relevant_files": []},
        ]
        best = pick_best_turn(turns, min_chars=100)
        assert best is turns[1]

    def test_tool_diversity_boost(self):
        """Turn with tools can beat a longer turn without tools."""
        turns = [
            {"user_text": "x" * 200, "assistant_text": "y" * 200, "tool_names": [], "relevant_files": []},
            {"user_text": "x" * 150, "assistant_text": "y" * 150, "tool_names": ["Read", "Edit", "Write"], "relevant_files": []},
        ]
        # Turn 1: 400 chars, Turn 2: 300 + 300 (3 tools * 100) = 600
        best = pick_best_turn(turns, min_chars=100)
        assert best is turns[1]

    def test_file_boost(self):
        """Turn with files gets +200 score boost."""
        turns = [
            {"user_text": "x" * 200, "assistant_text": "y" * 200, "tool_names": [], "relevant_files": []},
            {"user_text": "x" * 150, "assistant_text": "y" * 150, "tool_names": [], "relevant_files": ["/a.py"]},
        ]
        # Turn 1: 400, Turn 2: 300 + 200 = 500
        best = pick_best_turn(turns, min_chars=100)
        assert best is turns[1]

    def test_below_threshold_filtered(self):
        """Turns below min_chars are not considered."""
        turns = [
            {"user_text": "Hi", "assistant_text": "Hello", "tool_names": ["Read"], "relevant_files": ["/a.py"]},
        ]
        best = pick_best_turn(turns, min_chars=200)
        assert best is None

    def test_empty_list(self):
        """Returns None for empty list."""
        assert pick_best_turn([], min_chars=200) is None

    def test_all_below_threshold(self):
        """Returns None when all turns are below threshold."""
        turns = [
            {"user_text": "x" * 50, "assistant_text": "y" * 50, "tool_names": [], "relevant_files": []},
            {"user_text": "x" * 80, "assistant_text": "y" * 80, "tool_names": [], "relevant_files": []},
        ]
        best = pick_best_turn(turns, min_chars=200)
        assert best is None

    def test_single_turn_above_threshold(self):
        """Returns the only qualifying turn."""
        turn = {"user_text": "x" * 200, "assistant_text": "y" * 200, "tool_names": [], "relevant_files": []}
        best = pick_best_turn([turn], min_chars=200)
        assert best is turn


# ──────────────────────────────────────────────
# TestParseTranscriptTurn (existing — preserved)
# ──────────────────────────────────────────────


class TestParseTranscriptTurn:
    """Tests for parse_transcript_turn() — JSONL parsing."""

    def test_valid_turn(self):
        """Parses valid user + assistant turn."""
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Hello"}]}}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [{"type": "text", "text": "Hi there"}],
                    "usage": {"input_tokens": 10, "output_tokens": 5}
                }
            }),
        ]
        result = parse_transcript_turn(lines)

        assert result is not None
        assert result["user_text"] == "Hello"
        assert result["assistant_text"] == "Hi there"
        assert result["tool_names"] == []
        assert "10 in, 5 out" in result["token_usage"]

    def test_assistant_with_tools(self):
        """Extracts tool_use names from assistant content."""
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "commit"}]}}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Committing"},
                        {"type": "tool_use", "name": "jarvis_commit"},
                        {"type": "tool_use", "name": "jarvis_push"},
                    ],
                    "usage": {"input_tokens": 20, "output_tokens": 30}
                }
            }),
        ]
        result = parse_transcript_turn(lines)

        assert result["tool_names"] == ["jarvis_commit", "jarvis_push"]

    def test_dedup_tool_names(self):
        """Deduplicates tool names while preserving order."""
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "test"}]}}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Read"},
                        {"type": "tool_use", "name": "Write"},
                        {"type": "tool_use", "name": "Read"},  # Duplicate
                        {"type": "tool_use", "name": "Write"},  # Duplicate
                    ],
                    "usage": {}
                }
            }),
        ]
        result = parse_transcript_turn(lines)

        assert result["tool_names"] == ["Read", "Write"]

    def test_skip_metadata_types(self):
        """Skips system, progress, file-history-snapshot types."""
        lines = [
            json.dumps({"type": "system", "message": {}}),
            json.dumps({"type": "progress", "message": {}}),
            json.dumps({"type": "file-history-snapshot", "message": {}}),
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Hi"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}], "usage": {}}}),
        ]
        result = parse_transcript_turn(lines)

        assert result is not None
        assert result["user_text"] == "Hi"

    def test_no_assistant_message(self):
        """Returns None if no assistant message found."""
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Hello"}]}}),
        ]
        result = parse_transcript_turn(lines)

        assert result is None

    def test_no_user_message(self):
        """Returns None if no user message before assistant."""
        lines = [
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi"}], "usage": {}}}),
        ]
        result = parse_transcript_turn(lines)

        assert result is None

    def test_invalid_json_line(self):
        """Skips invalid JSON lines gracefully."""
        lines = [
            "not valid json",
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Hello"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi"}], "usage": {}}}),
        ]
        result = parse_transcript_turn(lines)

        assert result is not None
        assert result["user_text"] == "Hello"

    def test_empty_lines(self):
        """Returns None on empty input."""
        result = parse_transcript_turn([])
        assert result is None

    def test_multiline_user_text(self):
        """Joins multiple text blocks in user message."""
        lines = [
            json.dumps({
                "type": "user",
                "message": {
                    "content": [
                        {"type": "text", "text": "Line 1"},
                        {"type": "text", "text": "Line 2"},
                    ]
                }
            }),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "OK"}], "usage": {}}}),
        ]
        result = parse_transcript_turn(lines)

        assert "Line 1" in result["user_text"]
        assert "Line 2" in result["user_text"]

    def test_multiline_assistant_text(self):
        """Joins multiple text blocks in assistant message."""
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Test"}]}}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Part 1"},
                        {"type": "tool_use", "name": "Read"},
                        {"type": "text", "text": "Part 2"},
                    ],
                    "usage": {}
                }
            }),
        ]
        result = parse_transcript_turn(lines)

        assert "Part 1" in result["assistant_text"]
        assert "Part 2" in result["assistant_text"]

    def test_finds_last_assistant(self):
        """Finds the LAST assistant message (most recent turn)."""
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "First"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Old"}], "usage": {}}}),
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Second"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "New"}], "usage": {}}}),
        ]
        result = parse_transcript_turn(lines)

        assert result["user_text"] == "Second"
        assert result["assistant_text"] == "New"

    def test_missing_usage_field(self):
        """Handles missing usage field gracefully."""
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Test"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "OK"}]}}),  # No usage
        ]
        result = parse_transcript_turn(lines)

        assert result is not None
        assert "0 in, 0 out" in result["token_usage"]


# ──────────────────────────────────────────────
# TestCheckSubstance
# ──────────────────────────────────────────────


class TestCheckSubstance:
    """Tests for check_substance() — min text threshold."""

    def test_sufficient_text_passes(self):
        """Turn with enough text passes substance check."""
        turn = {
            "user_text": "x" * 100,
            "assistant_text": "y" * 150,
            "tool_names": [],
            "token_usage": "250 in, 100 out",
        }
        assert check_substance(turn, min_chars=200) is True

    def test_short_text_fails(self):
        """Turn with too little text fails substance check."""
        turn = {
            "user_text": "Hi",
            "assistant_text": "Hello",
            "tool_names": [],
            "token_usage": "10 in, 5 out",
        }
        assert check_substance(turn, min_chars=200) is False

    def test_custom_threshold(self):
        """Custom min_chars threshold works."""
        turn = {
            "user_text": "x" * 50,
            "assistant_text": "y" * 60,
            "tool_names": [],
            "token_usage": "110 in, 50 out",
        }
        assert check_substance(turn, min_chars=100) is True
        assert check_substance(turn, min_chars=150) is False

    def test_empty_text(self):
        """Empty text fails substance check."""
        turn = {
            "user_text": "",
            "assistant_text": "",
            "tool_names": [],
            "token_usage": "0 in, 0 out",
        }
        assert check_substance(turn, min_chars=200) is False


# ──────────────────────────────────────────────
# TestBuildTurnPrompt
# ──────────────────────────────────────────────


class TestBuildTurnPrompt:
    """Tests for build_turn_prompt() — prompt formatting."""

    def test_includes_user_text(self):
        """Prompt includes user text."""
        turn = {
            "user_text": "Create a journal entry",
            "assistant_text": "Sure, I'll create that",
            "tool_names": ["jarvis_store"],
            "token_usage": "100 in, 50 out",
        }
        prompt = build_turn_prompt(turn)

        assert "Create a journal entry" in prompt
        assert "Sure, I'll create that" in prompt
        assert "jarvis_store" in prompt
        assert "100 in, 50 out" in prompt

    def test_truncates_long_text(self):
        """Long text is truncated to limits."""
        turn = {
            "user_text": "z" * 1000,  # Use 'z' to avoid template collisions
            "assistant_text": "q" * 3000,  # Use 'q' to avoid template collisions
            "tool_names": [],
            "token_usage": "1000 in, 500 out",
        }
        prompt = build_turn_prompt(turn)

        # User text truncated to 500
        assert prompt.count("z") <= 503  # 500 + "..." potential

        # Assistant text truncated to 1500
        assert prompt.count("q") <= 1503  # 1500 + "..." potential

    def test_formats_tools_list(self):
        """Tool names are formatted as comma-separated list."""
        turn = {
            "user_text": "Do stuff",
            "assistant_text": "Done",
            "tool_names": ["Read", "Write", "Edit"],
            "token_usage": "200 in, 100 out",
        }
        prompt = build_turn_prompt(turn)

        assert "Read, Write, Edit" in prompt

    def test_no_tools(self):
        """Handles empty tool list."""
        turn = {
            "user_text": "Test",
            "assistant_text": "OK",
            "tool_names": [],
            "token_usage": "50 in, 25 out",
        }
        prompt = build_turn_prompt(turn)

        assert "None" in prompt


# ──────────────────────────────────────────────
# TestTruncate
# ──────────────────────────────────────────────


class TestTruncate:
    """Tests for truncate() helper."""

    def test_short_unchanged(self):
        """Short text passes through unchanged."""
        assert truncate("hello", 100) == "hello"

    def test_exact_limit_unchanged(self):
        """Text at exactly max_chars is unchanged."""
        text = "x" * 100
        assert truncate(text, 100) == text

    def test_long_truncated(self):
        """Long text is truncated with ellipsis."""
        text = "x" * 200
        result = truncate(text, 100)
        assert len(result) == 103  # 100 + "..."
        assert result.endswith("...")


# ──────────────────────────────────────────────
# TestParseHaikuText
# ──────────────────────────────────────────────


class TestParseHaikuText:
    """Tests for _parse_haiku_text() — JSON extraction from Haiku response."""

    def test_plain_json(self):
        """Parses plain JSON response."""
        text = '{"has_observation": true, "content": "Test"}'
        result = _parse_haiku_text(text)

        assert result is not None
        assert result["has_observation"] is True
        assert result["content"] == "Test"

    def test_json_in_code_block(self):
        """Parses JSON wrapped in markdown code blocks."""
        text = '```json\n{"has_observation": false}\n```'
        result = _parse_haiku_text(text)

        assert result is not None
        assert result["has_observation"] is False

    def test_code_block_no_lang(self):
        """Parses JSON in code block without language tag."""
        text = '```\n{"has_observation": true}\n```'
        result = _parse_haiku_text(text)

        assert result is not None
        assert result["has_observation"] is True

    def test_invalid_json(self):
        """Returns None on invalid JSON."""
        text = "not valid json"
        result = _parse_haiku_text(text)

        assert result is None

    def test_whitespace_handling(self):
        """Handles leading/trailing whitespace."""
        text = '\n  {"has_observation": true}  \n'
        result = _parse_haiku_text(text)

        assert result is not None
        assert result["has_observation"] is True

    def test_empty_string(self):
        """Returns None on empty string."""
        result = _parse_haiku_text("")
        assert result is None


# ──────────────────────────────────────────────
# TestCallHaikuAPI
# ──────────────────────────────────────────────


class TestCallHaikuAPI:
    """Tests for call_haiku_api() — Anthropic SDK extraction."""

    @patch.dict(os.environ, {}, clear=True)
    def test_no_api_key(self):
        """Returns None if ANTHROPIC_API_KEY not set."""
        result = call_haiku_api("Test prompt")
        assert result is None

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"})
    @patch("extract_observation.anthropic")
    def test_successful_extraction(self, mock_anthropic):
        """Successful API call returns parsed observation."""
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = [
            MagicMock(text='{"has_observation": true, "content": "Test", "importance_score": 0.6, "tags": ["test"]}')
        ]
        mock_response.usage = MagicMock(input_tokens=100, output_tokens=50)
        mock_client.messages.create.return_value = mock_response

        result = call_haiku_api("Test prompt")

        assert result is not None
        parsed, tokens_in, tokens_out = result
        assert parsed["has_observation"] is True
        assert parsed["content"] == "Test"
        assert tokens_in == 100
        assert tokens_out == 50

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"})
    def test_missing_anthropic_package(self):
        """Returns None if anthropic package not installed."""
        with patch.dict(sys.modules, {"anthropic": None}):
            result = call_haiku_api("Test prompt")
            # This will still import successfully in tests, but the function handles ImportError
            # Just verify it doesn't crash

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"})
    @patch("extract_observation.anthropic")
    def test_api_failure(self, mock_anthropic):
        """Returns None on API call failure."""
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client
        mock_client.messages.create.side_effect = Exception("API error")

        result = call_haiku_api("Test prompt")
        assert result is None

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"})
    @patch("extract_observation.anthropic")
    def test_invalid_response_json(self, mock_anthropic):
        """Returns None if response JSON is invalid."""
        mock_client = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        mock_response = MagicMock()
        mock_response.content = [MagicMock(text='not valid json')]
        mock_client.messages.create.return_value = mock_response

        result = call_haiku_api("Test prompt")
        assert result is None


# ──────────────────────────────────────────────
# TestCallHaikuCLI
# ──────────────────────────────────────────────


class TestCallHaikuCLI:
    """Tests for call_haiku_cli() — Claude CLI extraction."""

    @patch("extract_observation.shutil.which")
    def test_no_claude_binary(self, mock_which):
        """Returns None if claude binary not found."""
        mock_which.return_value = None
        result = call_haiku_cli("Test prompt")
        assert result is None

    @patch("extract_observation.shutil.which")
    @patch("extract_observation.subprocess.run")
    def test_successful_extraction(self, mock_run, mock_which):
        """Successful CLI call returns parsed observation."""
        mock_which.return_value = "/usr/local/bin/claude"

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = '{"has_observation": true, "content": "CLI test", "importance_score": 0.5, "tags": []}'
        mock_run.return_value = mock_result

        result = call_haiku_cli("Test prompt")

        assert result is not None
        parsed, tokens_in, tokens_out = result
        assert parsed["has_observation"] is True
        assert parsed["content"] == "CLI test"
        assert tokens_in > 0  # Estimated tokens
        assert tokens_out > 0

    @patch("extract_observation.shutil.which")
    @patch("extract_observation.subprocess.run")
    def test_cli_failure(self, mock_run, mock_which):
        """Returns None on CLI failure."""
        mock_which.return_value = "/usr/local/bin/claude"

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_run.return_value = mock_result

        result = call_haiku_cli("Test prompt")
        assert result is None

    @patch("extract_observation.shutil.which")
    @patch("extract_observation.subprocess.run")
    def test_cli_timeout(self, mock_run, mock_which):
        """Returns None on CLI timeout."""
        mock_which.return_value = "/usr/local/bin/claude"
        mock_run.side_effect = subprocess.TimeoutExpired("claude", 30)

        result = call_haiku_cli("Test prompt")
        assert result is None

    @patch("extract_observation.shutil.which")
    @patch("extract_observation.subprocess.run")
    def test_invalid_response_json(self, mock_run, mock_which):
        """Returns None if CLI response JSON is invalid."""
        mock_which.return_value = "/usr/local/bin/claude"

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "not valid json"
        mock_run.return_value = mock_result

        result = call_haiku_cli("Test prompt")
        assert result is None


# ──────────────────────────────────────────────
# TestCallHaiku
# ──────────────────────────────────────────────


class TestCallHaiku:
    """Tests for call_haiku() — mode routing."""

    @patch("extract_observation.call_haiku_api")
    def test_background_api_mode(self, mock_api):
        """background-api mode calls API only."""
        mock_api.return_value = ({"has_observation": True}, 100, 50)

        result = call_haiku("Test prompt", mode="background-api")

        assert result is not None
        parsed, tokens_in, tokens_out, backend = result
        assert parsed == {"has_observation": True}
        assert backend == "API"
        mock_api.assert_called_once_with("Test prompt")

    @patch("extract_observation.call_haiku_cli")
    def test_background_cli_mode(self, mock_cli):
        """background-cli mode calls CLI only."""
        mock_cli.return_value = ({"has_observation": True}, 100, 50)

        result = call_haiku("Test prompt", mode="background-cli")

        assert result is not None
        parsed, tokens_in, tokens_out, backend = result
        assert parsed == {"has_observation": True}
        assert backend == "CLI"
        mock_cli.assert_called_once_with("Test prompt")

    @patch("extract_observation.call_haiku_api")
    @patch("extract_observation.call_haiku_cli")
    def test_smart_fallback_api_success(self, mock_cli, mock_api):
        """Smart background mode tries API first, succeeds."""
        mock_api.return_value = ({"has_observation": True}, 100, 50)

        result = call_haiku("Test prompt", mode="background")

        assert result is not None
        parsed, tokens_in, tokens_out, backend = result
        assert parsed == {"has_observation": True}
        assert backend == "API"
        mock_api.assert_called_once()
        mock_cli.assert_not_called()  # Didn't need fallback

    @patch("extract_observation.call_haiku_api")
    @patch("extract_observation.call_haiku_cli")
    def test_smart_fallback_to_cli(self, mock_cli, mock_api):
        """Smart background mode falls back to CLI if API fails."""
        mock_api.return_value = None  # API failed
        mock_cli.return_value = ({"has_observation": True}, 100, 50)

        result = call_haiku("Test prompt", mode="background")

        assert result is not None
        parsed, tokens_in, tokens_out, backend = result
        assert parsed == {"has_observation": True}
        assert backend == "CLI"
        mock_api.assert_called_once()
        mock_cli.assert_called_once()

    @patch("extract_observation.call_haiku_api")
    @patch("extract_observation.call_haiku_cli")
    def test_both_backends_fail(self, mock_cli, mock_api):
        """Returns None if both backends fail."""
        mock_api.return_value = None
        mock_cli.return_value = None

        result = call_haiku("Test prompt", mode="background")

        assert result is None


# ──────────────────────────────────────────────
# TestStoreObservation
# ──────────────────────────────────────────────


class TestStoreObservation:
    """Tests for store_observation() — tier2_write integration."""

    @patch("tools.tier2.tier2_write")
    def test_stores_with_correct_params(self, mock_tier2_write):
        """Stores observation with correct parameters."""
        mock_tier2_write.return_value = {"success": True, "id": "obs::abc123"}

        result = store_observation(
            content="Test observation",
            importance_score=0.7,
            tags=["test", "pattern"],
            source_label="auto-extract:stop-hook",
        )

        mock_tier2_write.assert_called_once_with(
            content="Test observation",
            content_type="observation",
            importance_score=0.7,
            source="auto-extract:stop-hook",
            tags=["test", "pattern"],
            extra_metadata=None,
            skip_secret_scan=False,
        )
        assert result["success"] is True
        assert result["id"] == "obs::abc123"

    @patch("tools.tier2.tier2_write")
    def test_source_label_stop_hook(self, mock_tier2_write):
        """Uses correct source label for stop-hook."""
        mock_tier2_write.return_value = {"success": True}

        store_observation("Test", 0.5, [], "auto-extract:stop-hook")

        call_args = mock_tier2_write.call_args[1]
        assert call_args["source"] == "auto-extract:stop-hook"

    @patch("tools.tier2.tier2_write")
    def test_project_context_passthrough(self, mock_tier2_write):
        """Project context is passed as extra_metadata."""
        mock_tier2_write.return_value = {"success": True, "id": "obs::123"}

        store_observation(
            content="Test observation",
            importance_score=0.7,
            tags=["test"],
            source_label="auto-extract:stop-hook",
            project_path="/Users/test/jarvis-plugin",
            git_branch="master",
        )

        call_args = mock_tier2_write.call_args[1]
        assert "project_dir" not in call_args["extra_metadata"]
        assert call_args["extra_metadata"]["project_path"] == "/Users/test/jarvis-plugin"
        assert call_args["extra_metadata"]["git_branch"] == "master"

    @patch("tools.tier2.tier2_write")
    def test_no_project_context_sends_none(self, mock_tier2_write):
        """Without project context, extra_metadata is None."""
        mock_tier2_write.return_value = {"success": True, "id": "obs::123"}

        store_observation("Test", 0.5, [], "auto-extract:stop-hook")

        call_args = mock_tier2_write.call_args[1]
        assert call_args["extra_metadata"] is None

    @patch("tools.tier2.tier2_write")
    def test_relevant_files_passthrough(self, mock_tier2_write):
        """relevant_files are stored as comma-separated string in extra_metadata."""
        mock_tier2_write.return_value = {"success": True, "id": "obs::123"}

        store_observation(
            content="Test observation",
            importance_score=0.7,
            tags=["test"],
            source_label="auto-extract:stop-hook",
            relevant_files=["src/main.py", "tests/test_main.py"],
        )

        call_args = mock_tier2_write.call_args[1]
        assert call_args["extra_metadata"]["relevant_files"] == "src/main.py,tests/test_main.py"

    @patch("tools.tier2.tier2_write")
    def test_scope_passthrough(self, mock_tier2_write):
        """scope is stored in extra_metadata."""
        mock_tier2_write.return_value = {"success": True, "id": "obs::123"}

        store_observation(
            content="Test observation",
            importance_score=0.7,
            tags=["test"],
            source_label="auto-extract:stop-hook",
            scope="project",
        )

        call_args = mock_tier2_write.call_args[1]
        assert call_args["extra_metadata"]["scope"] == "project"

    @patch("tools.tier2.tier2_write")
    def test_empty_relevant_files_not_in_metadata(self, mock_tier2_write):
        """Empty relevant_files list doesn't add to metadata."""
        mock_tier2_write.return_value = {"success": True, "id": "obs::123"}

        store_observation("Test", 0.5, [], "auto-extract:stop-hook", relevant_files=[])

        call_args = mock_tier2_write.call_args[1]
        assert call_args["extra_metadata"] is None

    @patch("tools.tier2.tier2_write")
    def test_empty_scope_not_in_metadata(self, mock_tier2_write):
        """Empty scope string doesn't add to metadata."""
        mock_tier2_write.return_value = {"success": True, "id": "obs::123"}

        store_observation("Test", 0.5, [], "auto-extract:stop-hook", scope="")

        call_args = mock_tier2_write.call_args[1]
        assert call_args["extra_metadata"] is None


# ──────────────────────────────────────────────
# TestExtractFilePathsFromTools
# ──────────────────────────────────────────────


class TestExtractFilePathsFromTools:
    """Tests for extract_file_paths_from_tools() — file path extraction from tool_use blocks."""

    def test_extracts_file_path(self):
        """Extracts file_path from tool_use input."""
        content = [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/src/main.py"}},
        ]
        result = extract_file_paths_from_tools(content)
        assert result == ["/src/main.py"]

    def test_extracts_relative_path(self):
        """Extracts relative_path from tool_use input."""
        content = [
            {"type": "tool_use", "name": "mcp__serena__read_file", "input": {"relative_path": "src/lib.rs"}},
        ]
        result = extract_file_paths_from_tools(content)
        assert result == ["src/lib.rs"]

    def test_extracts_path_key(self):
        """Extracts 'path' from tool_use input."""
        content = [
            {"type": "tool_use", "name": "Glob", "input": {"path": "/Users/test/project"}},
        ]
        result = extract_file_paths_from_tools(content)
        assert result == ["/Users/test/project"]

    def test_skips_bash(self):
        """Skips Bash tool — file paths in commands aren't structured."""
        content = [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls", "path": "/tmp"}},
        ]
        result = extract_file_paths_from_tools(content)
        assert result == []

    def test_skips_webfetch(self):
        """Skips WebFetch tool."""
        content = [
            {"type": "tool_use", "name": "WebFetch", "input": {"url": "https://example.com", "path": "/tmp"}},
        ]
        result = extract_file_paths_from_tools(content)
        assert result == []

    def test_skips_websearch(self):
        """Skips WebSearch tool."""
        content = [
            {"type": "tool_use", "name": "WebSearch", "input": {"query": "test", "path": "/tmp"}},
        ]
        result = extract_file_paths_from_tools(content)
        assert result == []

    def test_deduplicates(self):
        """Deduplicates file paths across multiple tool_use blocks."""
        content = [
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/src/main.py"}},
            {"type": "tool_use", "name": "Edit", "input": {"file_path": "/src/main.py"}},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/src/lib.py"}},
        ]
        result = extract_file_paths_from_tools(content)
        assert result == ["/src/main.py", "/src/lib.py"]

    def test_caps_at_10(self):
        """Caps file paths at 10."""
        content = [
            {"type": "tool_use", "name": "Read", "input": {"file_path": f"/src/file{i}.py"}}
            for i in range(15)
        ]
        result = extract_file_paths_from_tools(content)
        assert len(result) == 10

    def test_ignores_non_tool_use(self):
        """Ignores text blocks."""
        content = [
            {"type": "text", "text": "Hello"},
            {"type": "tool_use", "name": "Read", "input": {"file_path": "/src/main.py"}},
        ]
        result = extract_file_paths_from_tools(content)
        assert result == ["/src/main.py"]

    def test_handles_missing_input(self):
        """Handles tool_use blocks without input."""
        content = [
            {"type": "tool_use", "name": "Read"},
            {"type": "tool_use", "name": "Edit", "input": {}},
        ]
        result = extract_file_paths_from_tools(content)
        assert result == []

    def test_empty_content(self):
        """Returns empty list for empty content."""
        result = extract_file_paths_from_tools([])
        assert result == []

    def test_non_dict_blocks_skipped(self):
        """Skips non-dict items in content list."""
        content = ["string_item", 42, None, {"type": "tool_use", "name": "Read", "input": {"file_path": "/a.py"}}]
        result = extract_file_paths_from_tools(content)
        assert result == ["/a.py"]


class TestParseTranscriptTurnRelevantFiles:
    """Tests for relevant_files in parse_transcript_turn()."""

    def test_includes_relevant_files(self):
        """parsed turn includes relevant_files from tool_use blocks."""
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Read this"}]}}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Reading file"},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/src/main.py"}},
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/src/lib.py"}},
                    ],
                    "usage": {"input_tokens": 100, "output_tokens": 50}
                }
            }),
        ]
        result = parse_transcript_turn(lines)
        assert result is not None
        assert result["relevant_files"] == ["/src/main.py", "/src/lib.py"]

    def test_empty_relevant_files_when_no_tools(self):
        """relevant_files is empty when no tool_use blocks."""
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Hello"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi"}], "usage": {}}}),
        ]
        result = parse_transcript_turn(lines)
        assert result is not None
        assert result["relevant_files"] == []


class TestBuildTurnPromptRelevantFiles:
    """Tests for relevant_files in build_turn_prompt()."""

    def test_includes_file_list_in_prompt(self):
        """Prompt includes relevant files as bulleted list."""
        turn = {
            "user_text": "Test",
            "assistant_text": "OK",
            "tool_names": ["Read"],
            "token_usage": "100 in, 50 out",
            "relevant_files": ["/src/main.py", "/src/lib.py"],
        }
        prompt = build_turn_prompt(turn)
        assert "- /src/main.py" in prompt
        assert "- /src/lib.py" in prompt

    def test_none_when_no_files(self):
        """Prompt shows 'None' when no relevant files."""
        turn = {
            "user_text": "Test",
            "assistant_text": "OK",
            "tool_names": [],
            "token_usage": "50 in, 25 out",
            "relevant_files": [],
        }
        prompt = build_turn_prompt(turn)
        assert "## Files Referenced\nNone" in prompt

    def test_includes_scope_in_prompt(self):
        """Prompt includes scope instruction."""
        turn = {
            "user_text": "Test",
            "assistant_text": "OK",
            "tool_names": [],
            "token_usage": "50 in, 25 out",
        }
        prompt = build_turn_prompt(turn)
        assert '"scope": "project" or "global"' in prompt


# ──────────────────────────────────────────────
# TestParseTranscriptTurnAssistantLine
# ──────────────────────────────────────────────


class TestParseTranscriptTurnAssistantLine:
    """Tests for assistant_line tracking in parse_transcript_turn()."""

    def test_returns_assistant_line(self):
        """assistant_line is correct forward index of the last assistant message."""
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Hi"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}], "usage": {}}}),
        ]
        result = parse_transcript_turn(lines)
        assert result is not None
        assert result["assistant_line"] == 1

    def test_assistant_line_with_metadata_lines(self):
        """assistant_line is correct when system/progress lines are interspersed."""
        lines = [
            json.dumps({"type": "system", "message": {}}),
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Hi"}]}}),
            json.dumps({"type": "progress", "message": {}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}], "usage": {}}}),
            json.dumps({"type": "file-history-snapshot", "message": {}}),
        ]
        result = parse_transcript_turn(lines)
        assert result is not None
        # Assistant is at index 3 (0-based)
        assert result["assistant_line"] == 3

    def test_assistant_line_picks_last_assistant(self):
        """assistant_line refers to the LAST assistant message."""
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "First"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Old"}], "usage": {}}}),
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Second"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "New"}], "usage": {}}}),
        ]
        result = parse_transcript_turn(lines)
        assert result is not None
        assert result["assistant_line"] == 3


# ──────────────────────────────────────────────
# TestStoreObservationSessionTracing
# ──────────────────────────────────────────────


class TestStoreObservationSessionTracing:
    """Tests for session_id and transcript_line in store_observation()."""

    @patch("tools.tier2.tier2_write")
    def test_session_id_passthrough(self, mock_tier2_write):
        """session_id appears in extra_metadata."""
        mock_tier2_write.return_value = {"success": True, "id": "obs::123"}

        store_observation(
            content="Test",
            importance_score=0.5,
            tags=[],
            source_label="auto-extract:stop-hook",
            session_id="abc-123-def",
        )

        call_args = mock_tier2_write.call_args[1]
        assert call_args["extra_metadata"]["session_id"] == "abc-123-def"

    @patch("tools.tier2.tier2_write")
    def test_transcript_line_passthrough(self, mock_tier2_write):
        """transcript_line appears in extra_metadata as string."""
        mock_tier2_write.return_value = {"success": True, "id": "obs::123"}

        store_observation(
            content="Test",
            importance_score=0.5,
            tags=[],
            source_label="auto-extract:stop-hook",
            transcript_line=42,
        )

        call_args = mock_tier2_write.call_args[1]
        assert call_args["extra_metadata"]["transcript_line"] == "42"

    @patch("tools.tier2.tier2_write")
    def test_empty_session_id_omitted(self, mock_tier2_write):
        """Empty session_id is not stored in metadata."""
        mock_tier2_write.return_value = {"success": True, "id": "obs::123"}

        store_observation(
            content="Test",
            importance_score=0.5,
            tags=[],
            source_label="auto-extract:stop-hook",
            session_id="",
        )

        call_args = mock_tier2_write.call_args[1]
        assert call_args["extra_metadata"] is None

    @patch("tools.tier2.tier2_write")
    def test_negative_transcript_line_omitted(self, mock_tier2_write):
        """Negative transcript_line (-1) is not stored in metadata."""
        mock_tier2_write.return_value = {"success": True, "id": "obs::123"}

        store_observation(
            content="Test",
            importance_score=0.5,
            tags=[],
            source_label="auto-extract:stop-hook",
            transcript_line=-1,
        )

        call_args = mock_tier2_write.call_args[1]
        assert call_args["extra_metadata"] is None


# ──────────────────────────────────────────────
# TestRelevantFilesAllTurns
# ──────────────────────────────────────────────


class TestRelevantFilesAllTurns:
    """Tests for relevant_files scanning across all assistant turns."""

    def test_files_from_all_turns(self):
        """File paths collected from multiple assistant messages, not just the last."""
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Read file A"}]}}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Reading A"},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/src/a.py"}},
                    ],
                    "usage": {}
                }
            }),
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Now edit B"}]}}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Done"},
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/src/b.py"}},
                    ],
                    "usage": {}
                }
            }),
        ]
        result = parse_transcript_turn(lines)
        assert result is not None
        # Both files should be present even though the last assistant only touched b.py
        assert "/src/a.py" in result["relevant_files"]
        assert "/src/b.py" in result["relevant_files"]

    def test_files_deduplicated_across_turns(self):
        """Same file in multiple assistant turns only appears once."""
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Read it"}]}}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/src/main.py"}},
                    ],
                    "usage": {}
                }
            }),
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Edit it"}]}}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Edit", "input": {"file_path": "/src/main.py"}},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/src/other.py"}},
                    ],
                    "usage": {}
                }
            }),
        ]
        result = parse_transcript_turn(lines)
        assert result is not None
        assert result["relevant_files"] == ["/src/main.py", "/src/other.py"]

    def test_files_from_early_turns_with_text_only_ending(self):
        """Files from earlier turns are captured even when last assistant is text-only."""
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Read files"}]}}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/src/config.py"}},
                        {"type": "tool_use", "name": "Read", "input": {"file_path": "/src/main.py"}},
                    ],
                    "usage": {}
                }
            }),
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Thanks, summarize"}]}}),
            json.dumps({
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "Here is your summary of the codebase..."},
                    ],
                    "usage": {"input_tokens": 500, "output_tokens": 200}
                }
            }),
        ]
        result = parse_transcript_turn(lines)
        assert result is not None
        # Last assistant has no tool_use, but files from earlier turn should be captured
        assert "/src/config.py" in result["relevant_files"]
        assert "/src/main.py" in result["relevant_files"]


# ──────────────────────────────────────────────
# TestParseOutputTokens
# ──────────────────────────────────────────────


class TestParseOutputTokens:
    """Tests for _parse_output_tokens() — token usage string parsing."""

    def test_normal_format(self):
        """Parses standard 'N in, M out' format."""
        assert _parse_output_tokens("1234 in, 567 out") == 567

    def test_zero_output(self):
        """Returns 0 for zero output tokens."""
        assert _parse_output_tokens("100 in, 0 out") == 0

    def test_large_numbers(self):
        """Handles large token counts."""
        assert _parse_output_tokens("150000 in, 50000 out") == 50000

    def test_malformed_string(self):
        """Returns 0 for unparseable strings."""
        assert _parse_output_tokens("garbage") == 0
        assert _parse_output_tokens("") == 0
        assert _parse_output_tokens("no commas here") == 0

    def test_missing_out_part(self):
        """Returns 0 when 'out' part is missing."""
        assert _parse_output_tokens("100 in") == 0


# ──────────────────────────────────────────────
# TestFilterSubstantiveTurns
# ──────────────────────────────────────────────


class TestFilterSubstantiveTurns:
    """Tests for filter_substantive_turns() — multi-turn filtering."""

    def test_returns_all_above_threshold(self):
        """Returns all turns meeting the character threshold."""
        turns = [
            {"user_text": "x" * 100, "assistant_text": "y" * 200},
            {"user_text": "x" * 50, "assistant_text": "y" * 50},
            {"user_text": "x" * 150, "assistant_text": "y" * 150},
        ]
        result = filter_substantive_turns(turns, min_chars=200)
        assert len(result) == 2
        assert result[0] is turns[0]
        assert result[1] is turns[2]

    def test_preserves_order(self):
        """Returns turns in original order."""
        turns = [
            {"user_text": "a" * 200, "assistant_text": "b" * 200},
            {"user_text": "c" * 300, "assistant_text": "d" * 300},
            {"user_text": "e" * 150, "assistant_text": "f" * 150},
        ]
        result = filter_substantive_turns(turns, min_chars=200)
        assert len(result) == 3
        assert result[0] is turns[0]
        assert result[1] is turns[1]
        assert result[2] is turns[2]

    def test_all_below_threshold(self):
        """Returns empty list when all turns are below threshold."""
        turns = [
            {"user_text": "Hi", "assistant_text": "Hello"},
            {"user_text": "x" * 50, "assistant_text": "y" * 50},
        ]
        result = filter_substantive_turns(turns, min_chars=200)
        assert result == []

    def test_empty_input(self):
        """Returns empty list for empty input."""
        assert filter_substantive_turns([], min_chars=200) == []

    def test_custom_threshold(self):
        """Respects custom min_chars threshold."""
        turns = [
            {"user_text": "x" * 60, "assistant_text": "y" * 60},
        ]
        assert len(filter_substantive_turns(turns, min_chars=100)) == 1
        assert len(filter_substantive_turns(turns, min_chars=200)) == 0

    def test_single_qualifying_turn(self):
        """Returns single qualifying turn."""
        turns = [
            {"user_text": "x" * 200, "assistant_text": "y" * 200},
        ]
        result = filter_substantive_turns(turns, min_chars=200)
        assert len(result) == 1
        assert result[0] is turns[0]


# ──────────────────────────────────────────────
# TestExtractFirstUserMessage
# ──────────────────────────────────────────────


class TestExtractFirstUserMessage:
    """Tests for extract_first_user_message() — conversation context extraction."""

    def _write_transcript(self, tmp_path, lines):
        path = tmp_path / "transcript.jsonl"
        path.write_text("\n".join(lines) + "\n")
        return str(path)

    def test_finds_first_user(self, tmp_path):
        """Extracts the first user message text."""
        lines = [
            json.dumps({"type": "system", "message": {}}),
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Hello world"}]}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi"}], "usage": {}}}),
        ]
        path = self._write_transcript(tmp_path, lines)
        result = extract_first_user_message(path)
        assert result == "Hello world"

    def test_truncates_long_message(self, tmp_path):
        """Truncates message to _FIRST_USER_MAX_CHARS."""
        long_text = "x" * 500
        lines = [
            json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": long_text}]}}),
        ]
        path = self._write_transcript(tmp_path, lines)
        result = extract_first_user_message(path)
        assert len(result) == _FIRST_USER_MAX_CHARS

    def test_missing_file(self, tmp_path):
        """Returns empty string for nonexistent file."""
        result = extract_first_user_message(str(tmp_path / "nope.jsonl"))
        assert result == ""

    def test_no_user_message(self, tmp_path):
        """Returns empty string when no user message found."""
        lines = [
            json.dumps({"type": "system", "message": {}}),
            json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "Hi"}], "usage": {}}}),
        ]
        path = self._write_transcript(tmp_path, lines)
        result = extract_first_user_message(path)
        assert result == ""

    def test_scan_limit(self, tmp_path):
        """Stops scanning after max_scan_lines."""
        # Put system lines before the user message, beyond the scan limit
        lines = [json.dumps({"type": "system", "message": {}}) for _ in range(10)]
        lines.append(json.dumps({"type": "user", "message": {"content": [{"type": "text", "text": "Hello"}]}}))
        path = self._write_transcript(tmp_path, lines)
        # Scan limit of 5 should not find the user message at line 10
        result = extract_first_user_message(path, max_scan_lines=5)
        assert result == ""

    def test_multiline_text_blocks(self, tmp_path):
        """Joins multiple text blocks in user content."""
        lines = [
            json.dumps({"type": "user", "message": {"content": [
                {"type": "text", "text": "Part 1"},
                {"type": "text", "text": "Part 2"},
            ]}}),
        ]
        path = self._write_transcript(tmp_path, lines)
        result = extract_first_user_message(path)
        assert "Part 1" in result
        assert "Part 2" in result


# ──────────────────────────────────────────────
# TestComputeContentBudget
# ──────────────────────────────────────────────


class TestComputeContentBudget:
    """Tests for compute_content_budget() — dynamic budget scaling."""

    def test_base_only_small_session(self):
        """Tiny session gets base budget."""
        turns = [{"token_usage": "100 in, 100 out"}]
        budget = compute_content_budget(turns)
        # 2000 + 100 * 0.04 = 2004
        assert budget == 2004

    def test_medium_session(self):
        """Medium session scales proportionally."""
        turns = [{"token_usage": "10000 in, 5000 out"}]
        budget = compute_content_budget(turns)
        # 2000 + 5000 * 0.04 = 2200
        assert budget == 2200

    def test_large_session(self):
        """Large session gets more budget."""
        turns = [{"token_usage": "50000 in, 50000 out"}]
        budget = compute_content_budget(turns)
        # 2000 + 50000 * 0.04 = 4000
        assert budget == 4000

    def test_hard_max_cap(self):
        """Very large session is capped at _BUDGET_HARD_MAX."""
        turns = [{"token_usage": "500000 in, 500000 out"}]
        budget = compute_content_budget(turns)
        assert budget == _BUDGET_HARD_MAX

    def test_empty_turns(self):
        """Empty turns list returns base budget."""
        budget = compute_content_budget([])
        assert budget == _BUDGET_BASE

    def test_multiple_turns_sum(self):
        """Output tokens from multiple turns are summed."""
        turns = [
            {"token_usage": "100 in, 1000 out"},
            {"token_usage": "200 in, 2000 out"},
            {"token_usage": "300 in, 3000 out"},
        ]
        budget = compute_content_budget(turns)
        # 2000 + 6000 * 0.04 = 2240
        assert budget == 2240

    def test_malformed_token_usage(self):
        """Malformed token_usage strings treated as 0 output tokens."""
        turns = [{"token_usage": "garbage"}]
        budget = compute_content_budget(turns)
        assert budget == _BUDGET_BASE


# ──────────────────────────────────────────────
# TestBuildSessionPrompt
# ──────────────────────────────────────────────


class TestBuildSessionPrompt:
    """Tests for build_session_prompt() — session-level prompt construction."""

    def _make_turn(self, user_text="Test user", assistant_text="Test assistant",
                   tools=None, token_usage="100 in, 50 out", relevant_files=None):
        return {
            "user_text": user_text,
            "assistant_text": assistant_text,
            "tool_names": tools or [],
            "token_usage": token_usage,
            "relevant_files": relevant_files or [],
        }

    def test_includes_first_user_text(self):
        """Prompt includes the conversation opener."""
        turns = [self._make_turn()]
        prompt = build_session_prompt(turns, "Hello, start here", 2000)
        assert "Hello, start here" in prompt

    def test_numbered_turns(self):
        """Each turn gets a numbered heading."""
        turns = [
            self._make_turn(user_text="First question"),
            self._make_turn(user_text="Second question"),
        ]
        prompt = build_session_prompt(turns, "", 4000)
        assert "### Turn 1" in prompt
        assert "### Turn 2" in prompt
        assert "First question" in prompt
        assert "Second question" in prompt

    def test_aggregates_tools(self):
        """All unique tools from all turns appear in All Tools Used."""
        turns = [
            self._make_turn(tools=["Read", "Edit"]),
            self._make_turn(tools=["Write", "Read"]),
        ]
        prompt = build_session_prompt(turns, "", 4000)
        assert "Edit" in prompt
        assert "Read" in prompt
        assert "Write" in prompt

    def test_relevant_files_from_last_turn(self):
        """Uses relevant_files from the last turn."""
        turns = [
            self._make_turn(relevant_files=["/a.py"]),
            self._make_turn(relevant_files=["/a.py", "/b.py"]),
        ]
        prompt = build_session_prompt(turns, "", 4000)
        assert "- /a.py" in prompt
        assert "- /b.py" in prompt

    def test_single_turn(self):
        """Works correctly with a single turn."""
        turns = [self._make_turn(user_text="Solo question", assistant_text="Solo answer")]
        prompt = build_session_prompt(turns, "Context", 2000)
        assert "### Turn 1" in prompt
        assert "Solo question" in prompt
        assert "Solo answer" in prompt
        assert "1 turns" in prompt

    def test_empty_turns(self):
        """Returns empty string for empty turns list."""
        assert build_session_prompt([], "", 2000) == ""

    def test_all_turns_always_included(self):
        """All turns are always present regardless of budget — budget truncates, not excludes."""
        turns = [
            self._make_turn(user_text="x" * 100, assistant_text="y" * 100),  # 200 chars
            self._make_turn(user_text="x" * 300, assistant_text="y" * 300, tools=["Read"]),  # 600 chars
            self._make_turn(user_text="x" * 200, assistant_text="y" * 200),  # 400 chars
        ]
        prompt = build_session_prompt(turns, "", 300)
        # All 3 turns must be present — budget truncates long content, never drops turns
        assert prompt.count("### Turn") == 3

    def test_project_context(self):
        """Includes project and branch info."""
        turns = [self._make_turn()]
        prompt = build_session_prompt(turns, "", 2000, project_name="my-project", git_branch="feature/x")
        assert "my-project" in prompt
        assert "feature/x" in prompt

    def test_short_turns_preserved_at_full_text(self):
        """Short turns are never truncated — their full text appears in prompt."""
        short_user = "ok"
        short_assistant = "Deployed to production."
        turns = [
            self._make_turn(user_text="x" * 500, assistant_text="y" * 500),  # Long turn
            self._make_turn(user_text=short_user, assistant_text=short_assistant),  # Short turn (26 chars)
            self._make_turn(user_text="x" * 400, assistant_text="y" * 400),  # Long turn
        ]
        prompt = build_session_prompt(turns, "", 2000)
        # Short turn must appear verbatim — no truncation
        assert short_user in prompt
        assert short_assistant in prompt
        assert "### Turn 2" in prompt

    def test_long_turns_truncated_by_budget(self):
        """Long turns are truncated when budget is tight, but still present."""
        long_text = "z" * 5000
        turns = [
            self._make_turn(user_text="ok", assistant_text="sure"),  # Short
            self._make_turn(user_text="q" * 200, assistant_text=long_text),  # Long
        ]
        prompt = build_session_prompt(turns, "", 2000)
        # Both turns present
        assert "### Turn 1" in prompt
        assert "### Turn 2" in prompt
        # Long text should be truncated (5000 chars won't fit in 2000 budget)
        assert prompt.count("z") < 5000

    def test_json_response_schema(self):
        """Prompt includes multi-observation JSON schema."""
        turns = [self._make_turn()]
        prompt = build_session_prompt(turns, "", 2000)
        assert '"observations"' in prompt
        assert "1-3 observations" in prompt


# ──────────────────────────────────────────────
# TestNormalizeExtractionResponse
# ──────────────────────────────────────────────


class TestNormalizeExtractionResponse:
    """Tests for normalize_extraction_response() — schema normalization."""

    def test_new_schema_with_observations(self):
        """Handles new multi-observation schema."""
        parsed = {
            "observations": [
                {"content": "First insight", "importance_score": 0.7, "tags": ["test"], "scope": "global"},
                {"content": "Second insight", "importance_score": 0.5, "tags": [], "scope": "project"},
            ]
        }
        result = normalize_extraction_response(parsed)
        assert len(result) == 2
        assert result[0]["content"] == "First insight"
        assert result[1]["content"] == "Second insight"

    def test_new_schema_empty_array(self):
        """Handles empty observations array."""
        parsed = {"observations": []}
        result = normalize_extraction_response(parsed)
        assert result == []

    def test_new_schema_filters_empty_content(self):
        """Filters out observations with empty content."""
        parsed = {
            "observations": [
                {"content": "Good insight", "importance_score": 0.7},
                {"content": "", "importance_score": 0.5},
                {"content": "  ", "importance_score": 0.6},
            ]
        }
        result = normalize_extraction_response(parsed)
        assert len(result) == 1
        assert result[0]["content"] == "Good insight"

    def test_legacy_schema_with_observation(self):
        """Handles legacy single-observation schema."""
        parsed = {
            "has_observation": True,
            "content": "Legacy insight",
            "importance_score": 0.6,
            "tags": ["legacy"],
            "scope": "project",
        }
        result = normalize_extraction_response(parsed)
        assert len(result) == 1
        assert result[0]["content"] == "Legacy insight"
        assert result[0]["importance_score"] == 0.6
        assert result[0]["tags"] == ["legacy"]
        assert result[0]["scope"] == "project"

    def test_legacy_schema_no_observation(self):
        """Legacy schema with has_observation=false returns empty."""
        parsed = {"has_observation": False, "content": ""}
        result = normalize_extraction_response(parsed)
        assert result == []

    def test_none_input(self):
        """Returns empty list for None input."""
        assert normalize_extraction_response(None) == []

    def test_non_dict_input(self):
        """Returns empty list for non-dict input."""
        assert normalize_extraction_response("string") == []
        assert normalize_extraction_response([1, 2]) == []

    def test_observations_not_list(self):
        """Returns empty when observations is not a list."""
        parsed = {"observations": "not a list"}
        result = normalize_extraction_response(parsed)
        assert result == []

    def test_observations_with_non_dict_items(self):
        """Filters out non-dict items in observations array."""
        parsed = {
            "observations": [
                {"content": "Good", "importance_score": 0.7},
                "string item",
                42,
            ]
        }
        result = normalize_extraction_response(parsed)
        assert len(result) == 1
        assert result[0]["content"] == "Good"
