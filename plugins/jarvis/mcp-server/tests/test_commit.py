"""Tests for commit.py git commit operations."""
import pytest

from tools.commit import (
    stage_files, execute_commit, get_commit_stats,
    get_committed_files, reindex_committed_files,
)


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


class TestGetCommittedFiles:
    """Test get_committed_files function."""

    def test_returns_committed_files(self, mock_config, git_repo):
        """Returns list of files from last commit."""
        f1 = git_repo / "a.md"
        f2 = git_repo / "b.txt"
        f1.write_text("# A")
        f2.write_text("B")
        stage_files([str(f1), str(f2)])
        execute_commit("Two files")

        result = get_committed_files()

        assert "a.md" in result
        assert "b.txt" in result

    def test_empty_on_git_error(self, mock_config, git_repo, monkeypatch):
        """Returns empty list when git command fails."""
        from tools import commit as commit_module

        monkeypatch.setattr(
            commit_module, "run_git_command",
            lambda *a, **kw: (False, {"success": False}),
        )
        assert get_committed_files() == []


class TestReindexCommittedFiles:
    """Test reindex_committed_files — full commit-to-ChromaDB flow."""

    def test_reindexes_md_files(self, mock_config, git_repo):
        """Committed .md files are indexed into ChromaDB."""
        from tools.memory import _get_collection

        md_file = git_repo / "notes" / "reindex-test.md"
        md_file.write_text("# Reindex Test\n\nThis should appear in ChromaDB.")
        stage_files([str(md_file)])
        execute_commit("Add reindex test note")

        result = reindex_committed_files()

        assert "notes/reindex-test.md" in result["reindexed"]

        # Verify actually in ChromaDB
        collection = _get_collection()
        results = collection.get(where={"parent_file": "notes/reindex-test.md"})
        assert len(results["ids"]) >= 1
        assert "Reindex Test" in results["documents"][0]

    def test_skips_non_md_files(self, mock_config, git_repo):
        """Non-markdown files are not reindexed."""
        txt_file = git_repo / "data.txt"
        txt_file.write_text("plain text")
        stage_files([str(txt_file)])
        execute_commit("Add txt file")

        result = reindex_committed_files()

        assert result["reindexed"] == []
        assert result["unindexed"] == []

    def test_mixed_files_only_indexes_md(self, mock_config, git_repo):
        """Only .md files reindexed when commit has mixed types."""
        from tools.memory import _get_collection

        md = git_repo / "notes" / "mixed.md"
        txt = git_repo / "config.yaml"
        md.write_text("# Mixed Test\n\nMarkdown content.")
        txt.write_text("key: value")
        stage_files([str(md), str(txt)])
        execute_commit("Mixed file commit")

        result = reindex_committed_files()

        assert "notes/mixed.md" in result["reindexed"]
        assert len(result["reindexed"]) == 1

        collection = _get_collection()
        results = collection.get(where={"parent_file": "notes/mixed.md"})
        assert len(results["ids"]) >= 1

    def test_handles_index_failure_gracefully(self, mock_config, git_repo, monkeypatch):
        """Indexing failures are logged, not raised."""
        from tools import memory as memory_module

        md_file = git_repo / "notes" / "fail-test.md"
        md_file.write_text("# Fail Test")
        stage_files([str(md_file)])
        execute_commit("Add failing file")

        # Make index_file always fail
        monkeypatch.setattr(
            memory_module, "index_file",
            lambda path: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        # Should not raise
        result = reindex_committed_files()
        assert result["reindexed"] == []

    def test_reindex_updates_existing_content(self, mock_config, git_repo):
        """Editing a file and recommitting updates the ChromaDB entry."""
        from tools.memory import _get_collection

        md_file = git_repo / "notes" / "evolving.md"
        md_file.write_text("# Version 1\n\nOriginal content.")
        stage_files([str(md_file)])
        execute_commit("Create evolving note")
        reindex_committed_files()

        # Now edit it
        md_file.write_text("# Version 2\n\nUpdated content.")
        stage_files([str(md_file)])
        execute_commit("Update evolving note")
        result = reindex_committed_files()

        assert "notes/evolving.md" in result["reindexed"]

        collection = _get_collection()
        results = collection.get(where={"parent_file": "notes/evolving.md"})
        assert len(results["ids"]) >= 1
        assert "Version 2" in results["documents"][0]
        assert "Version 1" not in results["documents"][0]

    def test_unindexes_deleted_md_files(self, mock_config, git_repo):
        """Deleted .md files are removed from ChromaDB."""
        import os
        from tools.memory import _get_collection, index_file

        # Create and index a file
        md_file = git_repo / "notes" / "doomed.md"
        md_file.write_text("# Doomed File\n\nThis will be deleted.")
        stage_files([str(md_file)])
        execute_commit("Create doomed note")
        reindex_committed_files()

        # Verify it's in ChromaDB
        collection = _get_collection()
        results = collection.get(where={"parent_file": "notes/doomed.md"})
        assert len(results["ids"]) >= 1

        # Delete the file and commit
        os.remove(str(md_file))
        os.system(f'cd {git_repo} && git add -A')
        execute_commit("Delete doomed note")

        result = reindex_committed_files()

        assert "notes/doomed.md" in result["unindexed"]
        assert result["reindexed"] == []

        # Verify removed from ChromaDB
        results = collection.get(where={"parent_file": "notes/doomed.md"})
        assert len(results["ids"]) == 0

    def test_mixed_create_and_delete(self, mock_config, git_repo):
        """Single commit with both created and deleted files syncs both."""
        import os
        from tools.memory import _get_collection

        # Create first file and commit
        old_file = git_repo / "notes" / "old.md"
        old_file.write_text("# Old File\n\nWill be replaced.")
        stage_files([str(old_file)])
        execute_commit("Create old note")
        reindex_committed_files()

        # Verify old file indexed
        collection = _get_collection()
        results = collection.get(where={"parent_file": "notes/old.md"})
        assert len(results["ids"]) >= 1

        # Now delete old and create new in one commit
        os.remove(str(old_file))
        new_file = git_repo / "notes" / "new.md"
        new_file.write_text("# New File\n\nFresh content.")
        os.system(f'cd {git_repo} && git add -A')
        execute_commit("Replace old with new")

        result = reindex_committed_files()

        assert "notes/old.md" in result["unindexed"]
        assert "notes/new.md" in result["reindexed"]

        # Old gone from ChromaDB
        results = collection.get(where={"parent_file": "notes/old.md"})
        assert len(results["ids"]) == 0

        # New present in ChromaDB
        results = collection.get(where={"parent_file": "notes/new.md"})
        assert len(results["ids"]) >= 1
