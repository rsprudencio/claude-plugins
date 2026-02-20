"""Tests for GeminiProvider — CLI + API paths, availability, command structure."""

import json
import os
import sys
import pytest
from unittest.mock import patch, Mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "hooks-handlers"))

from providers.gemini import GeminiProvider, _GEMINI_API_KEY_ENV, _GEMINI_HOST
from providers.base import ProviderAdapter, ProviderResult


@pytest.fixture
def provider():
    return GeminiProvider()


# ---------------------------------------------------------------------------
# TestProtocolCompliance
# ---------------------------------------------------------------------------


class TestProtocolCompliance:
    def test_satisfies_protocol(self, provider):
        assert isinstance(provider, ProviderAdapter)

    def test_name(self, provider):
        assert provider.name == "gemini"


# ---------------------------------------------------------------------------
# TestAvailability
# ---------------------------------------------------------------------------


class TestAvailability:
    @patch("providers.gemini.which", return_value="/usr/local/bin/gemini")
    def test_available_when_binary_found(self, mock_which, provider):
        available, path = provider.is_available()
        assert available is True
        assert path == "/usr/local/bin/gemini"

    @patch("providers.gemini.which", return_value=None)
    def test_unavailable_when_binary_missing(self, mock_which, provider):
        available, path = provider.is_available()
        assert available is False
        assert path is None

    @patch.dict(os.environ, {_GEMINI_API_KEY_ENV: "test-key"}, clear=False)
    def test_has_api_key(self, provider):
        assert provider.has_api_key() is True

    @patch.dict(os.environ, {}, clear=False)
    def test_no_api_key(self, provider):
        os.environ.pop(_GEMINI_API_KEY_ENV, None)
        assert provider.has_api_key() is False

    def test_availability_error_message(self, provider):
        msg = provider.availability_error()
        assert "gemini" in msg
        assert "not found" in msg
        assert _GEMINI_API_KEY_ENV in msg


# ---------------------------------------------------------------------------
# TestBuildCommand
# ---------------------------------------------------------------------------


