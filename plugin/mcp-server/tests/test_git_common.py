"""Tests for git_common.py shared git utilities."""
import subprocess
from unittest.mock import Mock
import pytest

from tools.git_common import run_git_command, get_vault_path_safe, GIT_ENV, GIT_TIMEOUT


class TestRunGitCommand:
    """Test run_git_command function."""

    def test_simple_command_succeeds(self, mock_config, git_repo):
        """Simple git command executes successfully."""
        success, result = run_git_command(["status", "--short"])

        assert success is True
        assert result["success"] is True
        assert "stdout" in result
        assert "returncode" in result
        assert result["returncode"] == 0

    def test_command_with_args(self, mock_config, git_repo):
        """Git command with multiple arguments."""
        success, result = run_git_command(["log", "-1", "--oneline"])

        assert success is True
        assert result["success"] is True
        assert len(result["stdout"]) > 0

    def test_captures_stdout_stderr(self, mock_config, git_repo):
        """Command captures both stdout and stderr."""
        success, result = run_git_command(["status"])

        assert "stdout" in result
        assert "stderr" in result
        # Git status outputs to stdout
        assert len(result["stdout"]) > 0

    def test_runs_in_vault_directory(self, mock_config, git_repo, monkeypatch):
        """Command runs in vault directory (cwd verification)."""
        called_with_cwd = None
        original_run = subprocess.run

        def mock_run(*args, **kwargs):
            nonlocal called_with_cwd
            called_with_cwd = kwargs.get("cwd")
            return original_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", mock_run)

        run_git_command(["status"])

        assert called_with_cwd is not None
        assert str(called_with_cwd) == str(git_repo)

    def test_disables_git_pager(self, mock_config, git_repo, monkeypatch):
        """Verifies GIT_PAGER is disabled in environment."""
        called_with_env = None
        original_run = subprocess.run

        def mock_run(*args, **kwargs):
            nonlocal called_with_env
            called_with_env = kwargs.get("env")
            return original_run(*args, **kwargs)

        monkeypatch.setattr(subprocess, "run", mock_run)

        run_git_command(["log"])

        assert called_with_env is not None
        assert called_with_env.get("GIT_PAGER") == ""

    def test_vault_not_configured_returns_permission_denied(self, no_config):
        """Returns permission denied when vault not configured."""
        success, result = run_git_command(["status"])

        assert success is False
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]

    def test_vault_not_confirmed_returns_permission_denied(self, unconfirmed_config):
        """Returns permission denied when vault not confirmed."""
        success, result = run_git_command(["status"])

        assert success is False
        assert result["success"] is False
        assert "PERMISSION DENIED" in result["error"]

    def test_command_failure_returns_error(self, mock_config, git_repo):
        """Non-zero exit code returns error."""
        # Try to checkout a branch that doesn't exist
        success, result = run_git_command(["checkout", "nonexistent-branch"])

        assert success is False
        assert result["success"] is False
        assert result["returncode"] != 0
        assert "error" in result

    def test_command_timeout_returns_error(self, mock_config, git_repo, monkeypatch):
        """Command timeout returns error."""
        def mock_timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=1)

        monkeypatch.setattr(subprocess, "run", mock_timeout)

        success, result = run_git_command(["status"])

        assert success is False
        assert result["success"] is False
        assert "timed out" in result["error"].lower()

    def test_not_a_git_repo_returns_error(self, mock_config, temp_vault):
        """Git command in non-repo returns error."""
        # temp_vault has git repo, so remove it
        import shutil
        git_dir = temp_vault / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir)

        success, result = run_git_command(["status"])

        assert success is False
        assert result["success"] is False

    def test_command_with_special_characters(self, mock_config, git_repo):
        """Command handles special characters in arguments."""
        # Create file with special chars
        test_file = git_repo / "file with spaces.txt"
        test_file.write_text("content")

        success, result = run_git_command(["add", str(test_file)])

        assert success is True
        assert result["success"] is True

    def test_command_with_unicode(self, mock_config, git_repo):
        """Command handles unicode in arguments."""
        test_file = git_repo / "文件.txt"
        test_file.write_text("content")

        success, result = run_git_command(["add", str(test_file)])

        # Should succeed or fail gracefully
        assert "success" in result


class TestGetVaultPathSafe:
    """Test get_vault_path_safe function."""

    def test_returns_path_when_configured(self, mock_config):
        """Returns vault path when properly configured."""
        path = get_vault_path_safe()

        assert path is not None
        assert str(path) == str(mock_config.vault_path)

    def test_returns_none_when_not_configured(self, no_config):
        """Returns None when config file doesn't exist."""
        path = get_vault_path_safe()

        assert path is None

    def test_returns_none_when_not_confirmed(self, unconfirmed_config):
        """Returns None when vault not confirmed."""
        path = get_vault_path_safe()

        assert path is None
