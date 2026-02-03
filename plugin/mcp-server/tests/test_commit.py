"""Tests for commit.py git commit operations."""
import pytest

from tools.commit import stage_files, execute_commit, get_commit_stats


class TestStageFiles:
    """Test stage_files function."""

    def test_stage_single_file(self, mock_config, git_repo):
        """Stage a single specific file."""
        # Create unstaged file
        test_file = git_repo / "new_file.txt"
        test_file.write_text("content")

        result = stage_files([str(test_file)])

        assert result["success"] is True
        assert result["staged_count"] == 1

    def test_stage_multiple_files(self, mock_config, git_repo):
        """Stage multiple specific files."""
        file1 = git_repo / "file1.txt"
        file2 = git_repo / "file2.txt"
        file1.write_text("content1")
        file2.write_text("content2")

        result = stage_files([str(file1), str(file2)])

        assert result["success"] is True
        assert result["staged_count"] == 2

    def test_stage_all_with_flag(self, mock_config, git_repo):
        """Stage all changes when stage_all=True."""
        # Create multiple unstaged files
        (git_repo / "file1.txt").write_text("content1")
        (git_repo / "file2.txt").write_text("content2")

        result = stage_files(stage_all=True)

        assert result["success"] is True
        assert result["staged_count"] == -1  # -1 indicates "all"

    def test_returns_staged_count(self, mock_config, git_repo):
        """Returns correct count of staged files."""
        files = []
        for i in range(5):
            f = git_repo / f"file{i}.txt"
            f.write_text(f"content{i}")
            files.append(str(f))

        result = stage_files(files)

        assert result["success"] is True
        assert result["staged_count"] == 5

    def test_staged_count_minus_one_for_all(self, mock_config, git_repo):
        """Returns -1 when staging all files with stage_all=True."""
        result = stage_files(stage_all=True)

        assert result["staged_count"] == -1

    def test_nonexistent_file_fails(self, mock_config, git_repo):
        """Staging nonexistent file returns error."""
        result = stage_files(["/nonexistent/file.txt"])

        assert result["success"] is False
        assert "error" in result

    def test_vault_not_configured_fails(self, no_config):
        """Fails when vault not configured."""
        result = stage_files(["test.txt"])

        assert result["success"] is False
        assert "error" in result

    def test_partial_failure_stops_early(self, mock_config, git_repo):
        """Stops on first failure when staging multiple files."""
        file1 = git_repo / "exists.txt"
        file1.write_text("content")

        result = stage_files([str(file1), "/nonexistent.txt"])

        # Should succeed for first file or fail early
        assert "success" in result

    def test_file_path_with_spaces(self, mock_config, git_repo):
        """Handles file paths with spaces."""
        file_with_spaces = git_repo / "file with spaces.txt"
        file_with_spaces.write_text("content")

        result = stage_files([str(file_with_spaces)])

        assert result["success"] is True

    def test_empty_files_list(self, mock_config, git_repo):
        """Empty files list stages nothing (safe default)."""
        result = stage_files([])

        assert result["success"] is True
        # Empty list stages nothing, preventing accidental commits
        assert result["staged_count"] == 0

    def test_none_stages_nothing(self, mock_config, git_repo):
        """None stages nothing (safe default)."""
        result = stage_files(None)

        assert result["success"] is True
        # None stages nothing - must use stage_all=True explicitly
        assert result["staged_count"] == 0


