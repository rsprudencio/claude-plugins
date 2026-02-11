"""Tests for extract_observation.py — transcript parsing and Haiku extraction."""
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
    COOLDOWN_SENTINEL,
    _parse_haiku_text,
    build_turn_prompt,
    call_haiku,
    call_haiku_api,
    call_haiku_cli,
    check_cooldown,
    check_substance,
    extract_file_paths_from_tools,
    parse_transcript_turn,
    store_observation,
    truncate,
)

# Pre-import tools.tier2 so it can be patched in TestStoreObservation
import tools.tier2  # noqa: F401


# ──────────────────────────────────────────────
# TestParseTranscriptTurn
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
# TestCheckCooldown
# ──────────────────────────────────────────────


class TestCheckCooldown:
    """Tests for check_cooldown() — rate limiting."""

    def test_first_extraction_passes(self):
        """First extraction passes (no sentinel file)."""
        # Clean up any existing sentinel
        if COOLDOWN_SENTINEL.exists():
            COOLDOWN_SENTINEL.unlink()

        assert check_cooldown(cooldown_seconds=120) is True
        assert COOLDOWN_SENTINEL.exists()

        # Clean up
        COOLDOWN_SENTINEL.unlink()

    def test_immediate_retry_blocked(self):
        """Immediate retry within cooldown is blocked."""
        # Create sentinel
        if COOLDOWN_SENTINEL.exists():
            COOLDOWN_SENTINEL.unlink()
        COOLDOWN_SENTINEL.touch()

        assert check_cooldown(cooldown_seconds=120) is False

        # Clean up
        COOLDOWN_SENTINEL.unlink()

    def test_expired_cooldown_passes(self):
        """Expired cooldown allows extraction."""
        # Create old sentinel
        if COOLDOWN_SENTINEL.exists():
            COOLDOWN_SENTINEL.unlink()
        COOLDOWN_SENTINEL.touch()

        # Backdate it
        old_time = time.time() - 200
        os.utime(COOLDOWN_SENTINEL, (old_time, old_time))

        assert check_cooldown(cooldown_seconds=120) is True

        # Clean up
        COOLDOWN_SENTINEL.unlink()

    def test_custom_cooldown_seconds(self):
        """Custom cooldown_seconds works."""
        # Create recent sentinel
        if COOLDOWN_SENTINEL.exists():
            COOLDOWN_SENTINEL.unlink()
        COOLDOWN_SENTINEL.touch()

        # Backdate it by 10 seconds
        recent_time = time.time() - 10
        os.utime(COOLDOWN_SENTINEL, (recent_time, recent_time))

        # Should fail with 60s cooldown
        assert check_cooldown(cooldown_seconds=60) is False

        # Should pass with 5s cooldown
        assert check_cooldown(cooldown_seconds=5) is True

        # Clean up
        COOLDOWN_SENTINEL.unlink()


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
            project_dir="jarvis-plugin",
            project_path="/Users/test/jarvis-plugin",
            git_branch="master",
        )

        call_args = mock_tier2_write.call_args[1]
        assert call_args["extra_metadata"]["project_dir"] == "jarvis-plugin"
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