class TestBuildCommand:
    def test_basic_structure(self, provider):
        cmd = provider.build_command(
            binary="/usr/bin/gemini",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        assert cmd[0] == "/usr/bin/gemini"
        # Gemini uses default command (no 'exec' subcommand)
        assert "exec" not in cmd
        # Uses -p "" to trigger headless mode (stdin provides prompt)
        assert "-p" in cmd
        idx = cmd.index("-p")
        assert cmd[idx + 1] == ""

    def test_approval_mode_plan(self, provider):
        cmd = provider.build_command(
            binary="gemini",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        assert "--approval-mode" in cmd
        idx = cmd.index("--approval-mode")
        assert cmd[idx + 1] == "plan"

    def test_json_output_format(self, provider):
        cmd = provider.build_command(
            binary="gemini",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        assert "-o" in cmd
        idx = cmd.index("-o")
        assert cmd[idx + 1] == "json"

    def test_no_codex_specific_flags(self, provider):
        """Gemini CLI doesn't support Codex-specific flags."""
        cmd = provider.build_command(
            binary="gemini",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        codex_flags = ["--color", "--output-last-message", "--output-schema",
                       "--ephemeral", "--skip-git-repo-check", "--cd", "--sandbox"]
        for flag in codex_flags:
            assert flag not in cmd, f"Codex-specific flag {flag} found in Gemini command"

    def test_model_override(self, provider):
        cmd = provider.build_command(
            binary="gemini",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
            model="gemini-pro",
        )
        assert "-m" in cmd
        idx = cmd.index("-m")
        assert cmd[idx + 1] == "gemini-pro"

    def test_no_model_by_default(self, provider):
        cmd = provider.build_command(
            binary="gemini",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        assert "-m" not in cmd


# ---------------------------------------------------------------------------
# TestReadResponse
# ---------------------------------------------------------------------------


class TestReadResponse:
    def test_extracts_response_field(self, provider):
        """Gemini -o json puts model text in top-level 'response' field."""
        stdout = json.dumps({
            "session_id": "abc-123",
            "response": '{"status": "approved"}',
            "stats": {},
        })
        result = provider.read_response("/nonexistent/path", stdout)
        assert result == '{"status": "approved"}'

    def test_extracts_multiline_response(self, provider):
        stdout = json.dumps({
            "session_id": "abc-123",
            "response": "line1\nline2\nline3",
            "stats": {},
        })
        result = provider.read_response("/nonexistent/path", stdout)
        assert "line1" in result
        assert "line3" in result

    def test_falls_back_to_raw_stdout(self, provider):
        """When stdout isn't parseable JSON, return raw text."""
        result = provider.read_response("/nonexistent/path", "plain text output")
        assert result == "plain text output"

    def test_empty_stdout(self, provider):
        result = provider.read_response("/nonexistent/path", "")
        assert result == ""

    def test_no_response_field_returns_raw(self, provider):
        """If JSON lacks 'response' key, return raw stdout."""
        stdout = json.dumps({"error": "something went wrong"})
        result = provider.read_response("/nonexistent/path", stdout)
        assert result == stdout


# ---------------------------------------------------------------------------
# TestInvokeApi
# ---------------------------------------------------------------------------


class TestInvokeApi:
    @patch.dict(os.environ, {_GEMINI_API_KEY_ENV: "test-key"}, clear=False)
    @patch("providers.gemini._gemini_api_call")
    def test_api_call_made(self, mock_call, provider):
        mock_call.return_value = ProviderResult(raw_text='{"status": "ok"}')

        result = provider._invoke_api("prompt", timeout=60)

        mock_call.assert_called_once()
        assert result.raw_text == '{"status": "ok"}'

    @patch.dict(os.environ, {_GEMINI_API_KEY_ENV: "test-key"}, clear=False)
    @patch("providers.gemini._gemini_api_call")
    def test_api_model_in_path(self, mock_call, provider):
        mock_call.return_value = ProviderResult(raw_text="ok")

        provider._invoke_api("prompt", timeout=60, model="gemini-pro")

        call_kwargs = mock_call.call_args.kwargs
        assert "gemini-pro" in call_kwargs["path"]

    @patch.dict(os.environ, {}, clear=False)
    def test_api_no_key_returns_error(self, provider):
        os.environ.pop(_GEMINI_API_KEY_ENV, None)
        result = provider._invoke_api("prompt", timeout=60)
        assert result.error is not None
        assert "key" in result.error.lower()


# ---------------------------------------------------------------------------
# TestInvokeResolutionOrder
# ---------------------------------------------------------------------------


class TestInvokeResolutionOrder:
    @patch("providers.gemini.invoke_cli")
    def test_cli_first_when_available(self, mock_cli, provider):
        mock_cli.return_value = ProviderResult(raw_text="cli output")

        with patch.object(provider, "is_available", return_value=(True, "/usr/bin/gemini")):
            result = provider.invoke("prompt", {}, "/tmp", 60)

        mock_cli.assert_called_once()
        assert result.raw_text == "cli output"

    @patch("providers.gemini.invoke_cli")
    @patch.dict(os.environ, {_GEMINI_API_KEY_ENV: "test-key"}, clear=False)
    @patch("providers.gemini._gemini_api_call")
    def test_api_fallback_when_cli_fails(self, mock_api, mock_cli, provider):
        mock_cli.return_value = ProviderResult(error="CLI crashed")
        mock_api.return_value = ProviderResult(raw_text="api output")

        with patch.object(provider, "is_available", return_value=(True, "/usr/bin/gemini")):
            result = provider.invoke("prompt", {}, "/tmp", 60)

        mock_cli.assert_called_once()
        mock_api.assert_called_once()
        assert result.raw_text == "api output"

    @patch.dict(os.environ, {_GEMINI_API_KEY_ENV: "test-key"}, clear=False)
    @patch("providers.gemini._gemini_api_call")
    def test_api_when_cli_unavailable(self, mock_api, provider):
        mock_api.return_value = ProviderResult(raw_text="api response")

        with patch.object(provider, "is_available", return_value=(False, None)):
            result = provider.invoke("prompt", {}, "/tmp", 60)

        mock_api.assert_called_once()

    @patch.dict(os.environ, {}, clear=False)
    def test_error_when_neither_available(self, provider):
        os.environ.pop(_GEMINI_API_KEY_ENV, None)

        with patch.object(provider, "is_available", return_value=(False, None)):
            result = provider.invoke("prompt", {}, "/tmp", 60)

        assert result.error is not None


# ---------------------------------------------------------------------------
# TestAuthRedaction
# ---------------------------------------------------------------------------


class TestAuthRedaction:
    def test_error_never_contains_actual_key(self, provider):
        msg = provider.availability_error()
        assert "test-key" not in msg
        assert _GEMINI_API_KEY_ENV in msg

    @patch.dict(os.environ, {_GEMINI_API_KEY_ENV: "super-secret-key"}, clear=False)
    @patch("providers.gemini._gemini_api_call")
    def test_api_error_never_contains_key(self, mock_call, provider):
        mock_call.return_value = ProviderResult(error="API returned HTTP 403")

        result = provider._invoke_api("prompt", timeout=60)
        assert "super-secret" not in (result.error or "")