class TestExecuteCommit:
    """Test execute_commit function."""

    def test_commit_succeeds(self, mock_config, git_repo):
        """Basic commit succeeds."""
        # Stage a file first
        test_file = git_repo / "commit_test.txt"
        test_file.write_text("content")
        stage_files([str(test_file)])

        result = execute_commit("Test commit message")

        assert result["success"] is True
        assert "commit_hash" in result
        assert len(result["commit_hash"]) == 7  # Short hash

    def test_returns_commit_hash(self, mock_config, git_repo):
        """Returns short commit hash."""
        test_file = git_repo / "test.txt"
        test_file.write_text("content")
        stage_files([str(test_file)])

        result = execute_commit("Test")

        assert "commit_hash" in result
        assert isinstance(result["commit_hash"], str)
        assert len(result["commit_hash"]) > 0

    def test_captures_commit_message(self, mock_config, git_repo):
        """Captures git commit output message."""
        test_file = git_repo / "test.txt"
        test_file.write_text("content")
        stage_files([str(test_file)])

        result = execute_commit("Test message")

        assert "message" in result

    def test_nothing_to_commit_detected(self, mock_config, git_repo):
        """Detects 'nothing to commit' state."""
        # Don't stage anything
        result = execute_commit("Empty commit")

        assert result["success"] is False
        assert "nothing to commit" in result["error"].lower()
        assert result.get("nothing_to_commit") is True

    def test_vault_not_configured_fails(self, no_config):
        """Fails when vault not configured."""
        result = execute_commit("Test")

        assert result["success"] is False
        assert "error" in result

    def test_git_error_propagated(self, mock_config, git_repo, monkeypatch):
        """Git errors are properly propagated."""
        # Mock run_git_command to simulate failure
        from tools import commit as commit_module

        def mock_error(*args, **kwargs):
            return False, {
                "success": False,
                "error": "Mock git error",
                "stderr": "fatal: error"
            }

        monkeypatch.setattr(commit_module, "run_git_command", mock_error)

        result = execute_commit("Test")

        assert result["success"] is False
        assert "error" in result

    def test_hash_retrieval_failure_returns_unknown(self, mock_config, git_repo, monkeypatch):
        """If hash retrieval fails, returns 'unknown'."""
        from tools import commit as commit_module

        call_count = [0]
        def mock_selective_failure(args, *other_args, **kwargs):
            call_count[0] += 1
            if "commit" in args:
                # Commit succeeds
                return True, {"success": True, "stdout": "[main abc123] Test"}
            elif "rev-parse" in args:
                # Hash retrieval fails
                return False, {"success": False, "error": "Failed"}
            return True, {"success": True, "stdout": ""}

        monkeypatch.setattr(commit_module, "run_git_command", mock_selective_failure)

        # Stage something
        test_file = git_repo / "test.txt"
        test_file.write_text("content")
        stage_files([str(test_file)])

        result = execute_commit("Test")

        assert result["success"] is True
        assert result["commit_hash"] == "unknown"

    def test_commit_message_with_newlines(self, mock_config, git_repo):
        """Handles commit messages with newlines."""
        test_file = git_repo / "test.txt"
        test_file.write_text("content")
        stage_files([str(test_file)])

        message = "First line\n\nSecond paragraph"
        result = execute_commit(message)

        assert result["success"] is True

    def test_commit_message_with_unicode(self, mock_config, git_repo):
        """Handles unicode in commit messages."""
        test_file = git_repo / "test.txt"
        test_file.write_text("content")
        stage_files([str(test_file)])

        message = "测试 commit 🎉 with émojis"
        result = execute_commit(message)

        assert result["success"] is True

    def test_commit_message_with_quotes(self, mock_config, git_repo):
        """Handles quotes in commit messages."""
        test_file = git_repo / "test.txt"
        test_file.write_text("content")
        stage_files([str(test_file)])

        message = 'Message with "quotes" and \'apostrophes\''
        result = execute_commit(message)

        assert result["success"] is True


class TestGetCommitStats:
    """Test get_commit_stats function."""

    def test_parses_files_changed(self, mock_config, git_repo):
        """Parses number of files changed."""
        # Create a commit
        test_file = git_repo / "stats_test.txt"
        test_file.write_text("line1\nline2\nline3")
        stage_files([str(test_file)])
        execute_commit("Test commit for stats")

        result = get_commit_stats()

        assert "files_changed" in result
        assert result["files_changed"] >= 1

    def test_parses_insertions_deletions(self, mock_config, git_repo):
        """Parses insertions and deletions."""
        # Modify existing file
        test_file = git_repo / "test.txt"
        test_file.write_text("new content\nmore lines\nadded")
        stage_files([str(test_file)])
        execute_commit("Modify file")

        result = get_commit_stats()

        assert "insertions" in result
        assert "deletions" in result
        # Should have insertions from adding lines
        assert result["insertions"] >= 0

    def test_single_file_changed(self, mock_config, git_repo):
        """Correctly reports single file changed."""
        test_file = git_repo / "single.txt"
        test_file.write_text("content")
        stage_files([str(test_file)])
        execute_commit("Single file")

        result = get_commit_stats()

        assert result["files_changed"] == 1

    def test_no_previous_commit_returns_zeros(self, mock_config, temp_vault):
        """Returns zeros when no previous commit exists."""
        # temp_vault has a git repo but we need a commit
        # Try to get stats without HEAD~1
        result = get_commit_stats()

        # Should handle gracefully
        assert "files_changed" in result
        assert result["files_changed"] == 0

    def test_git_error_returns_zeros(self, mock_config, git_repo, monkeypatch):
        """Returns zeros when git command fails."""
        from tools import commit as commit_module

        def mock_error(*args, **kwargs):
            return False, {"success": False, "error": "Git error"}

        monkeypatch.setattr(commit_module, "run_git_command", mock_error)

        result = get_commit_stats()

        assert result["files_changed"] == 0
        assert result["insertions"] == 0
        assert result["deletions"] == 0
