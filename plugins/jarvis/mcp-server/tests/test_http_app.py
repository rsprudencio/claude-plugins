"""Tests for the HTTP transport wrapper (http_app.py).

Uses Starlette's TestClient which works with any ASGI callable,
including our raw ASGI app (not just Starlette apps).
"""
import importlib

import pytest
from starlette.testclient import TestClient


@pytest.fixture
def client():
    """Create a test client for the raw ASGI app.

    We reload the module per test because StreamableHTTPSessionManager
    can only run() once per instance.
    """
    import http_app as mod

    importlib.reload(mod)
    with TestClient(mod.app, raise_server_exceptions=False) as c:
        yield c


def test_app_creates_successfully():
    """The ASGI app should import without errors."""
    from http_app import app

    assert callable(app)


def test_health_endpoint(client):
    """GET /health should return status ok with server name."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["server"] == "jarvis-tools"


def test_not_found(client):
    """Unknown paths should return 404."""
    response = client.get("/unknown")
    assert response.status_code == 404
    assert response.json()["error"] == "Not found"


def test_mcp_endpoint_accepts_post(client):
    """POST /mcp should accept JSON-RPC requests (initialize handshake)."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0.1"},
        },
    }
    response = client.post(
        "/mcp",
        json=payload,
        headers={"Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["jsonrpc"] == "2.0"
    assert data["result"]["serverInfo"]["name"] == "core"


def test_mcp_no_trailing_slash_redirect(client):
    """POST /mcp should NOT redirect to /mcp/ (the raw ASGI fix)."""
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "0.1"},
        },
    }
    response = client.post(
        "/mcp",
        json=payload,
        headers={"Accept": "application/json, text/event-stream"},
        follow_redirects=False,
    )
    # Should be 200, NOT 307 (trailing slash redirect)
    assert response.status_code == 200
