"""Integration tests for system_check module."""

import sys
import pytest
from unittest.mock import patch, MagicMock

from tools.system_check import (
    check_python_version,
    check_docker,
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

    @patch("sys.version_info", (3, 9, 5, "final", 0))
    def test_check_python_version_fails(self):
        """Test Python version check fails with old version."""
        is_valid, message, details = check_python_version()
        assert is_valid == False
        assert "✗ Python" in message or "✗" in message
        assert "3.9.5" in message
        assert details["current"] == "3.9.5"


class TestCheckDocker:
    """Tests for Docker with Compose checking."""

    @patch("tools.system_check.which")
    @patch("subprocess.run")
    def test_check_docker_found(self, mock_run, mock_which):
        """Test Docker with Compose found."""
        mock_which.return_value = "/usr/local/bin/docker"

        def run_side_effect(cmd, **kwargs):
            result = MagicMock()
            result.returncode = 0
            if "compose" in cmd:
                result.stdout = "Docker Compose version v2.24.5"
            else:
                result.stdout = "Docker version 25.0.3, build 4debf41"
            return result

        mock_run.side_effect = run_side_effect

        is_valid, message, details = check_docker()
        assert is_valid == True
        assert "✓ Docker with Compose found" in message
        assert details["docker_path"] == "/usr/local/bin/docker"
        assert details["compose_available"] == True

    @patch("tools.system_check.which")
    def test_check_docker_not_found(self, mock_which):
        """Test Docker not found."""
        mock_which.return_value = None

        is_valid, message, details = check_docker()
        assert is_valid == False
        assert "not found" in message
        assert details["docker_path"] is None

    @patch("tools.system_check.which")
    @patch("subprocess.run")
    def test_check_docker_no_compose(self, mock_run, mock_which):
        """Test Docker found but Compose plugin missing."""
        mock_which.return_value = "/usr/local/bin/docker"

        def run_side_effect(cmd, **kwargs):
            result = MagicMock()
            if "compose" in cmd:
                result.returncode = 1
                result.stdout = ""
            else:
                result.returncode = 0
                result.stdout = "Docker version 25.0.3"
            return result

        mock_run.side_effect = run_side_effect

        is_valid, message, details = check_docker()
        assert is_valid == False
        assert "Compose" in message
        assert details["docker_path"] == "/usr/local/bin/docker"
        assert details["compose_available"] == False


class TestCheckGit:
    """Tests for git checking."""

    @patch("tools.system_check.which")
    def test_check_git_found(self, mock_which):
        """Test git found."""
        mock_which.return_value = "/usr/bin/git"

        is_valid, message, details = check_git()
        assert is_valid == True
        assert "✓ git found" in message
        assert details["git_path"] == "/usr/bin/git"

    @patch("tools.system_check.which")
    def test_check_git_not_found(self, mock_which):
        """Test git not found."""
        mock_which.return_value = None

        is_valid, message, details = check_git()
        assert is_valid == False
        assert "✗ git" in message
        assert "not found" in message
        assert details["git_path"] is None

    @patch("tools.system_check.which")
    @patch("subprocess.run")
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

    @patch("platform.system")
    def test_check_platform_specific_darwin(self, mock_system):
        """Test platform-specific checks on macOS."""
        mock_system.return_value = "Darwin"

        checks = check_platform_specific()
        assert isinstance(checks, list)

    @patch("platform.system")
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
        assert "docker" in details
        assert "git" in details
        assert "platform" in details

    def test_run_system_check_summary(self):
        """Test run_system_check summary."""
        result = run_system_check()

        summary = result["summary"]
        assert "python" in summary
        assert "docker" in summary
        assert "git" in summary

    @patch("tools.system_check.check_python_version")
    @patch("tools.system_check.check_docker")
    @patch("tools.system_check.check_git")
    def test_run_system_check_healthy_when_all_pass(
        self, mock_git, mock_docker, mock_python
    ):
        """Test healthy=True when all critical checks pass."""
        mock_python.return_value = (True, "✓ Python 3.11.6", {"current": "3.11.6"})
        mock_docker.return_value = (
            True,
            "✓ Docker with Compose found",
            {"docker_path": "/usr/bin/docker", "compose_available": True},
        )
        mock_git.return_value = (True, "✓ git found", {"git_path": "/usr/bin/git"})

        result = run_system_check()
        assert result["healthy"] == True
        assert len(result["critical_issues"]) == 0

    @patch("tools.system_check.check_python_version")
    @patch("tools.system_check.check_docker")
    @patch("tools.system_check.check_git")
    def test_run_system_check_unhealthy_when_check_fails(
        self, mock_git, mock_docker, mock_python
    ):
        """Test healthy=False when any critical check fails."""
        mock_python.return_value = (True, "✓ Python 3.11.6", {"current": "3.11.6"})
        mock_docker.return_value = (
            False,
            "✗ Docker not found",
            {"docker_path": None, "compose_available": False},
        )
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

    @patch("tools.system_check.check_docker")
    def test_format_check_result_with_issues(self, mock_docker):
        """Test formatting when there are critical issues."""
        mock_docker.return_value = (
            False,
            "✗ Docker not found",
            {"docker_path": None, "compose_available": False},
        )

        result = run_system_check()
        output = format_check_result(result, verbose=False)

        if not result["healthy"]:
            assert "Critical Issues:" in output
            assert "✗" in output
