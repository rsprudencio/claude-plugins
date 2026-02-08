"""Tests for auto_extract_config.py — filtering logic for PostToolUse hook."""
import hashlib
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.auto_extract_config import (
    DEDUP_DIR,
    DEDUP_WINDOW_SECONDS,
    MIN_OUTPUT_LENGTH,
    SKIP_BASH_COMMANDS,
    SKIP_TOOLS,
    check_dedup,
    filter_hook_input,
    should_skip_bash_command,
    should_skip_output,
    should_skip_tool,
)


# ──────────────────────────────────────────────
# TestShouldSkipTool
# ──────────────────────────────────────────────

class TestShouldSkipTool:
    """Tests for should_skip_tool()."""

    def test_skip_claude_internals(self):
        """Claude Code internal tools are skipped."""
        config = {"skip_tools_add": [], "skip_tools_remove": []}
        assert should_skip_tool("Glob", config) is True
        assert should_skip_tool("Read", config) is True
        assert should_skip_tool("Edit", config) is True
        assert should_skip_tool("Write", config) is True
        assert should_skip_tool("Grep", config) is True
        assert should_skip_tool("Task", config) is True

    def test_skip_anti_recursion_tools(self):
        """Tier 2 tools are skipped to prevent infinite loops."""
        config = {"skip_tools_add": [], "skip_tools_remove": []}
        assert should_skip_tool("jarvis_tier2_write", config) is True
        assert should_skip_tool("jarvis_tier2_read", config) is True
        assert should_skip_tool("jarvis_tier2_list", config) is True
        assert should_skip_tool("jarvis_tier2_delete", config) is True
        assert should_skip_tool("jarvis_promote", config) is True

    def test_skip_read_only_jarvis_tools(self):
        """Jarvis read-only tools are skipped."""
        config = {"skip_tools_add": [], "skip_tools_remove": []}
        assert should_skip_tool("jarvis_memory_read", config) is True
        assert should_skip_tool("jarvis_list_vault_dir", config) is True
        assert should_skip_tool("jarvis_debug_config", config) is True

    def test_allow_observable_tools(self):
        """Observable tools (commit, write, push) are NOT skipped."""
        config = {"skip_tools_add": [], "skip_tools_remove": []}
        assert should_skip_tool("jarvis_commit", config) is False
        assert should_skip_tool("jarvis_write_vault_file", config) is False
        assert should_skip_tool("jarvis_push", config) is False
        assert should_skip_tool("jarvis_memory_write", config) is False
        assert should_skip_tool("jarvis_status", config) is False

    def test_allow_bash_tool(self):
        """Bash is NOT in skip list (filtered at command level)."""
        config = {"skip_tools_add": [], "skip_tools_remove": []}
        assert should_skip_tool("Bash", config) is False

    def test_mcp_prefix_stripping(self):
        """MCP-prefixed tool names are matched after stripping prefix."""
        config = {"skip_tools_add": [], "skip_tools_remove": []}
        # Should skip (read-only tool with MCP prefix)
        assert should_skip_tool("mcp__plugin_jarvis_core__jarvis_memory_read", config) is True
        # Should allow (observable tool with MCP prefix)
        assert should_skip_tool("mcp__plugin_jarvis_core__jarvis_commit", config) is False

    def test_user_skip_tools_add(self):
        """User can add tools to skip list."""
        config = {"skip_tools_add": ["jarvis_commit"], "skip_tools_remove": []}
        assert should_skip_tool("jarvis_commit", config) is True

    def test_user_skip_tools_remove(self):
        """User can remove tools from skip list (allow observing)."""
        config = {"skip_tools_add": [], "skip_tools_remove": ["Read"]}
        assert should_skip_tool("Read", config) is False

    def test_add_and_remove_same_tool(self):
        """Remove takes precedence when same tool appears in both lists."""
        # First add, then remove should result in not-skipped
        # (because we add first, then discard)
        config = {"skip_tools_add": ["jarvis_commit"], "skip_tools_remove": ["jarvis_commit"]}
        # add puts it in set, remove discards it
        assert should_skip_tool("jarvis_commit", config) is False

    def test_empty_config(self):
        """Works with minimal config (no override keys)."""
        config = {}
        assert should_skip_tool("Read", config) is True
        assert should_skip_tool("jarvis_commit", config) is False

    def test_todoist_tools_allowed(self):
        """Todoist MCP tools are allowed (not in skip list)."""
        config = {"skip_tools_add": [], "skip_tools_remove": []}
        assert should_skip_tool("mcp__todoist__add-tasks", config) is False
        assert should_skip_tool("mcp__todoist__find-tasks", config) is False
        assert should_skip_tool("mcp__todoist__complete-tasks", config) is False


