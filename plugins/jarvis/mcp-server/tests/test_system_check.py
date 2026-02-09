"""Integration tests for system_check module."""
import sys
import pytest
from unittest.mock import patch, MagicMock

from tools.system_check import (
    check_python_version,
    check_uv,
    check_git,
    check_platform_specific,
    run_system_check,
    format_check_result,
)


class TestCheckPythonVersion:
    """Tests for Python version checking."""

    def test_check_python_version_passes(self):
        """Test Python version check passes (we have Python 3.10+)."""
        is_valid, message, details = check_python_version()
        assert is_valid == True
        assert "✓ Python" in message
        assert "required" in details
        assert "current" in details
        assert details["platform"] in ("Darwin", "Linux", "Windows")

    @patch('sys.version_info', (3, 9, 5, 'final', 0))
    def test_check_python_version_fails(self):
        """Test Python version check fails with old version."""
        is_valid, message, details = check_python_version()
        assert is_valid == False
        assert "✗ Python" in message or "✗" in message
        assert "3.9.5" in message
        assert details["current"] == "3.9.5"


class TestCheckUV:
    """Tests for uv/uvx checking."""

    @patch('tools.system_check.which')
    def test_check_uv_uvx_found(self, mock_which):
        """Test uvx found."""
        mock_which.side_effect = lambda cmd, enriched: "/usr/local/bin/uvx" if cmd == "uvx" else None

        is_valid, message, details = check_uv()
        assert is_valid == True
        assert "✓ uvx found" in message
        assert details["uvx_path"] == "/usr/local/bin/uvx"

    @patch('tools.system_check.which')
    def test_check_uv_only_uv_found(self, mock_which):
        """Test only uv found (uvx should be available too)."""
        mock_which.side_effect = lambda cmd, enriched: "/usr/local/bin/uv" if cmd == "uv" else None

        is_valid, message, details = check_uv()
        assert is_valid == True
        assert "✓ uv found" in message
        assert "uvx should be available" in message
        assert details["uv_path"] == "/usr/local/bin/uv"

    @patch('tools.system_check.which')
    def test_check_uv_not_found(self, mock_which):
        """Test uv not found."""
        mock_which.return_value = None

        is_valid, message, details = check_uv()
        assert is_valid == False
        assert "✗ uv" in message
        assert "not found" in message
        assert details["uv_path"] is None
        assert details["uvx_path"] is None

    @patch('tools.system_check.which')
    @patch('subprocess.run')
    def test_check_uv_with_version(self, mock_run, mock_which):
        """Test uvx version extraction."""
        mock_which.side_effect = lambda cmd, enriched: "/usr/local/bin/uvx" if cmd == "uvx" else None

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "uv 0.9.30 (Homebrew 2026-02-04)"
        mock_run.return_value = mock_result

        is_valid, message, details = check_uv()
        assert is_valid == True
        assert "version" in details
        assert "0.9.30" in details["version"]


class TestCheckGit:
    """Tests for git checking."""

    @patch('tools.system_check.which')
    def test_check_git_found(self, mock_which):
        """Test git found."""
        mock_which.return_value = "/usr/bin/git"

        is_valid, message, details = check_git()
        assert is_valid == True
        assert "✓ git found" in message
        assert details["git_path"] == "/usr/bin/git"

    @patch('tools.system_check.which')
    def test_check_git_not_found(self, mock_which):
        """Test git not found."""
        mock_which.return_value = None

        is_valid, message, details = check_git()
        assert is_valid == False
        assert "✗ git" in message
        assert "not found" in message
        assert details["git_path"] is None

    @patch('tools.system_check.which')
    @patch('subprocess.run')
    def test_check_git_with_version(self, mock_run, mock_which):
        """Test git version extraction."""
        mock_which.return_value = "/usr/bin/git"

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "git version 2.39.3 (Apple Git-145)"
        mock_run.return_value = mock_result

        is_valid, message, details = check_git()
        assert is_valid == True
        assert "version" in details
        assert "2.39.3" in details["version"]


