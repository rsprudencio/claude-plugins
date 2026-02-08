"""Tests for extract_observation.py — Haiku extraction and storage."""
import json
import os
import subprocess
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add hooks-handlers to path for importing
HOOKS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "hooks-handlers"
)
sys.path.insert(0, HOOKS_DIR)

from extract_observation import (
    MAX_OUTPUT_CHARS,
    _build_prompt,
    _parse_haiku_text,
    call_haiku,
    call_haiku_api,
    call_haiku_cli,
    store_observation,
    summarize_tool_input,
    truncate_for_prompt,
)

# Pre-import tools.tier2 so it can be patched in TestStoreObservation
import tools.tier2  # noqa: F401


# ──────────────────────────────────────────────
# TestTruncateForPrompt
# ──────────────────────────────────────────────

class TestTruncateForPrompt:
    """Tests for truncate_for_prompt()."""

    def test_short_text_unchanged(self):
        """Short text passes through unchanged."""
        text = "Hello, world!"
        assert truncate_for_prompt(text) == text

    def test_exact_limit_unchanged(self):
        """Text at exactly MAX_OUTPUT_CHARS is not truncated."""
        text = "x" * MAX_OUTPUT_CHARS
        assert truncate_for_prompt(text) == text

    def test_long_text_truncated(self):
        """Long text is truncated with indicator."""
        text = "x" * (MAX_OUTPUT_CHARS + 500)
        result = truncate_for_prompt(text)
        assert len(result) < len(text)
        assert "truncated" in result
        assert str(len(text)) in result

    def test_custom_limit(self):
        """Custom max_chars works."""
        text = "x" * 200
        result = truncate_for_prompt(text, max_chars=100)
        assert len(result) < 200
        assert "truncated" in result


class TestSummarizeToolInput:
    """Tests for summarize_tool_input()."""

    def test_short_input(self):
        """Short tool input is returned as JSON."""
        tool_input = {"operation": "create", "description": "test"}
        result = summarize_tool_input(tool_input)
        assert "create" in result
        assert "test" in result

    def test_long_input_truncated(self):
        """Long tool input is truncated to 500 chars."""
        tool_input = {"data": "x" * 1000}
        result = summarize_tool_input(tool_input)
        assert len(result) <= 503  # 500 + "..."
        assert result.endswith("...")


# ──────────────────────────────────────────────
# TestParseHaikuText
# ──────────────────────────────────────────────

class TestParseHaikuText:
    """Tests for _parse_haiku_text()."""

    def test_plain_json(self):
        """Parses plain JSON response."""
        text = '{"has_observation": true, "content": "test", "importance_score": 0.5, "topics": []}'
        result = _parse_haiku_text(text)
        assert result is not None
        assert result["has_observation"] is True

    def test_json_in_code_block(self):
        """Handles JSON wrapped in markdown code blocks."""
        text = '```json\n{"has_observation": true, "content": "test", "importance_score": 0.5, "topics": []}\n```'
        result = _parse_haiku_text(text)
        assert result is not None
        assert result["has_observation"] is True

    def test_json_in_bare_code_block(self):
        """Handles JSON in bare ``` blocks (no language tag)."""
        text = '```\n{"has_observation": false}\n```'
        result = _parse_haiku_text(text)
        assert result is not None
        assert result["has_observation"] is False

    def test_invalid_json(self):
        """Returns None for non-JSON text."""
        assert _parse_haiku_text("This is not JSON") is None

    def test_empty_string(self):
        """Returns None for empty string."""
        assert _parse_haiku_text("") is None

    def test_whitespace_padding(self):
        """Handles leading/trailing whitespace."""
        text = '  \n{"has_observation": true, "content": "x", "importance_score": 0.3, "topics": []}\n  '
        result = _parse_haiku_text(text)
        assert result is not None
        assert result["has_observation"] is True


# ──────────────────────────────────────────────
# TestBuildPrompt
# ──────────────────────────────────────────────

class TestBuildPrompt:
    """Tests for _build_prompt()."""

    def test_includes_tool_name(self):
        """Prompt includes the tool name."""
        prompt = _build_prompt("jarvis_commit", {"op": "create"}, "result")
        assert "jarvis_commit" in prompt

    def test_includes_truncated_output(self):
        """Prompt truncates long output."""
        prompt = _build_prompt("tool", {}, "x" * 5000)
        assert "truncated" in prompt