# ──────────────────────────────────────────────
# TestShouldSkipBashCommand
# ──────────────────────────────────────────────

class TestShouldSkipBashCommand:
    """Tests for should_skip_bash_command()."""

    def test_skip_trivial_commands(self):
        """Basic trivial commands are skipped."""
        assert should_skip_bash_command({"command": "ls"}) is True
        assert should_skip_bash_command({"command": "pwd"}) is True
        assert should_skip_bash_command({"command": "cd"}) is True
        assert should_skip_bash_command({"command": "echo"}) is True

    def test_skip_trivial_with_args(self):
        """Trivial commands with arguments are skipped (prefix match)."""
        assert should_skip_bash_command({"command": "ls -la /some/path"}) is True
        assert should_skip_bash_command({"command": "cat file.txt"}) is True
        assert should_skip_bash_command({"command": "head -20 file.txt"}) is True

    def test_skip_git_readonly(self):
        """Git read-only commands are skipped."""
        assert should_skip_bash_command({"command": "git status"}) is True
        assert should_skip_bash_command({"command": "git log --oneline"}) is True
        assert should_skip_bash_command({"command": "git diff HEAD~1"}) is True
        assert should_skip_bash_command({"command": "git branch -a"}) is True

    def test_allow_complex_commands(self):
        """Complex or write commands are allowed."""
        assert should_skip_bash_command({"command": "pip install anthropic"}) is False
        assert should_skip_bash_command({"command": "python3 -m pytest tests/"}) is False
        assert should_skip_bash_command({"command": "npm run build"}) is False
        assert should_skip_bash_command({"command": "docker compose up"}) is False

    def test_allow_git_write_commands(self):
        """Git write commands are NOT skipped."""
        assert should_skip_bash_command({"command": "git commit -m 'msg'"}) is False
        assert should_skip_bash_command({"command": "git push origin main"}) is False
        assert should_skip_bash_command({"command": "git merge feature"}) is False
        assert should_skip_bash_command({"command": "git rebase main"}) is False

    def test_empty_command(self):
        """Empty command is skipped."""
        assert should_skip_bash_command({"command": ""}) is True
        assert should_skip_bash_command({}) is True

    def test_whitespace_handling(self):
        """Whitespace is stripped before matching."""
        assert should_skip_bash_command({"command": "  ls  "}) is True


# ──────────────────────────────────────────────
# TestShouldSkipOutput
# ──────────────────────────────────────────────

class TestShouldSkipOutput:
    """Tests for should_skip_output()."""

    def test_skip_empty_output(self):
        assert should_skip_output("") is True
        assert should_skip_output(None) is True

    def test_skip_short_output(self):
        """Output shorter than MIN_OUTPUT_LENGTH is skipped."""
        assert should_skip_output("OK") is True
        assert should_skip_output("x" * (MIN_OUTPUT_LENGTH - 1)) is True

    def test_allow_sufficient_output(self):
        """Output at or above threshold is allowed."""
        assert should_skip_output("x" * MIN_OUTPUT_LENGTH) is False
        assert should_skip_output("x" * 1000) is False


# ──────────────────────────────────────────────
# TestCheckDedup
# ──────────────────────────────────────────────

