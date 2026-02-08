"""Tests for auto_extract_config.py — health checks for Stop hook extraction."""
import os
from unittest.mock import patch

import pytest

from tools.auto_extract_config import check_prerequisites
from tools.config import get_auto_extract_config


# ──────────────────────────────────────────────
# TestGetAutoExtractConfig
# ──────────────────────────────────────────────


class TestGetAutoExtractConfig:
    """Tests for get_auto_extract_config() defaults and overrides."""

    @patch("tools.config.get_config")
    def test_defaults(self, mock_get_config):
        """Default config values are set correctly."""
        mock_get_config.return_value = {}
        config = get_auto_extract_config()

        assert config["mode"] == "background"
        assert config["min_turn_chars"] == 200
        assert config["cooldown_seconds"] == 120
        assert config["max_transcript_lines"] == 100

    @patch("tools.config.get_config")
    def test_mode_override(self, mock_get_config):
        """User can override mode."""
        mock_get_config.return_value = {
            "memory": {
                "auto_extract": {
                    "mode": "background-api",
                }
            }
        }
        config = get_auto_extract_config()
        assert config["mode"] == "background-api"

    @patch("tools.config.get_config")
    def test_threshold_overrides(self, mock_get_config):
        """User can override thresholds."""
        mock_get_config.return_value = {
            "memory": {
                "auto_extract": {
                    "min_turn_chars": 500,
                    "cooldown_seconds": 60,
                    "max_transcript_lines": 200,
                }
            }
        }
        config = get_auto_extract_config()
        assert config["min_turn_chars"] == 500
        assert config["cooldown_seconds"] == 60
        assert config["max_transcript_lines"] == 200

    @patch("tools.config.get_config")
    def test_partial_overrides(self, mock_get_config):
        """Partial overrides merge with defaults."""
        mock_get_config.return_value = {
            "memory": {
                "auto_extract": {
                    "mode": "disabled",
                    "min_turn_chars": 300,
                }
            }
        }
        config = get_auto_extract_config()
        assert config["mode"] == "disabled"
        assert config["min_turn_chars"] == 300
        assert config["cooldown_seconds"] == 120  # Default
        assert config["max_transcript_lines"] == 100  # Default

    @patch("tools.config.get_config")
    def test_no_inline_mode(self, mock_get_config):
        """Inline mode is not a valid mode (removed in v1.12.0)."""
        # This test verifies that inline is NOT in the valid modes list
        # check_prerequisites will report it as invalid
        mock_get_config.return_value = {
            "memory": {
                "auto_extract": {
                    "mode": "inline",
                }
            }
        }
        config = get_auto_extract_config()
        result = check_prerequisites(config)

        assert result["healthy"] is False
        assert result["mode"] == "inline"
        assert any("Unknown mode" in issue for issue in result["issues"])


# ──────────────────────────────────────────────
# TestCheckPrerequisites
# ──────────────────────────────────────────────


