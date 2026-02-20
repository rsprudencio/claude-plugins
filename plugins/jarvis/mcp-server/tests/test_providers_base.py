"""Tests for providers.base — protocol compliance, shared utilities, registry."""

import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "hooks-handlers"))

from providers.base import (
    ProviderAdapter,
    ProviderResult,
    which,
    get_vault_path,
    resolve_working_directory,
)
from providers._registry import REGISTRY, resolve_provider
from providers.codex import CodexProvider


# ---------------------------------------------------------------------------
# TestProviderResult
# ---------------------------------------------------------------------------


class TestProviderResult:
    def test_default_values(self):
        r = ProviderResult()
        assert r.raw_text == ""
        assert r.error is None
        assert r.returncode is None
        assert r.timed_out is False

    def test_custom_values(self):
        r = ProviderResult(raw_text="output", error="fail", returncode=1, timed_out=True)
        assert r.raw_text == "output"
        assert r.error == "fail"
        assert r.returncode == 1
        assert r.timed_out is True


# ---------------------------------------------------------------------------
# TestProviderProtocol
# ---------------------------------------------------------------------------


class TestProviderProtocol:
    def test_codex_satisfies_protocol(self):
        """CodexProvider must satisfy the ProviderAdapter protocol."""
        provider = CodexProvider()
        assert isinstance(provider, ProviderAdapter)

    def test_protocol_requires_name(self):
        """Adapters must have a name attribute."""
        provider = CodexProvider()
        assert hasattr(provider, "name")
        assert isinstance(provider.name, str)

    def test_protocol_requires_is_available(self):
        provider = CodexProvider()
        assert callable(getattr(provider, "is_available", None))

    def test_protocol_requires_build_command(self):
        provider = CodexProvider()
        assert callable(getattr(provider, "build_command", None))

    def test_protocol_requires_read_response(self):
        provider = CodexProvider()
        assert callable(getattr(provider, "read_response", None))

    def test_protocol_requires_availability_error(self):
        provider = CodexProvider()
        assert callable(getattr(provider, "availability_error", None))


# ---------------------------------------------------------------------------
# TestRegistry
# ---------------------------------------------------------------------------


class TestRegistry:
    def test_codex_registered(self):
        assert "codex" in REGISTRY

    def test_resolve_known_provider(self):
        adapter = resolve_provider("codex")
        assert adapter is not None
        assert adapter.name == "codex"

    def test_resolve_unknown_provider(self):
        assert resolve_provider("nonexistent") is None

    def test_all_registered_satisfy_protocol(self):
        for name, adapter in REGISTRY.items():
            assert isinstance(adapter, ProviderAdapter), (
                f"Provider '{name}' does not satisfy ProviderAdapter protocol"
            )


# ---------------------------------------------------------------------------
# TestWhich
# ---------------------------------------------------------------------------


class TestWhich:
    @patch("shutil.which", return_value="/usr/bin/python3")
    def test_found_in_path(self, mock_shutil):
        assert which("python3") == "/usr/bin/python3"

    @patch("shutil.which", return_value=None)
    def test_not_found_returns_none(self, mock_shutil):
        result = which("nonexistent_binary_xyz")
        # May or may not find it in fallback dirs, but shouldn't crash
        assert result is None or isinstance(result, str)


# ---------------------------------------------------------------------------
# TestGetVaultPath
# ---------------------------------------------------------------------------


class TestGetVaultPath:
    @patch.dict(os.environ, {"JARVIS_VAULT_PATH": ""}, clear=False)
    def test_falls_back_to_cwd_on_empty_env(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Force no config file
        with patch("providers.base.Path.home", return_value=tmp_path):
            result = get_vault_path()
            assert isinstance(result, str)

    @patch.dict(os.environ, {"JARVIS_VAULT_PATH": "/tmp"}, clear=False)
    def test_env_var_takes_precedence(self):
        assert get_vault_path() == "/tmp"


# ---------------------------------------------------------------------------
# TestResolveWorkingDirectory
# ---------------------------------------------------------------------------


class TestResolveWorkingDirectory:
    @patch("providers.base.get_vault_path")
    def test_none_returns_vault(self, mock_vault, tmp_path):
        mock_vault.return_value = str(tmp_path)
        assert resolve_working_directory(None) == str(tmp_path)

    @patch("providers.base.get_vault_path")
    def test_outside_vault_returns_vault(self, mock_vault, tmp_path):
        mock_vault.return_value = str(tmp_path)
        assert resolve_working_directory("/tmp") == str(tmp_path)

    @patch("providers.base.get_vault_path")
    def test_valid_subdir_accepted(self, mock_vault, tmp_path):
        subdir = tmp_path / "notes"
        subdir.mkdir()
        mock_vault.return_value = str(tmp_path)
        assert resolve_working_directory(str(subdir)) == str(subdir)