class TestPlatformSpecific:
    """Tests for platform-specific checks."""

    @patch('platform.system')
    def test_check_platform_specific_darwin(self, mock_system):
        """Test platform-specific checks on macOS."""
        mock_system.return_value = "Darwin"

        checks = check_platform_specific()
        assert isinstance(checks, list)

    @patch('platform.system')
    def test_check_platform_specific_windows(self, mock_system):
        """Test platform-specific checks on Windows."""
        mock_system.return_value = "Windows"

        checks = check_platform_specific()
        assert isinstance(checks, list)
        # Should have at least the symlink warning
        assert len(checks) >= 1


class TestRunSystemCheck:
    """Tests for comprehensive system check."""

    def test_run_system_check_structure(self):
        """Test run_system_check returns proper structure."""
        result = run_system_check()

        assert "platform" in result
        assert "healthy" in result
        assert "critical_issues" in result
        assert "warnings" in result
        assert "details" in result
        assert "summary" in result

        assert isinstance(result["healthy"], bool)
        assert isinstance(result["critical_issues"], list)
        assert isinstance(result["warnings"], list)
        assert isinstance(result["details"], dict)
        assert isinstance(result["summary"], dict)

    def test_run_system_check_details(self):
        """Test run_system_check includes all check details."""
        result = run_system_check()

        details = result["details"]
        assert "python" in details
        assert "uv" in details
        assert "git" in details
        assert "platform" in details

    def test_run_system_check_summary(self):
        """Test run_system_check summary."""
        result = run_system_check()

        summary = result["summary"]
        assert "python" in summary
        assert "uv" in summary
        assert "git" in summary

    @patch('tools.system_check.check_python_version')
    @patch('tools.system_check.check_uv')
    @patch('tools.system_check.check_git')
    def test_run_system_check_healthy_when_all_pass(self, mock_git, mock_uv, mock_python):
        """Test healthy=True when all critical checks pass."""
        mock_python.return_value = (True, "✓ Python 3.11.6", {"current": "3.11.6"})
        mock_uv.return_value = (True, "✓ uvx found", {"uvx_path": "/usr/bin/uvx"})
        mock_git.return_value = (True, "✓ git found", {"git_path": "/usr/bin/git"})

        result = run_system_check()
        assert result["healthy"] == True
        assert len(result["critical_issues"]) == 0

    @patch('tools.system_check.check_python_version')
    @patch('tools.system_check.check_uv')
    @patch('tools.system_check.check_git')
    def test_run_system_check_unhealthy_when_check_fails(self, mock_git, mock_uv, mock_python):
        """Test healthy=False when any critical check fails."""
        mock_python.return_value = (True, "✓ Python 3.11.6", {"current": "3.11.6"})
        mock_uv.return_value = (False, "✗ uv not found", {"uv_path": None, "uvx_path": None})
        mock_git.return_value = (True, "✓ git found", {"git_path": "/usr/bin/git"})

        result = run_system_check()
        assert result["healthy"] == False
        assert len(result["critical_issues"]) > 0


class TestFormatCheckResult:
    """Tests for formatting check results."""

    def test_format_check_result_basic(self):
        """Test basic formatting of check result."""
        result = run_system_check()
        output = format_check_result(result, verbose=False)

        assert isinstance(output, str)
        assert "Jarvis System Requirements Check" in output
        assert "Critical Requirements:" in output
        assert "Python" in output

    def test_format_check_result_verbose(self):
        """Test verbose formatting includes detailed info."""
        result = run_system_check()
        output = format_check_result(result, verbose=True)

        assert "Detailed Information:" in output
        assert "Platform:" in output
        assert "Machine:" in output

    @patch('tools.system_check.check_uv')
    def test_format_check_result_with_issues(self, mock_uv):
        """Test formatting when there are critical issues."""
        mock_uv.return_value = (False, "✗ uv not found", {"uv_path": None, "uvx_path": None})

        result = run_system_check()
        output = format_check_result(result, verbose=False)

        if not result["healthy"]:
            assert "Critical Issues:" in output
            assert "✗" in output