class TestCheckPrerequisites:
    """Tests for check_prerequisites() health checks."""

    def test_disabled_mode_always_healthy(self):
        """Disabled mode is always healthy (no extraction)."""
        config = {"mode": "disabled"}
        result = check_prerequisites(config)

        assert result["healthy"] is True
        assert result["mode"] == "disabled"
        assert result["status"] == "Auto-Extract is disabled"
        assert result["issues"] == []

    def test_disabled_mode_details(self):
        """Disabled mode details include config fields."""
        config = {
            "mode": "disabled",
            "min_turn_chars": 300,
            "cooldown_seconds": 60,
            "max_transcript_lines": 50,
        }
        result = check_prerequisites(config)

        assert result["details"]["mode"] == "disabled"
        assert result["details"]["min_turn_chars"] == 300
        assert result["details"]["cooldown_seconds"] == 60
        assert result["details"]["max_transcript_lines"] == 50

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=True)
    @patch("tools.auto_extract_config.importlib.util.find_spec")
    def test_background_api_healthy(self, mock_find_spec):
        """background-api mode is healthy with API key and anthropic package."""
        mock_find_spec.return_value = True  # anthropic package exists
        config = {"mode": "background-api"}
        result = check_prerequisites(config)

        assert result["healthy"] is True
        assert result["mode"] == "background-api"
        assert "Anthropic SDK" in result["status"]
        assert result["issues"] == []

    @patch.dict(os.environ, {}, clear=True)
    @patch("tools.auto_extract_config.importlib.util.find_spec")
    def test_background_api_missing_key(self, mock_find_spec):
        """background-api mode unhealthy without API key."""
        mock_find_spec.return_value = True
        config = {"mode": "background-api"}
        result = check_prerequisites(config)

        assert result["healthy"] is False
        assert result["mode"] == "background-api"
        assert any("ANTHROPIC_API_KEY not found" in issue for issue in result["issues"])

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=True)
    @patch("tools.auto_extract_config.importlib.util.find_spec")
    def test_background_api_missing_package(self, mock_find_spec):
        """background-api mode unhealthy without anthropic package."""
        mock_find_spec.return_value = None  # Package not found
        config = {"mode": "background-api"}
        result = check_prerequisites(config)

        assert result["healthy"] is False
        assert result["mode"] == "background-api"
        assert any("anthropic' package not installed" in issue for issue in result["issues"])

    @patch("tools.auto_extract_config.shutil.which")
    def test_background_cli_healthy(self, mock_which):
        """background-cli mode is healthy with claude binary."""
        mock_which.return_value = "/usr/local/bin/claude"
        config = {"mode": "background-cli"}
        result = check_prerequisites(config)

        assert result["healthy"] is True
        assert result["mode"] == "background-cli"
        assert "Claude CLI" in result["status"]
        assert result["issues"] == []

    @patch("tools.auto_extract_config.shutil.which")
    def test_background_cli_missing_binary(self, mock_which):
        """background-cli mode unhealthy without claude binary."""
        mock_which.return_value = None
        config = {"mode": "background-cli"}
        result = check_prerequisites(config)

        assert result["healthy"] is False
        assert result["mode"] == "background-cli"
        assert any("'claude' binary not found" in issue for issue in result["issues"])

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=True)
    @patch("tools.auto_extract_config.importlib.util.find_spec")
    @patch("tools.auto_extract_config.shutil.which")
    def test_background_smart_both_available(self, mock_which, mock_find_spec):
        """Smart background mode is healthy with both API and CLI."""
        mock_find_spec.return_value = True
        mock_which.return_value = "/usr/local/bin/claude"
        config = {"mode": "background"}
        result = check_prerequisites(config)

        assert result["healthy"] is True
        assert result["mode"] == "background"
        assert "smart fallback" in result["status"]
        assert result["issues"] == []
        assert result["details"]["available_backends"] == ["API", "CLI"]

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}, clear=True)
    @patch("tools.auto_extract_config.importlib.util.find_spec")
    @patch("tools.auto_extract_config.shutil.which")
    def test_background_smart_api_only(self, mock_which, mock_find_spec):
        """Smart background mode is healthy with API only."""
        mock_find_spec.return_value = True
        mock_which.return_value = None  # No CLI
        config = {"mode": "background"}
        result = check_prerequisites(config)

        assert result["healthy"] is True
        assert result["details"]["available_backends"] == ["API"]

    @patch.dict(os.environ, {}, clear=True)
    @patch("tools.auto_extract_config.importlib.util.find_spec")
    @patch("tools.auto_extract_config.shutil.which")
    def test_background_smart_cli_only(self, mock_which, mock_find_spec):
        """Smart background mode is healthy with CLI only."""
        mock_find_spec.return_value = None  # No API package
        mock_which.return_value = "/usr/local/bin/claude"
        config = {"mode": "background"}
        result = check_prerequisites(config)

        assert result["healthy"] is True
        assert result["details"]["available_backends"] == ["CLI"]

    @patch.dict(os.environ, {}, clear=True)
    @patch("tools.auto_extract_config.importlib.util.find_spec")
    @patch("tools.auto_extract_config.shutil.which")
    def test_background_smart_no_backends(self, mock_which, mock_find_spec):
        """Smart background mode unhealthy with no backends available."""
        mock_find_spec.return_value = None
        mock_which.return_value = None
        config = {"mode": "background"}
        result = check_prerequisites(config)

        assert result["healthy"] is False
        assert any("No extraction backend available" in issue for issue in result["issues"])

    def test_unknown_mode(self):
        """Unknown mode is reported as invalid."""
        config = {"mode": "inline"}  # inline was valid in v1.10-1.11, removed in v1.12.0
        result = check_prerequisites(config)

        assert result["healthy"] is False
        assert result["mode"] == "inline"
        assert result["status"] == "Auto-Extract has invalid configuration"
        assert any("Unknown mode 'inline'" in issue for issue in result["issues"])
        assert "disabled, background, background-api, background-cli" in result["issues"][0]

    def test_details_include_backends(self):
        """Details dict includes backend availability."""
        config = {"mode": "background"}
        result = check_prerequisites(config)

        assert "has_api_key" in result["details"]
        assert "has_anthropic_package" in result["details"]
        assert "has_claude_cli" in result["details"]

    def test_details_include_thresholds(self):
        """Details dict includes config thresholds."""
        config = {
            "mode": "disabled",
            "min_turn_chars": 250,
            "cooldown_seconds": 90,
            "max_transcript_lines": 150,
        }
        result = check_prerequisites(config)

        assert result["details"]["min_turn_chars"] == 250
        assert result["details"]["cooldown_seconds"] == 90
        assert result["details"]["max_transcript_lines"] == 150
