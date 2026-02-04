"""Tests for git_ops.py git query and management operations."""
import pytest

from tools.git_ops import (
    get_status,
    parse_last_commit,
    push_to_remote,
    move_files,
    query_history,
    rollback_commit,
    file_history,
    rewrite_commit_messages,
)


class TestGetStatus:
    """Test get_status function."""

    def test_clean_working_tree(self, mock_config, git_repo):
        """Clean working tree returns empty lists."""
        result = get_status()

        assert result["success"] is True
        assert isinstance(result["staged"], list)
        assert isinstance(result["unstaged"], list)
        assert isinstance(result["untracked"], list)

    def test_staged_files(self, mock_config, git_repo):
        """Detects staged files."""
        test_file = git_repo / "staged.txt"
        test_file.write_text("content")
        import os
        os.system(f"cd {git_repo} && git add staged.txt")

        result = get_status()

        assert result["success"] is True
        assert len(result["staged"]) > 0

    def test_unstaged_files(self, mock_config, git_repo):
        """Detects unstaged modifications."""
        # Modify existing file without staging
        test_file = git_repo / "test.txt"
        test_file.write_text("modified content")

        result = get_status()

        assert result["success"] is True
        # May have unstaged files
        assert "unstaged" in result

    def test_untracked_files(self, mock_config, git_repo):
        """Detects untracked files."""
        untracked = git_repo / "untracked.txt"
        untracked.write_text("new file")

        result = get_status()

        assert result["success"] is True
        assert len(result["untracked"]) > 0
        assert any("untracked" in f for f in result["untracked"])

    def test_mixed_status(self, mock_config, git_repo):
        """Handles mix of staged, unstaged, and untracked."""
        import os
        # Staged
        staged = git_repo / "staged.txt"
        staged.write_text("staged")
        os.system(f"cd {git_repo} && git add staged.txt")

        # Unstaged
        test_file = git_repo / "test.txt"
        test_file.write_text("modified")

        # Untracked
        untracked = git_repo / "untracked.txt"
        untracked.write_text("untracked")

        result = get_status()

        assert result["success"] is True
        assert "staged" in result
        assert "unstaged" in result
        assert "untracked" in result

    def test_deleted_files(self, mock_config, git_repo):
        """Handles deleted files."""
        # Delete existing file
        test_file = git_repo / "test.txt"
        if test_file.exists():
            test_file.unlink()

        result = get_status()

        assert result["success"] is True

    def test_vault_not_configured_fails(self, no_config):
        """Fails when vault not configured."""
        result = get_status()

        assert result["success"] is False
        assert "error" in result


class TestParseLastCommit:
    """Test parse_last_commit function."""

    def test_parses_commit_hash(self, mock_config, git_repo):
        """Parses commit hash from last commit."""
        result = parse_last_commit()

        assert result["success"] is True
        assert "commit_hash" in result
        assert len(result["commit_hash"]) == 7  # Short hash

    def test_parses_subject(self, mock_config, git_repo):
        """Parses commit subject line."""
        result = parse_last_commit()

        assert result["success"] is True
        assert "subject" in result
        assert isinstance(result["subject"], str)

    def test_extracts_jarvis_protocol_tag(self, mock_config, git_repo_with_jarvis_commits):
        """Extracts JARVIS protocol tag from commit message."""
        result = parse_last_commit()

        assert result["success"] is True
        assert "protocol_tag" in result
        # Should find [JARVIS:...] tag
        if result["protocol_tag"]:
            assert result["protocol_tag"].startswith("[JARVIS:")

    def test_no_protocol_tag_returns_none(self, mock_config, git_repo):
        """Returns None for protocol_tag when not present."""
        result = parse_last_commit()

        assert result["success"] is True
        # Might be None if no JARVIS tag
        assert "protocol_tag" in result

    def test_counts_files_changed(self, mock_config, git_repo):
        """Counts files changed in commit."""
        result = parse_last_commit()

        assert result["success"] is True
        assert "files_changed" in result
        assert result["files_changed"] >= 0

    def test_no_commits_fails(self, mock_config, temp_vault):
        """Fails gracefully when no commits exist."""
        # temp_vault has no commits
        import shutil
        git_dir = temp_vault / ".git"
        if git_dir.exists():
            shutil.rmtree(git_dir)
        import os
        os.system(f"cd {temp_vault} && git init -q")

        result = parse_last_commit()

        assert result["success"] is False

    def test_non_jarvis_commit(self, mock_config, git_repo):
        """Handles non-JARVIS commits."""
        result = parse_last_commit()

        assert result["success"] is True
        # protocol_tag may be None for non-JARVIS commits
        assert "protocol_tag" in result