class TestCheckDedup:
    """Tests for check_dedup()."""

    def setup_method(self):
        """Use a test-specific dedup directory."""
        self._original_dedup_dir = DEDUP_DIR
        self._test_dir = Path(tempfile.mkdtemp(prefix="jarvis_test_dedup_"))

    def teardown_method(self):
        """Clean up test dedup directory."""
        import shutil
        shutil.rmtree(self._test_dir, ignore_errors=True)

    def test_first_call_not_duplicate(self):
        """First call for a tool+result is not a duplicate."""
        with patch("tools.auto_extract_config.DEDUP_DIR", self._test_dir):
            assert check_dedup("jarvis_commit", "result content here") is False

    def test_second_call_is_duplicate(self):
        """Same tool+result within window is a duplicate."""
        with patch("tools.auto_extract_config.DEDUP_DIR", self._test_dir):
            check_dedup("jarvis_commit", "result content here")
            assert check_dedup("jarvis_commit", "result content here") is True

    def test_different_tools_not_deduped(self):
        """Different tool names produce different hashes."""
        with patch("tools.auto_extract_config.DEDUP_DIR", self._test_dir):
            check_dedup("jarvis_commit", "result content here")
            assert check_dedup("jarvis_push", "result content here") is False

    def test_different_results_not_deduped(self):
        """Different results produce different hashes."""
        with patch("tools.auto_extract_config.DEDUP_DIR", self._test_dir):
            check_dedup("jarvis_commit", "result A")
            assert check_dedup("jarvis_commit", "result B") is False

    def test_expired_dedup_not_duplicate(self):
        """Expired dedup entries are not considered duplicates."""
        with patch("tools.auto_extract_config.DEDUP_DIR", self._test_dir):
            # Create a sentinel file with old mtime
            content = "jarvis_commit:result content here"[:500]
            full = f"jarvis_commit:{content}"
            hash_hex = hashlib.sha256(full.encode()).hexdigest()[:16]
            sentinel = self._test_dir / hash_hex
            self._test_dir.mkdir(parents=True, exist_ok=True)
            sentinel.touch()
            # Set mtime to past the window
            old_time = time.time() - DEDUP_WINDOW_SECONDS - 10
            os.utime(sentinel, (old_time, old_time))

            assert check_dedup("jarvis_commit", "result content here") is False


# ──────────────────────────────────────────────
# TestFilterHookInput
# ──────────────────────────────────────────────

