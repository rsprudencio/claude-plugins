"""Tests for the HTTP transport wrapper (http_app.py).

Uses Starlette's TestClient which works with any ASGI callable,
including our raw ASGI app (not just Starlette apps).

These tests require the MCP Streamable HTTP SDK module, which is only
available in the Docker environment. They are skipped locally.
"""

import importlib

import pytest

from starlette.testclient import TestClient

try:
    import mcp.server.streamable_http_manager  # noqa: F401

    _HAS_STREAMABLE_HTTP = True
except Exception:
    _HAS_STREAMABLE_HTTP = False

pytestmark = pytest.mark.skipif(
    not _HAS_STREAMABLE_HTTP,
    reason="Streamable HTTP module only available in Docker environment",
)


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
    """GET /health should return minimal liveness response — no DB, no secrets."""
    response = client.get("/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "ok"
    assert data["server"] == "jarvis-core"
    assert "version" in data
    # Health must NOT contain operational details (those live on /telemetry)
    assert "postgres" not in data
    assert "sync" not in data
    assert "auth" not in data


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


def test_hook_prompt_context_success(client, monkeypatch):
    """POST /hook/prompt-context returns endpoint payload."""
    import tools.hook_endpoints as hook_endpoints

    monkeypatch.setattr(
        hook_endpoints,
        "get_prompt_context",
        lambda prompt: {
            "success": True,
            "enabled": True,
            "debug": False,
            "matches": [{"id": "notes/a.md", "relevance": 0.8}],
            "query_ms": 5,
            "total_searched": 10,
            "budget_used": {"local": 10, "vault": 20, "remote": 0, "total": 8000},
            "todoist_prompt_alerts": {"enabled": False, "max_per_category": 3},
        },
    )
    response = client.post("/hook/prompt-context", json={"prompt": "hello"})
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["matches"][0]["id"] == "notes/a.md"


def test_hook_prompt_context_malformed_body(client):
    """Malformed JSON body returns 400."""
    response = client.post(
        "/hook/prompt-context",
        data="{not-json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 400
    assert response.json()["success"] is False


def test_hook_auto_extract_context_success(client, monkeypatch):
    """POST /hook/auto-extract/context returns config + workstreams."""
    import tools.hook_endpoints as hook_endpoints

    monkeypatch.setattr(
        hook_endpoints,
        "get_auto_extract_context",
        lambda workstream_limit: {
            "success": True,
            "auto_extract": {
                "mode": "background",
                "min_turn_chars": 200,
                "max_transcript_lines": 500,
                "max_observations": 3,
                "dedup_threshold": 0.95,
                "debug": False,
            },
            "worklog": {"enabled": True, "dedup_threshold": 0.7},
            "known_workstreams": ["Jarvis Plugin"],
        },
    )
    response = client.post("/hook/auto-extract/context", json={"workstream_limit": 15})
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert "Jarvis Plugin" in data["known_workstreams"]


def test_hook_auto_extract_ingest_statuses(client, monkeypatch):
    """POST /hook/auto-extract/ingest returns stored/duplicate/error statuses."""
    import tools.hook_endpoints as hook_endpoints

    monkeypatch.setattr(
        hook_endpoints,
        "ingest_auto_extract",
        lambda payload: {
            "success": True,
            "observations": [
                {"status": "stored", "id": "obs::1", "error": ""},
                {"status": "duplicate", "id": "", "error": ""},
                {"status": "error", "id": "", "error": "write failed"},
            ],
            "worklog": {"status": "stored", "id": "worklog::1", "error": ""},
        },
    )

    response = client.post(
        "/hook/auto-extract/ingest",
        json={"observations": [], "worklog": None, "context": {}, "dedup": {}},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["success"] is True
    assert data["observations"][0]["status"] == "stored"
    assert data["observations"][1]["status"] == "duplicate"
    assert data["observations"][2]["status"] == "error"


def test_hook_auto_extract_ingest_bad_shape(client):
    """Invalid payload shape returns 400."""
    response = client.post(
        "/hook/auto-extract/ingest",
        json={"observations": "wrong-shape"},
    )
    assert response.status_code == 400
    assert response.json()["success"] is False