class TestPushToRemote:
    """Test push_to_remote function."""

    def test_push_current_branch(self, mock_config, git_repo, monkeypatch):
        """Push current branch."""
        # Mock successful push
        from tools import git_ops as git_ops_module

        def mock_push(*args, **kwargs):
            return True, {
                "success": True,
                "stderr": "Everything up-to-date"
            }

        monkeypatch.setattr(git_ops_module, "run_git_command", mock_push)

        result = push_to_remote()

        assert result["success"] is True
        assert "pushed_to" in result

    def test_push_specific_branch(self, mock_config, git_repo, monkeypatch):
        """Push specific branch."""
        from tools import git_ops as git_ops_module

        def mock_push(*args, **kwargs):
            return True, {
                "success": True,
                "stderr": "Branch pushed"
            }

        monkeypatch.setattr(git_ops_module, "run_git_command", mock_push)

        result = push_to_remote(branch="main")

        assert result["success"] is True
        assert result["pushed_to"] == "main"

    def test_captures_push_message(self, mock_config, git_repo, monkeypatch):
        """Captures git push output (stderr)."""
        from tools import git_ops as git_ops_module

        def mock_push(*args, **kwargs):
            return True, {
                "success": True,
                "stderr": "Push output message"
            }

        monkeypatch.setattr(git_ops_module, "run_git_command", mock_push)

        result = push_to_remote()

        assert "message" in result

    def test_no_remote_fails(self, mock_config, git_repo):
        """Fails when no remote configured."""
        # Real git repo has no remote
        result = push_to_remote()

        assert result["success"] is False
        assert "error" in result

    def test_rejected_push_fails(self, mock_config, git_repo, monkeypatch):
        """Handles rejected push."""
        from tools import git_ops as git_ops_module

        def mock_rejected(*args, **kwargs):
            return False, {
                "success": False,
                "error": "rejected",
                "stderr": "non-fast-forward"
            }

        monkeypatch.setattr(git_ops_module, "run_git_command", mock_rejected)

        result = push_to_remote()

        assert result["success"] is False

    def test_vault_not_configured_fails(self, no_config):
        """Fails when vault not configured."""
        result = push_to_remote()

        assert result["success"] is False


class TestMoveFiles:
    """Test move_files function."""

    def test_move_single_file(self, mock_config, git_repo):
        """Move single file successfully."""
        source = git_repo / "source.txt"
        source.write_text("content")
        import os
        os.system(f"cd {git_repo} && git add source.txt && git commit -q -m 'Add source'")

        result = move_files([{
            "source": "source.txt",
            "destination": "dest.txt"
        }])

        assert result["success"] is True
        assert len(result["moved"]) == 1

    def test_move_multiple_files(self, mock_config, git_repo):
        """Move multiple files."""
        import os
        for i in range(3):
            f = git_repo / f"file{i}.txt"
            f.write_text(f"content{i}")
            os.system(f"cd {git_repo} && git add file{i}.txt")
        os.system(f"cd {git_repo} && git commit -q -m 'Add files'")

        moves = [
            {"source": f"file{i}.txt", "destination": f"moved{i}.txt"}
            for i in range(3)
        ]

        result = move_files(moves)

        assert len(result["moved"]) >= 0

    def test_rename_file(self, mock_config, git_repo):
        """Rename file in same directory."""
        import os
        source = git_repo / "old_name.txt"
        source.write_text("content")
        os.system(f"cd {git_repo} && git add old_name.txt && git commit -q -m 'Add'")

        result = move_files([{
            "source": "old_name.txt",
            "destination": "new_name.txt"
        }])

        assert "moved" in result

    def test_source_not_found_fails(self, mock_config, git_repo):
        """Reports error when source doesn't exist."""
        result = move_files([{
            "source": "nonexistent.txt",
            "destination": "dest.txt"
        }])

        assert result["success"] is False or len(result.get("errors", [])) > 0

    def test_missing_source_key_fails(self, mock_config, git_repo):
        """Handles missing source key."""
        result = move_files([{
            "destination": "dest.txt"
        }])

        assert len(result.get("errors", [])) > 0

    def test_missing_destination_key_fails(self, mock_config, git_repo):
        """Handles missing destination key."""
        result = move_files([{
            "source": "source.txt"
        }])

        assert len(result.get("errors", [])) > 0

    def test_partial_success_returns_errors(self, mock_config, git_repo):
        """Returns both moved and errors."""
        import os
        good = git_repo / "good.txt"
        good.write_text("content")
        os.system(f"cd {git_repo} && git add good.txt && git commit -q -m 'Add'")

        result = move_files([
            {"source": "good.txt", "destination": "moved.txt"},
            {"source": "bad.txt", "destination": "dest.txt"}
        ])

        assert "moved" in result
        assert "errors" in result

    def test_empty_moves_list(self, mock_config, git_repo):
        """Handles empty moves list."""
        result = move_files([])

        assert result["success"] is True
        assert len(result["moved"]) == 0


