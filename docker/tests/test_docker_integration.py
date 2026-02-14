"""Docker integration tests for Jarvis MCP servers.

These tests require Docker to be installed and the jarvis image to be built.
Run from repo root:
    docker build -f docker/Dockerfile -t jarvis-test .
    pytest docker/tests/ -v

The tests manage their own container lifecycle (start/stop per session).
"""
import json
import os
import subprocess
import time

import pytest
import requests

DOCKER_IMAGE = os.environ.get("JARVIS_DOCKER_IMAGE", "jarvis-local")
CONTAINER_NAME = "jarvis-integration-test"
CORE_PORT = 18741  # Use non-default ports to avoid conflicts
TODOIST_PORT = 18742
CORE_URL = f"http://localhost:{CORE_PORT}"
MCP_URL = f"{CORE_URL}/mcp"


def _docker_available():
    """Check if Docker is available."""
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=10)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        return False


def _image_exists():
    """Check if the test Docker image exists."""
    try:
        result = subprocess.run(
            ["docker", "image", "inspect", DOCKER_IMAGE],
            capture_output=True, timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


pytestmark = pytest.mark.skipif(
    not (_docker_available() and _image_exists()),
    reason=f"Docker not available or image '{DOCKER_IMAGE}' not built",
)


@pytest.fixture(scope="session")
def docker_container(tmp_path_factory):
    """Start a Docker container for the test session."""
    vault_dir = tmp_path_factory.mktemp("vault")
    config_dir = tmp_path_factory.mktemp("config")

    # Write minimal config
    config = {"vault_path": "/vault", "vault_confirmed": True}
    (config_dir / "config.json").write_text(json.dumps(config))

    # Stop any existing container with same name
    subprocess.run(
        ["docker", "rm", "-f", CONTAINER_NAME],
        capture_output=True, timeout=10,
    )

    # Start container
    subprocess.run(
        [
            "docker", "run", "-d",
            "--name", CONTAINER_NAME,
            "-p", f"{CORE_PORT}:8741",
            "-p", f"{TODOIST_PORT}:8742",
            "-v", f"{vault_dir}:/vault",
            "-v", f"{config_dir}:/config",
            "-e", "JARVIS_HOME=/config",
            "-e", "JARVIS_VAULT_PATH=/vault",
            DOCKER_IMAGE,
        ],
        check=True, capture_output=True, timeout=30,
    )

    # Wait for health
    for _ in range(30):
        try:
            r = requests.get(f"{CORE_URL}/health", timeout=2)
            if r.status_code == 200:
                break
        except requests.ConnectionError:
            pass
        time.sleep(1)
    else:
        logs = subprocess.run(
            ["docker", "logs", CONTAINER_NAME],
            capture_output=True, text=True, timeout=10,
        )
        pytest.fail(f"Container health check timed out.\nLogs:\n{logs.stdout}\n{logs.stderr}")

    yield {"core_url": CORE_URL, "mcp_url": MCP_URL}

    # Cleanup
    subprocess.run(["docker", "rm", "-f", CONTAINER_NAME], capture_output=True, timeout=10)


def _mcp_request(url, method, params=None, request_id=1):
    """Send a JSON-RPC request to the MCP server."""
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or {},
    }
    return requests.post(
        url,
        json=payload,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        },
        timeout=10,
    )


class TestHealthEndpoint:
    def test_health_returns_ok(self, docker_container):
        r = requests.get(f"{docker_container['core_url']}/health", timeout=5)
        assert r.status_code == 200
        data = r.json()
        assert data["status"] == "ok"
        assert data["server"] == "jarvis-core"
        assert "version" in data

    def test_health_json_content_type(self, docker_container):
        r = requests.get(f"{docker_container['core_url']}/health", timeout=5)
        assert "application/json" in r.headers.get("content-type", "")


class TestMCPInitialize:
    def test_initialize_handshake(self, docker_container):
        r = _mcp_request(
            docker_container["mcp_url"],
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.1"},
            },
        )
        assert r.status_code == 200
        data = r.json()
        assert data["jsonrpc"] == "2.0"
        assert "serverInfo" in data["result"]
        assert data["result"]["serverInfo"]["name"] == "core"

    def test_no_trailing_slash_redirect(self, docker_container):
        """POST /mcp should respond directly, not redirect to /mcp/."""
        r = _mcp_request(
            docker_container["mcp_url"],
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.1"},
            },
        )
        # Should NOT have been redirected
        assert r.status_code == 200
        assert len(r.history) == 0, "Request was redirected (307)"

    def test_json_response_format(self, docker_container):
        """Response should be JSON, not SSE."""
        r = _mcp_request(
            docker_container["mcp_url"],
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.1"},
            },
        )
        assert "application/json" in r.headers.get("content-type", "")


class TestToolsList:
    def test_list_tools(self, docker_container):
        # Initialize first (stateless, but needed for MCP protocol)
        _mcp_request(
            docker_container["mcp_url"],
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.1"},
            },
        )

        r = _mcp_request(docker_container["mcp_url"], "tools/list", {}, request_id=2)
        assert r.status_code == 200
        data = r.json()
        tools = data["result"]["tools"]
        tool_names = [t["name"] for t in tools]

        # Should have at least 20 core tools
        assert len(tools) >= 20, f"Expected >= 20 tools, got {len(tools)}: {tool_names}"

        # Verify key tools are present
        for expected in ["jarvis_store", "jarvis_retrieve", "jarvis_status", "jarvis_commit"]:
            assert expected in tool_names, f"Missing tool: {expected}"


class TestVaultOps:
    def test_write_and_read_file(self, docker_container):
        """Write a file to the vault and read it back via MCP tools."""
        # Initialize
        _mcp_request(
            docker_container["mcp_url"],
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0.1"},
            },
        )

        # Write a file
        r = _mcp_request(
            docker_container["mcp_url"],
            "tools/call",
            {
                "name": "jarvis_store",
                "arguments": {
                    "relative_path": "test-docker-file.md",
                    "content": "# Docker Integration Test\n\nThis file was written by an integration test.",
                    "auto_index": False,
                },
            },
            request_id=2,
        )
        assert r.status_code == 200
        write_data = r.json()
        write_text = " ".join(c.get("text", "") for c in write_data["result"]["content"])
        assert "error" not in write_text.lower() or "success" in write_text.lower(), \
            f"Store failed: {write_text[:500]}"

        # Read it back
        r = _mcp_request(
            docker_container["mcp_url"],
            "tools/call",
            {
                "name": "jarvis_read_vault_file",
                "arguments": {"relative_path": "test-docker-file.md"},
            },
            request_id=3,
        )
        assert r.status_code == 200
        data = r.json()
        # Tool results are in content array; flatten all text fields
        content = data["result"]["content"]
        all_text = " ".join(c.get("text", "") for c in content)
        assert "Docker Integration Test" in all_text, f"Expected content not found in: {all_text[:500]}"
