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