class TestFilterHookInput:
    """Tests for filter_hook_input() — the main entry point."""

    def setup_method(self):
        """Use a test-specific dedup directory."""
        self._test_dir = Path(tempfile.mkdtemp(prefix="jarvis_test_filter_"))

    def teardown_method(self):
        import shutil
        shutil.rmtree(self._test_dir, ignore_errors=True)

    def test_disabled_mode(self):
        """Disabled mode always skips."""
        config = {"mode": "disabled"}
        hook_data = {
            "tool_name": "jarvis_commit",
            "tool_input": {},
            "tool_result": "x" * 100,
        }
        should_skip, reason = filter_hook_input(hook_data, config)
        assert should_skip is True
        assert reason == "disabled"

    def test_skipped_tool(self):
        """Skipped tools are filtered."""
        config = {"mode": "background", "skip_tools_add": [], "skip_tools_remove": []}
        hook_data = {
            "tool_name": "Read",
            "tool_input": {},
            "tool_result": "x" * 100,
        }
        should_skip, reason = filter_hook_input(hook_data, config)
        assert should_skip is True
        assert "skip_tool" in reason

    def test_trivial_bash_filtered(self):
        """Trivial bash commands are filtered."""
        config = {"mode": "background", "skip_tools_add": [], "skip_tools_remove": []}
        hook_data = {
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "tool_result": "x" * 100,
        }
        should_skip, reason = filter_hook_input(hook_data, config)
        assert should_skip is True
        assert reason == "skip_bash_trivial"

    def test_short_output_filtered(self):
        """Short output is filtered."""
        config = {"mode": "background", "skip_tools_add": [], "skip_tools_remove": []}
        hook_data = {
            "tool_name": "jarvis_commit",
            "tool_input": {},
            "tool_result": "OK",
        }
        should_skip, reason = filter_hook_input(hook_data, config)
        assert should_skip is True
        assert reason == "skip_output_short"

    def test_observable_tool_passes_background(self):
        """Observable tool with sufficient output passes in background mode."""
        config = {"mode": "background", "skip_tools_add": [], "skip_tools_remove": []}
        hook_data = {
            "tool_name": "jarvis_commit",
            "tool_input": {"operation": "create"},
            "tool_result": "Committed: journal entry created " + "x" * 100,
        }
        with patch("tools.auto_extract_config.DEDUP_DIR", self._test_dir):
            should_skip, reason = filter_hook_input(hook_data, config)
        assert should_skip is False
        assert reason == "background"

    def test_observable_tool_passes_inline(self):
        """Observable tool passes in inline mode."""
        config = {"mode": "inline", "skip_tools_add": [], "skip_tools_remove": []}
        hook_data = {
            "tool_name": "jarvis_commit",
            "tool_input": {"operation": "create"},
            "tool_result": "Committed: journal entry created " + "x" * 100,
        }
        with patch("tools.auto_extract_config.DEDUP_DIR", self._test_dir):
            should_skip, reason = filter_hook_input(hook_data, config)
        assert should_skip is False
        assert reason == "inline"

    def test_dedup_filters_repeat(self):
        """Duplicate tool+result within window is filtered."""
        config = {"mode": "background", "skip_tools_add": [], "skip_tools_remove": []}
        hook_data = {
            "tool_name": "jarvis_commit",
            "tool_input": {},
            "tool_result": "Committed: some result " + "x" * 100,
        }
        with patch("tools.auto_extract_config.DEDUP_DIR", self._test_dir):
            # First call passes
            should_skip1, _ = filter_hook_input(hook_data, config)
            assert should_skip1 is False
            # Second identical call is deduped
            should_skip2, reason2 = filter_hook_input(hook_data, config)
            assert should_skip2 is True
            assert reason2 == "dedup"

    def test_non_trivial_bash_passes(self):
        """Non-trivial bash commands pass filtering."""
        config = {"mode": "background", "skip_tools_add": [], "skip_tools_remove": []}
        hook_data = {
            "tool_name": "Bash",
            "tool_input": {"command": "python3 -m pytest tests/ -v"},
            "tool_result": "PASSED: 42 tests " + "x" * 100,
        }
        with patch("tools.auto_extract_config.DEDUP_DIR", self._test_dir):
            should_skip, reason = filter_hook_input(hook_data, config)
        assert should_skip is False
        assert reason == "background"


# ──────────────────────────────────────────────
# TestGetAutoExtractConfig
# ──────────────────────────────────────────────

class TestGetAutoExtractConfig:
    """Tests for get_auto_extract_config()."""

    def test_defaults(self, mock_config):
        """Returns sensible defaults when no config is set."""
        from tools.config import get_auto_extract_config
        config = get_auto_extract_config()
        assert config["mode"] == "background"
        assert config["max_observations_per_session"] == 100
        assert config["skip_tools_add"] == []
        assert config["skip_tools_remove"] == []

    def test_mode_override(self, mock_config):
        """User can override mode via config."""
        mock_config.set(memory={"auto_extract": {"mode": "disabled"}})
        from tools.config import get_auto_extract_config
        config = get_auto_extract_config()
        assert config["mode"] == "disabled"

    def test_inline_mode(self, mock_config):
        """User can set inline mode."""
        mock_config.set(memory={"auto_extract": {"mode": "inline"}})
        from tools.config import get_auto_extract_config
        config = get_auto_extract_config()
        assert config["mode"] == "inline"

    def test_partial_override(self, mock_config):
        """Partial config merges with defaults."""
        mock_config.set(memory={"auto_extract": {"skip_tools_add": ["Bash"]}})
        from tools.config import get_auto_extract_config
        config = get_auto_extract_config()
        assert config["mode"] == "background"  # default preserved
        assert config["skip_tools_add"] == ["Bash"]  # override applied