# ──────────────────────────────────────────────
# TestCallHaikuApi
# ──────────────────────────────────────────────

class TestCallHaikuApi:
    """Tests for call_haiku_api() — Anthropic SDK backend."""

    def test_no_api_key(self):
        """Returns None when ANTHROPIC_API_KEY is not set."""
        with patch.dict(os.environ, {}, clear=True):
            env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
            with patch.dict(os.environ, env, clear=True):
                result = call_haiku_api("jarvis_commit", {}, "some output")
                assert result is None

    def test_no_anthropic_package(self):
        """Returns None when anthropic package is not installed."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch.dict("sys.modules", {"anthropic": None}):
                result = call_haiku_api("jarvis_commit", {}, "some output")
                assert result is None

    def test_successful_extraction(self):
        """Successful Haiku API call returns parsed observation."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock()]
        mock_response.content[0].text = json.dumps({
            "has_observation": True,
            "content": "The project uses JARVIS protocol for git commits",
            "importance_score": 0.6,
            "topics": ["architecture", "git"],
        })

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
                result = call_haiku_api("jarvis_commit", {"op": "create"}, "Committed entry")
                assert result is not None
                assert result["has_observation"] is True
                assert "JARVIS" in result["content"]
                assert result["importance_score"] == 0.6
                assert "architecture" in result["topics"]

    def test_invalid_json_response(self):
        """Returns None when Haiku returns non-JSON."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock()]
        mock_response.content[0].text = "This is not JSON"

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
                result = call_haiku_api("jarvis_commit", {}, "output")
                assert result is None

    def test_api_error_returns_none(self):
        """Returns None when API call raises an exception."""
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value.messages.create.side_effect = Exception("API error")

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
                result = call_haiku_api("jarvis_commit", {}, "output")
                assert result is None


# ──────────────────────────────────────────────
# TestCallHaikuCli
# ──────────────────────────────────────────────

class TestCallHaikuCli:
    """Tests for call_haiku_cli() — Claude CLI backend."""

    def test_no_claude_binary(self):
        """Returns None when claude binary is not found."""
        with patch("extract_observation.shutil.which", return_value=None):
            result = call_haiku_cli("jarvis_commit", {}, "some output")
            assert result is None

    def test_successful_extraction(self):
        """Successful CLI call returns parsed observation."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "has_observation": True,
            "content": "Observation from CLI",
            "importance_score": 0.5,
            "topics": ["test"],
        })

        with patch("extract_observation.shutil.which", return_value="/usr/local/bin/claude"):
            with patch("extract_observation.subprocess.run", return_value=mock_result) as mock_run:
                result = call_haiku_cli("jarvis_commit", {"op": "create"}, "output here")
                assert result is not None
                assert result["has_observation"] is True
                assert result["content"] == "Observation from CLI"
                # Verify correct CLI args
                mock_run.assert_called_once()
                args = mock_run.call_args
                assert args[0][0] == ["/usr/local/bin/claude", "-p", "--model", "haiku"]
                assert args[1]["timeout"] == 30

    def test_cli_nonzero_exit(self):
        """Returns None when CLI exits with non-zero code."""
        mock_result = MagicMock()
        mock_result.returncode = 1

        with patch("extract_observation.shutil.which", return_value="/usr/local/bin/claude"):
            with patch("extract_observation.subprocess.run", return_value=mock_result):
                result = call_haiku_cli("jarvis_commit", {}, "output")
                assert result is None

    def test_cli_timeout(self):
        """Returns None when CLI times out."""
        with patch("extract_observation.shutil.which", return_value="/usr/local/bin/claude"):
            with patch("extract_observation.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 30)):
                result = call_haiku_cli("jarvis_commit", {}, "output")
                assert result is None

    def test_cli_invalid_json(self):
        """Returns None when CLI returns non-JSON output."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "I couldn't parse that request."

        with patch("extract_observation.shutil.which", return_value="/usr/local/bin/claude"):
            with patch("extract_observation.subprocess.run", return_value=mock_result):
                result = call_haiku_cli("jarvis_commit", {}, "output")
                assert result is None

    def test_cli_exception(self):
        """Returns None when subprocess.run raises an exception."""
        with patch("extract_observation.shutil.which", return_value="/usr/local/bin/claude"):
            with patch("extract_observation.subprocess.run", side_effect=OSError("No such file")):
                result = call_haiku_cli("jarvis_commit", {}, "output")
                assert result is None


# ──────────────────────────────────────────────
# TestCallHaikuRouter
# ──────────────────────────────────────────────

class TestCallHaikuRouter:
    """Tests for call_haiku() — mode routing logic."""

    def test_background_api_mode_uses_api_only(self):
        """background-api mode only calls call_haiku_api."""
        with patch("extract_observation.call_haiku_api", return_value=None) as mock_api:
            with patch("extract_observation.call_haiku_cli") as mock_cli:
                call_haiku("tool", {}, "output", mode="background-api")
                mock_api.assert_called_once()
                mock_cli.assert_not_called()

    def test_background_cli_mode_uses_cli_only(self):
        """background-cli mode only calls call_haiku_cli."""
        with patch("extract_observation.call_haiku_api") as mock_api:
            with patch("extract_observation.call_haiku_cli", return_value=None) as mock_cli:
                call_haiku("tool", {}, "output", mode="background-cli")
                mock_api.assert_not_called()
                mock_cli.assert_called_once()

    def test_background_smart_tries_api_first(self):
        """Smart background mode tries API first, skips CLI if API succeeds."""
        api_result = {"has_observation": True, "content": "from API"}
        with patch("extract_observation.call_haiku_api", return_value=api_result) as mock_api:
            with patch("extract_observation.call_haiku_cli") as mock_cli:
                result = call_haiku("tool", {}, "output", mode="background")
                assert result == api_result
                mock_api.assert_called_once()
                mock_cli.assert_not_called()

    def test_background_smart_falls_back_to_cli(self):
        """Smart background mode falls back to CLI when API returns None."""
        cli_result = {"has_observation": True, "content": "from CLI"}
        with patch("extract_observation.call_haiku_api", return_value=None) as mock_api:
            with patch("extract_observation.call_haiku_cli", return_value=cli_result) as mock_cli:
                result = call_haiku("tool", {}, "output", mode="background")
                assert result == cli_result
                mock_api.assert_called_once()
                mock_cli.assert_called_once()

    def test_background_smart_both_fail(self):
        """Smart background mode returns None when both backends fail."""
        with patch("extract_observation.call_haiku_api", return_value=None):
            with patch("extract_observation.call_haiku_cli", return_value=None):
                result = call_haiku("tool", {}, "output", mode="background")
                assert result is None

    def test_default_mode_is_background(self):
        """Default mode is 'background' (smart fallback)."""
        with patch("extract_observation.call_haiku_api", return_value=None) as mock_api:
            with patch("extract_observation.call_haiku_cli", return_value=None) as mock_cli:
                call_haiku("tool", {}, "output")  # No mode arg
                mock_api.assert_called_once()
                mock_cli.assert_called_once()


# ──────────────────────────────────────────────
# TestStoreObservation
# ──────────────────────────────────────────────

class TestStoreObservation:
    """Tests for store_observation()."""

    def test_stores_observation(self, mock_config):
        """Successfully stores observation via tier2_write."""
        # Patch tier2_write where store_observation imports it from
        with patch("tools.tier2.tier2_write") as mock_write:
            mock_write.return_value = {
                "success": True,
                "id": "obs::1234567890",
                "content_type": "observation",
            }
            result = store_observation(
                content="Project uses monorepo structure",
                importance_score=0.6,
                topics=["architecture"],
                source_tool="jarvis_commit",
            )
            assert result["success"] is True
            mock_write.assert_called_once_with(
                content="Project uses monorepo structure",
                content_type="observation",
                importance_score=0.6,
                source="auto-extract:jarvis_commit",
                topics=["architecture"],
                skip_secret_scan=False,
            )

    def test_secret_scan_blocks_secrets(self, mock_config):
        """Secret scan rejects content with secrets."""
        with patch("tools.tier2.tier2_write") as mock_write:
            mock_write.return_value = {
                "success": False,
                "error": "Secret detected in content",
                "detections": [{"type": "api_key", "match": "sk-..."}],
            }
            result = store_observation(
                content="API key is sk-1234567890abcdef",
                importance_score=0.5,
                topics=["credentials"],
                source_tool="Bash",
            )
            assert result["success"] is False
            assert "Secret" in result["error"]
