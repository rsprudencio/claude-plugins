"""Tests for providers.codex — CodexProvider adapter."""

import os
import sys
import pytest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "hooks-handlers"))

from providers.codex import CodexProvider


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def provider():
    return CodexProvider()


# ---------------------------------------------------------------------------
# TestCodexAvailability
# ---------------------------------------------------------------------------


class TestCodexAvailability:
    def test_name(self, provider):
        assert provider.name == "codex"

    @patch("providers.codex.which", return_value="/usr/bin/codex")
    def test_available_when_binary_found(self, mock_which, provider):
        available, path = provider.is_available()
        assert available is True
        assert path == "/usr/bin/codex"

    @patch("providers.codex.which", return_value=None)
    def test_unavailable_when_binary_missing(self, mock_which, provider):
        available, path = provider.is_available()
        assert available is False
        assert path is None

    def test_availability_error_message(self, provider):
        msg = provider.availability_error()
        assert "codex" in msg
        assert "not found" in msg


# ---------------------------------------------------------------------------
# TestCodexBuildCommand
# ---------------------------------------------------------------------------


class TestCodexBuildCommand:
    def test_basic_structure(self, provider):
        cmd = provider.build_command(
            binary="/usr/bin/codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        assert cmd[0] == "/usr/bin/codex"
        assert cmd[1] == "exec"
        assert cmd[-1] == "-"

    def test_sandbox_read_only(self, provider):
        cmd = provider.build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        assert "--sandbox" in cmd
        idx = cmd.index("--sandbox")
        assert cmd[idx + 1] == "read-only"

    def test_ephemeral_flag(self, provider):
        cmd = provider.build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        assert "--ephemeral" in cmd

    def test_output_schema(self, provider):
        cmd = provider.build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        assert "--output-schema" in cmd
        idx = cmd.index("--output-schema")
        assert cmd[idx + 1] == "/tmp/schema.json"

    def test_output_last_message(self, provider):
        cmd = provider.build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        assert "--output-last-message" in cmd
        idx = cmd.index("--output-last-message")
        assert cmd[idx + 1] == "/tmp/out.md"

    def test_working_directory(self, provider):
        cmd = provider.build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/my/vault",
        )
        assert "--cd" in cmd
        idx = cmd.index("--cd")
        assert cmd[idx + 1] == "/my/vault"

    def test_model_override(self, provider):
        cmd = provider.build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
            model="o3",
        )
        assert "--model" in cmd
        idx = cmd.index("--model")
        assert cmd[idx + 1] == "o3"

    def test_no_model_by_default(self, provider):
        cmd = provider.build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        assert "--model" not in cmd

    def test_profile_override(self, provider):
        cmd = provider.build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
            profile="review",
        )
        assert "--profile" in cmd
        idx = cmd.index("--profile")
        assert cmd[idx + 1] == "review"

    def test_no_profile_by_default(self, provider):
        cmd = provider.build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        assert "--profile" not in cmd

    def test_mcp_disable_flags(self, provider):
        cmd = provider.build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        # Should have -c flags for MCP server disabling
        c_indices = [i for i, x in enumerate(cmd) if x == "-c"]
        assert len(c_indices) >= 2
        overrides = [cmd[i + 1] for i in c_indices]
        assert any("jarvis_core" in o for o in overrides)
        assert any("jarvis-todoist" in o for o in overrides)

    def test_stdin_marker_last(self, provider):
        cmd = provider.build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
            model="o3",
            profile="review",
        )
        assert cmd[-1] == "-"

    def test_no_approve_flags(self, provider):
        """Safety: no auto-approve flags should ever appear."""
        cmd = provider.build_command(
            binary="codex",
            output_path="/tmp/out.md",
            schema_path="/tmp/schema.json",
            working_dir="/vault",
        )
        assert "-a" not in cmd
        assert "--approve" not in cmd
        assert "--full-auto" not in cmd


# ---------------------------------------------------------------------------
# TestCodexReadResponse
# ---------------------------------------------------------------------------


class TestCodexReadResponse:
    def test_reads_from_file(self, provider, tmp_path):
        out_file = tmp_path / "response.md"
        out_file.write_text('{"status": "approved"}')
        result = provider.read_response(str(out_file), "fallback")
        assert result == '{"status": "approved"}'

    def test_falls_back_to_stdout(self, provider):
        result = provider.read_response("/nonexistent/path", "stdout output")
        assert result == "stdout output"

    def test_empty_file_falls_back(self, provider, tmp_path):
        out_file = tmp_path / "response.md"
        out_file.write_text("   ")
        result = provider.read_response(str(out_file), "fallback content")
        assert result == "fallback content"

    def test_empty_stdout_and_missing_file(self, provider):
        result = provider.read_response("/nonexistent", "")
        assert result == ""
