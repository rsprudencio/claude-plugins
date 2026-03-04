"""Tests for AnthropicProvider adapter."""

import json
import os
import sys
import pytest
from unittest.mock import patch, Mock, MagicMock
from pathlib import Path

# Import from standalone hooks-handlers location
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "hooks-handlers"))

from providers.anthropic import (
    AnthropicProvider,
    _ANTHROPIC_HOST,
    _ANTHROPIC_PATH,
    _ANTHROPIC_API_VERSION,
    _ANTHROPIC_DEFAULT_MODEL,
    _ANTHROPIC_API_KEY_ENV,
    _ANTHROPIC_MAX_TOKENS,
)
from providers.base import ProviderResult


class TestAnthropicProviderAvailability:
    """Tests for is_available() and has_api_key()."""

    def test_is_available_found(self):
        provider = AnthropicProvider()
        with patch("providers.anthropic.which", return_value="/usr/local/bin/claude"):
            available, path = provider.is_available()
            assert available is True
            assert path == "/usr/local/bin/claude"

    def test_is_available_not_found(self):
        provider = AnthropicProvider()
        with patch("providers.anthropic.which", return_value=None):
            available, path = provider.is_available()
            assert available is False
            assert path is None

    def test_has_api_key_set(self):
        provider = AnthropicProvider()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}):
            assert provider.has_api_key() is True

    def test_has_api_key_empty(self):
        provider = AnthropicProvider()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=False):
            assert provider.has_api_key() is False

    def test_has_api_key_missing(self):
        provider = AnthropicProvider()
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            assert provider.has_api_key() is False


class TestAnthropicProviderBuildCommand:
    """Tests for build_command()."""

    def test_default_command(self):
        provider = AnthropicProvider()
        cmd = provider.build_command(
            binary="/usr/local/bin/claude",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/home/user/project",
        )
        assert cmd[0] == "/usr/local/bin/claude"
        assert "-p" in cmd
        assert "--model" in cmd
        assert "haiku" in cmd
        assert "--no-session-persistence" in cmd

    def test_custom_model(self):
        provider = AnthropicProvider()
        cmd = provider.build_command(
            binary="/usr/local/bin/claude",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/home/user/project",
            model="sonnet",
        )
        assert "sonnet" in cmd


class TestAnthropicProviderReadResponse:
    """Tests for read_response()."""

    def test_reads_from_stdout(self):
        provider = AnthropicProvider()
        result = provider.read_response("/tmp/nonexistent.md", '  {"key": "value"}  ')
        assert result == '{"key": "value"}'

    def test_empty_stdout(self):
        provider = AnthropicProvider()
        result = provider.read_response("/tmp/nonexistent.md", "")
        assert result == ""


class TestAnthropicProviderAvailabilityError:
    """Tests for availability_error()."""

    def test_error_message(self):
        provider = AnthropicProvider()
        msg = provider.availability_error()
        assert "ANTHROPIC_API_KEY" in msg
        assert "claude" in msg
        assert "anthropic" in msg


