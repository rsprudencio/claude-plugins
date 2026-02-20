"""Tests for CodexProvider API fallback (OpenAI Chat Completions)."""

import json
import os
import sys
import pytest
from unittest.mock import patch, Mock, MagicMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "hooks-handlers"))

from providers.codex import CodexProvider, _OPENAI_API_KEY_ENV, _OPENAI_HOST, _OPENAI_PATH
from providers.base import ProviderResult


@pytest.fixture
def provider():
    return CodexProvider()


# ---------------------------------------------------------------------------
# TestHasApiKey
# ---------------------------------------------------------------------------


class TestHasApiKey:
    @patch.dict(os.environ, {_OPENAI_API_KEY_ENV: "sk-test-key"}, clear=False)
    def test_has_key(self, provider):
        assert provider.has_api_key() is True

    @patch.dict(os.environ, {}, clear=False)
    def test_no_key(self, provider):
        os.environ.pop(_OPENAI_API_KEY_ENV, None)
        assert provider.has_api_key() is False

    @patch.dict(os.environ, {_OPENAI_API_KEY_ENV: "  "}, clear=False)
    def test_whitespace_key(self, provider):
        assert provider.has_api_key() is False


# ---------------------------------------------------------------------------
# TestInvokeApi
# ---------------------------------------------------------------------------


class TestInvokeApi:
    @patch.dict(os.environ, {_OPENAI_API_KEY_ENV: "sk-test-key"}, clear=False)
    @patch("providers.codex.invoke_api")
    def test_api_call_made_with_correct_params(self, mock_invoke_api, provider):
        mock_invoke_api.return_value = ProviderResult(raw_text='{"status": "approved"}')

        result = provider._invoke_api("test prompt", timeout=60)

        mock_invoke_api.assert_called_once()
        call_kwargs = mock_invoke_api.call_args
        assert call_kwargs.kwargs["host"] == _OPENAI_HOST
        assert call_kwargs.kwargs["path"] == _OPENAI_PATH
        assert call_kwargs.kwargs["api_key"] == "sk-test-key"
        assert call_kwargs.kwargs["timeout"] == 60

    @patch.dict(os.environ, {_OPENAI_API_KEY_ENV: "sk-test-key"}, clear=False)
    @patch("providers.codex.invoke_api")
    def test_api_model_override(self, mock_invoke_api, provider):
        mock_invoke_api.return_value = ProviderResult(raw_text="ok")

        provider._invoke_api("prompt", timeout=60, model="gpt-4-turbo")

        payload = mock_invoke_api.call_args.kwargs["payload"]
        assert payload["model"] == "gpt-4-turbo"

    @patch.dict(os.environ, {_OPENAI_API_KEY_ENV: "sk-test-key"}, clear=False)
    @patch("providers.codex.invoke_api")
    def test_api_default_model(self, mock_invoke_api, provider):
        mock_invoke_api.return_value = ProviderResult(raw_text="ok")

        provider._invoke_api("prompt", timeout=60)

        payload = mock_invoke_api.call_args.kwargs["payload"]
        assert payload["model"] == "gpt-4o"

    @patch.dict(os.environ, {}, clear=False)
    def test_api_no_key_returns_error(self, provider):
        os.environ.pop(_OPENAI_API_KEY_ENV, None)
        result = provider._invoke_api("prompt", timeout=60)
        assert result.error is not None
        assert "key" in result.error.lower()


# ---------------------------------------------------------------------------
# TestInvokeResolutionOrder
# ---------------------------------------------------------------------------


class TestInvokeResolutionOrder:
    @patch("providers.codex.invoke_cli")
    def test_cli_first_when_available(self, mock_cli, provider):
        mock_cli.return_value = ProviderResult(raw_text='{"status": "ok"}')

        with patch.object(provider, "is_available", return_value=(True, "/usr/bin/codex")):
            result = provider.invoke("prompt", {}, "/tmp", 60)

        mock_cli.assert_called_once()
        assert result.raw_text == '{"status": "ok"}'

    @patch("providers.codex.invoke_cli")
    @patch.dict(os.environ, {_OPENAI_API_KEY_ENV: "sk-test"}, clear=False)
    @patch("providers.codex.invoke_api")
    def test_api_fallback_when_cli_fails(self, mock_api, mock_cli, provider):
        mock_cli.return_value = ProviderResult(error="CLI crashed")
        mock_api.return_value = ProviderResult(raw_text='{"status": "ok"}')

        with patch.object(provider, "is_available", return_value=(True, "/usr/bin/codex")):
            result = provider.invoke("prompt", {}, "/tmp", 60)

        mock_cli.assert_called_once()
        mock_api.assert_called_once()
        assert result.raw_text == '{"status": "ok"}'

    @patch.dict(os.environ, {_OPENAI_API_KEY_ENV: "sk-test"}, clear=False)
    @patch("providers.codex.invoke_api")
    def test_api_used_when_cli_unavailable(self, mock_api, provider):
        mock_api.return_value = ProviderResult(raw_text="api response")

        with patch.object(provider, "is_available", return_value=(False, None)):
            result = provider.invoke("prompt", {}, "/tmp", 60)

        mock_api.assert_called_once()
        assert result.raw_text == "api response"

    @patch.dict(os.environ, {}, clear=False)
    def test_error_when_neither_available(self, provider):
        os.environ.pop(_OPENAI_API_KEY_ENV, None)

        with patch.object(provider, "is_available", return_value=(False, None)):
            result = provider.invoke("prompt", {}, "/tmp", 60)

        assert result.error is not None
        assert "not found" in result.error.lower()

    @patch("providers.codex.invoke_cli")
    def test_cli_success_skips_api(self, mock_cli, provider):
        """When CLI succeeds, API should never be called."""
        mock_cli.return_value = ProviderResult(raw_text="cli output")

        with patch.object(provider, "is_available", return_value=(True, "/usr/bin/codex")):
            with patch.object(provider, "_invoke_api") as mock_api:
                result = provider.invoke("prompt", {}, "/tmp", 60)

        mock_api.assert_not_called()


# ---------------------------------------------------------------------------
# TestAuthRedaction
# ---------------------------------------------------------------------------


class TestAuthRedaction:
    def test_availability_error_never_contains_key(self, provider):
        """Error message must not contain any actual API key value."""
        msg = provider.availability_error()
        assert "sk-" not in msg
        assert _OPENAI_API_KEY_ENV in msg  # references the env var NAME, not value

    @patch.dict(os.environ, {_OPENAI_API_KEY_ENV: "sk-secret-key-12345"}, clear=False)
    @patch("providers.codex.invoke_api")
    def test_api_error_never_contains_key(self, mock_api, provider):
        mock_api.return_value = ProviderResult(error="API returned HTTP 401")

        result = provider._invoke_api("prompt", timeout=60)
        assert "sk-secret" not in (result.error or "")
        assert "12345" not in (result.error or "")
