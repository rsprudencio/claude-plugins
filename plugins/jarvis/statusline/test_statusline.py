"""Tests for Jarvis statusline."""
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# Add statusline dir to path
sys.path.insert(0, str(Path(__file__).parent))
import statusline as sl


# ---------------------------------------------------------------------------
# Formatter tests
# ---------------------------------------------------------------------------

class TestFmtModel:
    def test_opus(self):
        result = sl._fmt_model({"display_name": "Opus 4.6"})
        assert "Opus 4.6" in result
        assert sl.PURPLE in result

    def test_sonnet(self):
        result = sl._fmt_model({"display_name": "Sonnet 4.6"})
        assert "Sonnet 4.6" in result
        assert sl.BLUE in result

    def test_haiku(self):
        result = sl._fmt_model({"display_name": "Haiku 4.5"})
        assert "Haiku 4.5" in result
        assert sl.GREEN in result

    def test_unknown_model(self):
        result = sl._fmt_model({"id": "gpt-5"})
        assert "gpt-5" in result
        assert sl.CYAN in result

    def test_none(self):
        result = sl._fmt_model(None)
        assert "unknown" in result
        assert sl.GRAY in result

    def test_string_fallback(self):
        result = sl._fmt_model("raw-model-string")
        assert "raw-model-string" in result

    def test_id_only(self):
        result = sl._fmt_model({"id": "claude-opus-4-6"})
        assert "claude-opus-4-6" in result
        assert sl.PURPLE in result


class TestFmtCost:
    def test_zero(self):
        result = sl._fmt_cost(0)
        assert "$0.00" in result
        assert sl.GRAY in result

    def test_cheap(self):
        result = sl._fmt_cost(0.12)
        assert "$0.1200" in result
        assert sl.GREEN in result

    def test_moderate(self):
        result = sl._fmt_cost(3.50)
        assert "$3.5000" in result
        assert sl.YELLOW in result

    def test_expensive(self):
        result = sl._fmt_cost(15.0)
        assert "$15.0000" in result
        assert sl.RED in result

    def test_none(self):
        result = sl._fmt_cost(None)
        assert "$0.00" in result

    def test_string_input(self):
        result = sl._fmt_cost("not a number")
        assert "$0.00" in result


class TestFmtContext:
    def test_zero(self):
        result = sl._fmt_context({})
        assert "0%" in result

    def test_low(self):
        result = sl._fmt_context({"context_window": {"used_percentage": 25.0}})
        assert "25.0%" in result
        assert sl.GREEN in result

    def test_high(self):
        result = sl._fmt_context({"context_window": {"used_percentage": 75.0}})
        assert "75.0%" in result
        assert sl.YELLOW in result

    def test_critical(self):
        result = sl._fmt_context({"context_window": {"used_percentage": 95.0}})
        assert "95.0%" in result
        assert sl.RED in result

    def test_missing_context_window(self):
        result = sl._fmt_context({"model": "test"})
        assert "0%" in result

    def test_null_percentage(self):
        result = sl._fmt_context({"context_window": {"used_percentage": None}})
        assert "0%" in result


class TestFmtDuration:
    def test_zero(self):
        result = sl._fmt_duration(0)
        assert "0s" in result

    def test_seconds(self):
        result = sl._fmt_duration(45000)
        assert "45s" in result

    def test_minutes(self):
        result = sl._fmt_duration(125000)
        assert "2m5s" in result

    def test_hours(self):
        result = sl._fmt_duration(3700000)
        assert "1h1m" in result

    def test_none(self):
        result = sl._fmt_duration(None)
        assert "0s" in result


class TestFmtLines:
    def test_both(self):
        result = sl._fmt_lines(10, 5, False)
        assert "+10" in result
        assert "-5" in result

    def test_added_only(self):
        result = sl._fmt_lines(10, 0, False)
        assert "+10" in result
        assert "-" not in result

    def test_removed_only(self):
        result = sl._fmt_lines(0, 3, False)
        assert "-3" in result
        assert "+" not in result

    def test_dirty_no_changes(self):
        result = sl._fmt_lines(0, 0, True)
        assert "~" in result

    def test_clean_no_changes(self):
        result = sl._fmt_lines(0, 0, False)
        assert result == ""


class TestFmtMcp:
    def test_none(self):
        result = sl._fmt_mcp({"count": 0, "servers": []})
        assert "0 MCP" in result
        assert sl.GRAY in result

    def test_few(self):
        result = sl._fmt_mcp({"count": 2, "servers": ["core", "todoist"]})
        assert "2 MCP" in result
        assert "core, todoist" in result
        assert sl.GREEN in result

    def test_many(self):
        result = sl._fmt_mcp({"count": 5, "servers": ["a", "b", "c", "d", "e"]})
        assert "5 MCP" in result
        assert "a, b +3" in result
        assert sl.YELLOW in result