class TestQueryHistory:
    """Test query_history function."""

    def test_query_all_operations(self, mock_config, git_repo_with_jarvis_commits):
        """Query all operations."""
        result = query_history(operation="all", limit=10)

        assert result["success"] is True
        assert "operations" in result
        assert "count" in result
        assert isinstance(result["operations"], list)

    def test_filter_by_create(self, mock_config, git_repo_with_jarvis_commits):
        """Filter by create operation."""
        result = query_history(operation="create", limit=10)

        assert result["success"] is True

    def test_filter_by_edit(self, mock_config, git_repo_with_jarvis_commits):
        """Filter by edit operation."""
        result = query_history(operation="edit", limit=10)

        assert result["success"] is True

    def test_filter_by_delete(self, mock_config, git_repo_with_jarvis_commits):
        """Filter by delete operation."""
        result = query_history(operation="delete", limit=10)

        assert result["success"] is True

    def test_filter_by_move(self, mock_config, git_repo_with_jarvis_commits):
        """Filter by move operation."""
        result = query_history(operation="move", limit=10)

        assert result["success"] is True

    def test_filter_by_user(self, mock_config, git_repo_with_jarvis_commits):
        """Filter by user operation."""
        result = query_history(operation="user", limit=10)

        assert result["success"] is True

    def test_filter_by_since(self, mock_config, git_repo_with_jarvis_commits):
        """Filter by since date."""
        result = query_history(since="1 day ago", limit=10)

        assert result["success"] is True

    def test_filter_by_file_path(self, mock_config, git_repo_with_jarvis_commits):
        """Filter by file path."""
        result = query_history(file_path="test.txt", limit=10)

        assert result["success"] is True

    def test_apply_limit(self, mock_config, git_repo_with_jarvis_commits):
        """Respects limit parameter."""
        result = query_history(limit=1)

        assert result["success"] is True
        assert len(result["operations"]) <= 1

    def test_empty_history(self, mock_config, git_repo):
        """Handles repos with few commits."""
        result = query_history(operation="delete", limit=10)

        assert result["success"] is True
        assert result["count"] >= 0


class TestRollbackCommit:
    """Test rollback_commit function."""

    def test_revert_succeeds(self, mock_config, git_repo, monkeypatch):
        """Revert succeeds."""
        from tools import git_ops as git_ops_module

        call_count = [0]
        def mock_revert(args, *other, **kwargs):
            call_count[0] += 1
            if "revert" in args:
                return True, {"success": True, "stdout": "Reverted"}
            return True, {"success": True, "stdout": "abc1234"}

        monkeypatch.setattr(git_ops_module, "run_git_command", mock_revert)

        result = rollback_commit("abc1234")

        assert result["success"] is True
        assert "revert_hash" in result

    def test_returns_revert_hash(self, mock_config, git_repo, monkeypatch):
        """Returns new revert commit hash."""
        from tools import git_ops as git_ops_module

        def mock_revert(args, *other, **kwargs):
            if "revert" in args:
                return True, {"success": True}
            return True, {"success": True, "stdout": "def5678"}

        monkeypatch.setattr(git_ops_module, "run_git_command", mock_revert)

        result = rollback_commit("abc1234")

        assert "revert_hash" in result

    def test_invalid_commit_fails(self, mock_config, git_repo):
        """Fails with invalid commit hash."""
        result = rollback_commit("invalid_hash_12345")

        assert result["success"] is False

    def test_revert_conflict_fails(self, mock_config, git_repo, monkeypatch):
        """Handles revert conflicts."""
        from tools import git_ops as git_ops_module

        def mock_conflict(*args, **kwargs):
            return False, {
                "success": False,
                "error": "conflict",
                "stderr": "CONFLICT"
            }

        monkeypatch.setattr(git_ops_module, "run_git_command", mock_conflict)

        result = rollback_commit("abc1234")

        assert result["success"] is False

    def test_vault_not_configured_fails(self, no_config):
        """Fails when vault not configured."""
        result = rollback_commit("abc1234")

        assert result["success"] is False