class TestAnthropicProviderInvokeApi:
    """Tests for _invoke_api()."""

    def test_api_success(self):
        provider = AnthropicProvider()
        response_data = {
            "content": [{"type": "text", "text": '{"has_observation": true}'}],
            "usage": {"input_tokens": 100, "output_tokens": 50},
        }
        mock_resp = Mock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(response_data).encode("utf-8")

        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_resp

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with patch("http.client.HTTPSConnection", return_value=mock_conn):
                result = provider._invoke_api("test prompt", timeout=30)

        assert result.error is None
        assert '{"has_observation": true}' in result.raw_text
        assert result._usage["input_tokens"] == 100
        assert result._usage["output_tokens"] == 50

    def test_api_no_key(self):
        provider = AnthropicProvider()
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            result = provider._invoke_api("test prompt", timeout=30)
        assert result.error is not None

    def test_api_http_error(self):
        provider = AnthropicProvider()
        mock_resp = Mock()
        mock_resp.status = 429
        mock_resp.read.return_value = b"rate limited"

        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_resp

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with patch("http.client.HTTPSConnection", return_value=mock_conn):
                result = provider._invoke_api("test prompt", timeout=30)

        assert result.error is not None
        assert "429" in result.error

    def test_api_connection_error(self):
        provider = AnthropicProvider()
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with patch("http.client.HTTPSConnection", side_effect=OSError("conn failed")):
                result = provider._invoke_api("test prompt", timeout=30)
        assert result.error is not None
        assert "connection" in result.error.lower()

    def test_api_custom_model(self):
        provider = AnthropicProvider()
        response_data = {
            "content": [{"type": "text", "text": "response"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }
        mock_resp = Mock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(response_data).encode("utf-8")

        mock_conn = MagicMock()
        mock_conn.getresponse.return_value = mock_resp

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with patch("http.client.HTTPSConnection", return_value=mock_conn):
                result = provider._invoke_api("test", timeout=30, model="claude-sonnet-4-5")

        # Verify the model was sent in the payload
        call_args = mock_conn.request.call_args
        payload = json.loads(call_args[1]["body"] if "body" in call_args[1] else call_args[0][2])
        assert payload["model"] == "claude-sonnet-4-5"


class TestAnthropicProviderInvokeCli:
    """Tests for _invoke_cli()."""

    def test_cli_success(self):
        provider = AnthropicProvider()
        mock_result = Mock()
        mock_result.returncode = 0
        mock_result.stdout = '{"has_observation": true}'
        mock_result.stderr = ""

        with patch("providers.anthropic.which", return_value="/usr/local/bin/claude"):
            with patch("subprocess.run", return_value=mock_result) as mock_run:
                result = provider._invoke_cli("test prompt", timeout=30)

        assert result.error is None
        assert result.raw_text == '{"has_observation": true}'
        # Verify JARVIS_EXTRACTING env var is set
        env = mock_run.call_args[1]["env"]
        assert env["JARVIS_EXTRACTING"] == "1"

    def test_cli_not_found(self):
        provider = AnthropicProvider()
        with patch("providers.anthropic.which", return_value=None):
            result = provider._invoke_cli("test prompt", timeout=30)
        assert result.error is not None
        assert "not found" in result.error

    def test_cli_timeout(self):
        provider = AnthropicProvider()
        import subprocess
        with patch("providers.anthropic.which", return_value="/usr/local/bin/claude"):
            with patch("subprocess.run", side_effect=subprocess.TimeoutExpired("cmd", 30)):
                result = provider._invoke_cli("test prompt", timeout=30)
        assert result.timed_out is True

    def test_cli_nonzero_exit(self):
        provider = AnthropicProvider()
        mock_result = Mock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "some error"

        with patch("providers.anthropic.which", return_value="/usr/local/bin/claude"):
            with patch("subprocess.run", return_value=mock_result):
                result = provider._invoke_cli("test prompt", timeout=30)
        assert result.error is not None


class TestAnthropicProviderInvoke:
    """Tests for invoke() resolution order."""

    def test_api_first_when_key_available(self):
        """Anthropic provider should try API first (unlike Codex/Gemini)."""
        provider = AnthropicProvider()
        api_result = ProviderResult(raw_text="api response")
        api_result._usage = {"input_tokens": 10, "output_tokens": 5}

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with patch.object(provider, "_invoke_api", return_value=api_result) as mock_api:
                with patch.object(provider, "_invoke_cli") as mock_cli:
                    result = provider.invoke("prompt", {}, "/tmp", 30)

        mock_api.assert_called_once()
        mock_cli.assert_not_called()
        assert result.raw_text == "api response"

    def test_cli_fallback_when_no_key(self):
        """Should fall back to CLI when API key not set."""
        provider = AnthropicProvider()
        cli_result = ProviderResult(raw_text="cli response")
        cli_result._usage = {"input_tokens": 10, "output_tokens": 5}

        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            with patch.object(provider, "is_available", return_value=(True, "/usr/bin/claude")):
                with patch.object(provider, "_invoke_cli", return_value=cli_result) as mock_cli:
                    result = provider.invoke("prompt", {}, "/tmp", 30)

        mock_cli.assert_called_once()
        assert result.raw_text == "cli response"

    def test_cli_fallback_when_api_fails(self):
        """Should fall back to CLI when API returns error."""
        provider = AnthropicProvider()
        api_result = ProviderResult(error="API error")
        cli_result = ProviderResult(raw_text="cli response")
        cli_result._usage = {"input_tokens": 10, "output_tokens": 5}

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}):
            with patch.object(provider, "_invoke_api", return_value=api_result):
                with patch.object(provider, "is_available", return_value=(True, "/usr/bin/claude")):
                    with patch.object(provider, "_invoke_cli", return_value=cli_result):
                        result = provider.invoke("prompt", {}, "/tmp", 30)

        assert result.raw_text == "cli response"

    def test_error_when_nothing_available(self):
        """Should return error when neither API nor CLI available."""
        provider = AnthropicProvider()
        env = os.environ.copy()
        env.pop("ANTHROPIC_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            with patch.object(provider, "is_available", return_value=(False, None)):
                result = provider.invoke("prompt", {}, "/tmp", 30)
        assert result.error is not None


class TestAnthropicProviderInRegistry:
    """Tests for provider registration."""

    def test_registered_in_registry(self):
        from providers._registry import REGISTRY
        assert "anthropic" in REGISTRY
        assert isinstance(REGISTRY["anthropic"], AnthropicProvider)

    def test_resolve_by_name(self):
        from providers._registry import resolve_provider
        provider = resolve_provider("anthropic")
        assert provider is not None
        assert provider.name == "anthropic"