class TestFmtJarvis:
    def test_healthy(self):
        result = sl._fmt_jarvis({"ok": True, "version": "2.0.0"})
        assert "J:2.0.0" in result
        assert sl.GREEN in result

    def test_healthy_no_version(self):
        result = sl._fmt_jarvis({"ok": True, "version": ""})
        assert result.count("J") >= 1
        assert sl.GREEN in result

    def test_down(self):
        result = sl._fmt_jarvis({"ok": False})
        assert "J:down" in result
        assert sl.RED in result

    def test_full_healthy(self):
        result = sl._fmt_jarvis({"ok": True, "version": "2.0.0"}, full=True)
        assert "JARVIS" in result
        assert "v2.0.0" in result
        assert sl.YELLOW in result

    def test_full_down_returns_empty(self):
        result = sl._fmt_jarvis({"ok": False}, full=True)
        assert result == ""


# ---------------------------------------------------------------------------
# Cache tests
# ---------------------------------------------------------------------------

class TestCache:
    def test_write_read_cycle(self, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            sl._write_cache("test.json", {"key": "value"})
            result = sl._read_cache("test.json", 60)
            assert result == {"key": "value"}

    def test_expired_cache(self, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            sl._write_cache("old.json", {"stale": True})
            # Set mtime to the past
            cache_file = tmp_path / "old.json"
            old_time = os.path.getmtime(cache_file) - 120
            os.utime(cache_file, (old_time, old_time))
            result = sl._read_cache("old.json", 60)
            assert result is None

    def test_missing_cache(self, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            result = sl._read_cache("missing.json", 60)
            assert result is None

    def test_corrupt_cache(self, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            (tmp_path / "bad.json").write_text("not json{{{")
            result = sl._read_cache("bad.json", 60)
            assert result is None


# ---------------------------------------------------------------------------
# Generate tests
# ---------------------------------------------------------------------------

class TestGenerate:
    @mock.patch.object(sl, "_jarvis_health", return_value={"ok": True, "version": "2.0.0"})
    @mock.patch.object(sl, "_mcp_info", return_value={"count": 2, "servers": ["core", "todoist"]})
    @mock.patch.object(sl, "_git_info", return_value={"branch": "main", "dirty": False})
    def test_full_output(self, mock_git, mock_mcp, mock_jarvis, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            data = {
                "model": {"display_name": "Opus 4.6"},
                "cwd": "/Users/test/project",
                "cost": {
                    "total_cost_usd": 0.50,
                    "total_duration_ms": 60000,
                    "total_lines_added": 20,
                    "total_lines_removed": 5,
                },
                "context_window": {"used_percentage": 45.0},
            }
            output = sl.generate(data)
            # Single line with │ separators
            assert "\n" not in output
            assert "JARVIS" in output
            assert "v2.0.0" in output
            assert "Opus 4.6" in output
            assert "project" in output
            assert "main" in output
            assert "$0.5000" in output
            assert "45.0%" in output

    @mock.patch.object(sl, "_jarvis_health", return_value={"ok": False})
    @mock.patch.object(sl, "_mcp_info", return_value={"count": 0, "servers": []})
    @mock.patch.object(sl, "_git_info", return_value={"branch": "no-git", "dirty": False})
    def test_minimal_data(self, mock_git, mock_mcp, mock_jarvis, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            output = sl.generate({})
            # No JARVIS branding when server is down
            assert "JARVIS" not in output
            assert "\n" not in output

    @mock.patch.object(sl, "_jarvis_health", return_value={"ok": True, "version": "2.0.0"})
    @mock.patch.object(sl, "_mcp_info", return_value={"count": 2, "servers": ["core", "todoist"]})
    @mock.patch.object(sl, "_git_info", return_value={"branch": "main", "dirty": False})
    def test_zero_cost_hidden(self, mock_git, mock_mcp, mock_jarvis, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            output = sl.generate({"model": "test", "cwd": "/tmp/x"})
            assert "$" not in output

    @mock.patch.object(sl, "_jarvis_health", return_value={"ok": True, "version": "2.0.0"})
    @mock.patch.object(sl, "_mcp_info", return_value={"count": 2, "servers": ["core", "todoist"]})
    @mock.patch.object(sl, "_git_info", return_value={"branch": "main", "dirty": False})
    def test_zero_context_hidden(self, mock_git, mock_mcp, mock_jarvis, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            output = sl.generate({"model": "test", "cwd": "/tmp/x"})
            assert "ctx" not in output

    @mock.patch.object(sl, "_jarvis_health", return_value={"ok": True, "version": "1.44.0"})
    @mock.patch.object(sl, "_mcp_info", return_value={"count": 1, "servers": ["core"]})
    @mock.patch.object(sl, "_git_info", return_value={"branch": "feature/x", "dirty": True})
    def test_dirty_with_changes(self, mock_git, mock_mcp, mock_jarvis, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            data = {
                "model": {"display_name": "Sonnet 4.6"},
                "cwd": "/tmp/test",
                "cost": {"total_cost_usd": 0, "total_duration_ms": 0,
                         "total_lines_added": 0, "total_lines_removed": 0},
            }
            output = sl.generate(data)
            # Dirty flag with no line changes should show ~
            assert "feature/x*" in output
            assert "~" in output


# ---------------------------------------------------------------------------
# Git info tests (mocked subprocess)
# ---------------------------------------------------------------------------

class TestGitInfo:
    @mock.patch("statusline.subprocess.run")
    def test_normal_repo(self, mock_run, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            mock_run.side_effect = [
                mock.Mock(stdout="main\n"),
                mock.Mock(stdout="M file.py\n"),
            ]
            result = sl._git_info()
            assert result["branch"] == "main"
            assert result["dirty"] is True

    @mock.patch("statusline.subprocess.run")
    def test_clean_repo(self, mock_run, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            mock_run.side_effect = [
                mock.Mock(stdout="develop\n"),
                mock.Mock(stdout=""),
            ]
            result = sl._git_info()
            assert result["branch"] == "develop"
            assert result["dirty"] is False

    @mock.patch("statusline.subprocess.run", side_effect=FileNotFoundError)
    def test_no_git(self, mock_run, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            result = sl._git_info()
            assert result["branch"] == "no-git"
            assert result["dirty"] is False

    def test_uses_cache(self, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            sl._write_cache("git.json", {"branch": "cached", "dirty": False})
            result = sl._git_info()
            assert result["branch"] == "cached"


# ---------------------------------------------------------------------------
# MCP info tests (mocked subprocess)
# ---------------------------------------------------------------------------

class TestMcpInfo:
    @mock.patch("statusline.subprocess.run")
    def test_parses_server_list(self, mock_run, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout="jarvis-core: http://localhost:8741 - connected\ntodoist: http://localhost:8742 - connected\n",
            )
            result = sl._mcp_info()
            assert result["count"] == 2
            assert "jarvis-core" in result["servers"]
            assert "todoist" in result["servers"]

    @mock.patch("statusline.subprocess.run")
    def test_skips_checking_line(self, mock_run, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout="Checking MCP servers...\ncore: local - ok\n",
            )
            result = sl._mcp_info()
            assert result["count"] == 1
            assert "core" in result["servers"]

    @mock.patch("statusline.subprocess.run", side_effect=FileNotFoundError)
    def test_claude_not_found(self, mock_run, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            result = sl._mcp_info()
            assert result["count"] == 0
            assert result["servers"] == []

    @mock.patch("statusline.subprocess.run")
    def test_strips_claudecode_env(self, mock_run, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            mock_run.return_value = mock.Mock(returncode=0, stdout="")
            with mock.patch.dict(os.environ, {"CLAUDECODE": "1"}):
                sl._mcp_info()
            call_env = mock_run.call_args[1]["env"]
            assert "CLAUDECODE" not in call_env


# ---------------------------------------------------------------------------
# Jarvis health tests (mocked subprocess)
# ---------------------------------------------------------------------------

class TestJarvisHealth:
    @mock.patch("statusline.subprocess.run")
    def test_healthy(self, mock_run, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout='{"status":"ok","server":"jarvis-core","version":"1.44.0"}',
            )
            result = sl._jarvis_health()
            assert result["ok"] is True
            assert result["version"] == "1.44.0"

    @mock.patch("statusline.subprocess.run")
    def test_down(self, mock_run, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            mock_run.return_value = mock.Mock(returncode=1, stdout="")
            result = sl._jarvis_health()
            assert result["ok"] is False

    @mock.patch("statusline.subprocess.run", side_effect=FileNotFoundError)
    def test_no_curl(self, mock_run, tmp_path):
        with mock.patch.object(sl, "CACHE_DIR", tmp_path):
            result = sl._jarvis_health()
            assert result["ok"] is False
