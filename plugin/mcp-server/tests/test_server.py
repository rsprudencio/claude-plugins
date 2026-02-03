"""Integration tests for server.py MCP tool dispatch."""
import pytest
import json


class TestServerIntegration:
    """Test server tool dispatch and error handling."""

    def test_unknown_tool_returns_error(self, mock_config, git_repo):
        """Unknown tool name returns error."""
        from server import call_tool
        import asyncio

        result = asyncio.run(call_tool("unknown_tool", {}))

        assert len(result) == 1
        data = json.loads(result[0].text)
        assert data["success"] is False
        assert "unknown" in data["error"].lower()

    def test_jarvis_commit_validates_inputs(self, mock_config, git_repo):
        """jarvis_commit validates protocol inputs."""
        from server import handle_commit
        import asyncio

        # Invalid operation
        result = asyncio.run(handle_commit({
            "operation": "invalid_op",
            "description": "Test"
        }))

        assert result["success"] is False
        assert "validation_errors" in result

    def test_jarvis_commit_requires_description(self, mock_config, git_repo):
        """jarvis_commit requires description."""
        from server import handle_commit
        import asyncio

        result = asyncio.run(handle_commit({
            "operation": "create",
            "description": ""  # Empty
        }))

        assert result["success"] is False
        assert "validation_errors" in result

    def test_jarvis_commit_validates_entry_id(self, mock_config, git_repo):
        """jarvis_commit validates entry_id format."""
        from server import handle_commit
        import asyncio

        result = asyncio.run(handle_commit({
            "operation": "create",
            "description": "Test",
            "entry_id": "123"  # Invalid format
        }))

        assert result["success"] is False
        assert "validation_errors" in result

    def test_jarvis_debug_config_returns_diagnostics(self, mock_config):
        """jarvis_debug_config returns config diagnostics."""
        from tools.config import get_debug_info

        result = get_debug_info()

        assert "config_path" in result
        assert "config_exists" in result
        assert "resolved_vault_path" in result
        assert "cwd" in result

    def test_vault_tools_use_relative_paths(self, mock_config, git_repo):
        """Vault file tools accept relative paths."""
        from tools.file_ops import write_vault_file, read_vault_file

        # Write with relative path
        write_result = write_vault_file("test_server.txt", "Test content")
        assert write_result["success"] is True

        # Read with relative path
        read_result = read_vault_file("test_server.txt")
        assert read_result["success"] is True
        assert read_result["content"] == "Test content"

    def test_git_tools_run_in_vault(self, mock_config, git_repo):
        """Git tools operate within vault directory."""
        from tools.git_ops import get_status

        result = get_status()

        assert result["success"] is True
        # Should work because vault has git repo

    def test_protocol_validator_catches_errors(self):
        """ProtocolValidator catches invalid inputs."""
        from protocol import ProtocolValidator

        errors = ProtocolValidator.validate_all(
            operation="invalid",
            description="",
            entry_id="bad",
            trigger_mode="wrong"
        )

        assert len(errors) == 4
        assert "operation" in errors
        assert "description" in errors
        assert "entry_id" in errors
        assert "trigger_mode" in errors

    def test_stage_before_commit_flow(self, mock_config, git_repo):
        """Integration: stage files then commit."""
        from tools.commit import stage_files, execute_commit

        # Create file
        test_file = git_repo / "integration_test.txt"
        test_file.write_text("Integration test content")

        # Stage
        stage_result = stage_files([str(test_file)])
        assert stage_result["success"] is True

        # Commit
        commit_result = execute_commit("Integration test commit")
        assert commit_result["success"] is True
        assert "commit_hash" in commit_result

    def test_commit_stats_after_commit(self, mock_config, git_repo):
        """Integration: get stats after making commit."""
        from tools.commit import stage_files, execute_commit, get_commit_stats

        # Create and commit file
        test_file = git_repo / "stats_integration.txt"
        test_file.write_text("line1\nline2\nline3")
        stage_files([str(test_file)])
        execute_commit("Stats test")

        # Get stats
        stats = get_commit_stats()
        assert "files_changed" in stats
        assert stats["files_changed"] >= 1

    def test_parse_after_jarvis_commit(self, mock_config, git_repo):
        """Integration: parse commit after creating JARVIS commit."""
        from tools.commit import stage_files, execute_commit
        from tools.git_ops import parse_last_commit
        from protocol import ProtocolTag, format_commit_message

        # Create JARVIS Protocol commit
        test_file = git_repo / "jarvis_integration.txt"
        test_file.write_text("JARVIS test")
        stage_files([str(test_file)])

        tag = ProtocolTag(operation="create", trigger_mode="conversational")
        message = format_commit_message("create", "Integration test", tag.to_string())
        execute_commit(message)

        # Parse it
        result = parse_last_commit()
        assert result["success"] is True
        assert result["protocol_tag"] is not None
        assert "[JARVIS:" in result["protocol_tag"]
