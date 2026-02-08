"""Tests for extract_observation.py — Haiku extraction and storage."""
import json
import os
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
    call_haiku,
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
# TestCallHaiku
# ──────────────────────────────────────────────

class TestCallHaiku:
    """Tests for call_haiku()."""

    def test_no_api_key(self):
        """Returns None when ANTHROPIC_API_KEY is not set."""
        with patch.dict(os.environ, {}, clear=True):
            # Also remove from env if present
            env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
            with patch.dict(os.environ, env, clear=True):
                result = call_haiku("jarvis_commit", {}, "some output")
                assert result is None

    def test_no_anthropic_package(self):
        """Returns None when anthropic package is not installed."""
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch.dict("sys.modules", {"anthropic": None}):
                result = call_haiku("jarvis_commit", {}, "some output")
                assert result is None

    def test_successful_extraction(self):
        """Successful Haiku call returns parsed observation."""
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
                result = call_haiku("jarvis_commit", {"op": "create"}, "Committed entry")
                assert result is not None
                assert result["has_observation"] is True
                assert "JARVIS" in result["content"]
                assert result["importance_score"] == 0.6
                assert "architecture" in result["topics"]

    def test_no_observation_found(self):
        """Returns dict with has_observation=False for routine results."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock()]
        mock_response.content[0].text = json.dumps({
            "has_observation": False,
            "content": "",
            "importance_score": 0.0,
            "topics": [],
        })

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
                result = call_haiku("jarvis_status", {}, "Clean working tree")
                assert result is not None
                assert result["has_observation"] is False

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
                result = call_haiku("jarvis_commit", {}, "output")
                assert result is None

    def test_json_in_code_block(self):
        """Handles Haiku wrapping JSON in markdown code blocks."""
        mock_response = MagicMock()
        mock_response.content = [MagicMock()]
        mock_response.content[0].text = '```json\n{"has_observation": true, "content": "test", "importance_score": 0.5, "topics": []}\n```'

        mock_client = MagicMock()
        mock_client.messages.create.return_value = mock_response

        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value = mock_client

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
                result = call_haiku("jarvis_commit", {}, "output here")
                assert result is not None
                assert result["has_observation"] is True

    def test_api_error_returns_none(self):
        """Returns None when API call raises an exception."""
        mock_anthropic = MagicMock()
        mock_anthropic.Anthropic.return_value.messages.create.side_effect = Exception("API error")

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test-key"}):
            with patch.dict("sys.modules", {"anthropic": mock_anthropic}):
                result = call_haiku("jarvis_commit", {}, "output")
                assert result is None


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