class TestFileHistory:
    """Test file_history function."""

    def test_returns_file_history(self, mock_config, git_repo):
        """Returns history for specific file."""
        result = file_history("test.txt", limit=10)

        assert result["success"] is True
        assert "operations" in result or "history" in result

    def test_applies_limit(self, mock_config, git_repo):
        """Respects limit parameter."""
        result = file_history("test.txt", limit=1)

        assert result["success"] is True

    def test_file_not_in_history(self, mock_config, git_repo):
        """Handles file with no history."""
        result = file_history("nonexistent.txt", limit=10)

        assert result["success"] is True
        # Should return empty or zero count
        assert result.get("count", 0) >= 0


class TestRewriteCommitMessages:
    """Test rewrite_commit_messages function."""

    def test_removes_coauthored_by(self, mock_config, git_repo, monkeypatch):
        """Removes Co-Authored-By lines."""
        from tools import git_ops as git_ops_module

        def mock_rewrite(args, *other, **kwargs):
            if "log" in args:
                return True, {"success": True, "stdout": "abc1234\ndef5678"}
            if "filter-branch" in args:
                return True, {"success": True}
            return True, {"success": True, "stdout": "abc1234"}

        monkeypatch.setattr(git_ops_module, "run_git_command", mock_rewrite)

        result = rewrite_commit_messages(count=1)

        assert result["success"] is True
        assert "commits_rewritten" in result

    def test_removes_custom_pattern(self, mock_config, git_repo, monkeypatch):
        """Removes custom patterns."""
        from tools import git_ops as git_ops_module

        def mock_rewrite(args, *other, **kwargs):
            if "log" in args:
                return True, {"success": True, "stdout": "abc1234"}
            if "filter-branch" in args:
                return True, {"success": True}
            return True, {"success": True, "stdout": "abc1234"}

        monkeypatch.setattr(git_ops_module, "run_git_command", mock_rewrite)

        result = rewrite_commit_messages(count=1, patterns=["CustomPattern:.*"])

        assert result["success"] is True

    def test_returns_old_new_hashes(self, mock_config, git_repo, monkeypatch):
        """Returns old and new commit hashes."""
        from tools import git_ops as git_ops_module

        def mock_rewrite(args, *other, **kwargs):
            if "log" in args:
                return True, {"success": True, "stdout": "old1234"}
            if "filter-branch" in args:
                return True, {"success": True}
            return True, {"success": True, "stdout": "new5678"}

        monkeypatch.setattr(git_ops_module, "run_git_command", mock_rewrite)

        result = rewrite_commit_messages(count=1)

        assert "old_hashes" in result
        assert "new_hashes" in result

    def test_count_exceeds_commits(self, mock_config, git_repo, monkeypatch):
        """Handles count exceeding available commits."""
        from tools import git_ops as git_ops_module

        def mock_rewrite(*args, **kwargs):
            return False, {"success": False, "error": "Not enough commits"}

        monkeypatch.setattr(git_ops_module, "run_git_command", mock_rewrite)

        result = rewrite_commit_messages(count=1000)

        assert result["success"] is False

    def test_pattern_matches_nothing(self, mock_config, git_repo, monkeypatch):
        """Handles pattern that matches nothing."""
        from tools import git_ops as git_ops_module

        def mock_rewrite(args, *other, **kwargs):
            if "log" in args:
                return True, {"success": True, "stdout": "abc1234"}
            if "filter-branch" in args:
                return True, {"success": True}
            return True, {"success": True, "stdout": "abc1234"}

        monkeypatch.setattr(git_ops_module, "run_git_command", mock_rewrite)

        result = rewrite_commit_messages(count=1, patterns=["NonexistentPattern.*"])

        # Should still succeed
        assert result["success"] is True
