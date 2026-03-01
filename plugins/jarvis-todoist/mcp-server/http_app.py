"""
Streamable HTTP transport for Jarvis Todoist API MCP Server.

Thin ASGI wrapper around the existing stdio-based server.py,
enabling Docker deployment via uvicorn.

Usage:
    uvicorn http_app:app --host 0.0.0.0 --port 8742
"""

import json
import logging
import os
import sys

# Mirror the sys.path setup from server.py so all tool imports resolve
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jarvis_common.auth import authenticate, current_user
from jarvis_common.mtls import patch_uvicorn_transport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from server import server

logger = logging.getLogger("jarvis-todoist")

# Patch uvicorn to expose transport in ASGI scope (required for mTLS CN extraction)
_mtls_patch_ok = patch_uvicorn_transport()
if os.environ.get("JARVIS_TLS_CA") and not _mtls_patch_ok:
    logger.error("JARVIS_TLS_CA is set but uvicorn transport patch failed — cannot verify client certs")
    sys.exit(1)


def _get_version():
    """Get plugin version from package metadata or JARVIS_VERSION env var."""
    try:
        from importlib.metadata import version

        return version("jarvis-todoist-api")
    except Exception:
        return os.environ.get("JARVIS_VERSION", "unknown")


_VERSION = _get_version()

session_manager = StreamableHTTPSessionManager(
    app=server,
    stateless=True,
    json_response=True,
)


async def _json_response(send, data: dict, status: int = 200):
    """Send a JSON response."""
    body = json.dumps(data).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [[b"content-type", b"application/json"]],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def health_response(scope, receive, send):
    """Minimal ASGI response for /health endpoint."""
    await _json_response(
        send, {"status": "ok", "server": "jarvis-todoist-api", "version": _VERSION}
    )


async def not_found(scope, receive, send):
    await _json_response(send, {"error": "Not found"}, status=404)


async def _send_401(send, message: str):
    """Send a 401 Unauthorized response with WWW-Authenticate header (RFC 7235)."""
    body = json.dumps({"error": message}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                [b"content-type", b"application/json"],
                [b"www-authenticate", b'Bearer realm="jarvis"'],
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


async def app(scope, receive, send):
    """ASGI application with path-based routing and opt-in auth.

    Routes:
        GET  /health  -> health check (always open)
        *    /mcp     -> MCP Streamable HTTP (initialize, tool calls, etc.)
    """
    if scope["type"] == "lifespan":
        await _handle_lifespan(scope, receive, send)
        return

    path = scope.get("path", "")

    # Health check always open (Docker healthcheck, monitoring)
    if path == "/health" and scope.get("method") == "GET":
        await health_response(scope, receive, send)
        return

    # Auth check for all other endpoints
    username, err = authenticate(scope)
    if err:
        await _send_401(send, err)
        return

    token = current_user.set(username)
    try:
        if path == "/mcp" or path.startswith("/mcp/"):
            await session_manager.handle_request(scope, receive, send)
        else:
            await not_found(scope, receive, send)
    finally:
        current_user.reset(token)


async def _handle_lifespan(scope, receive, send):
    """Handle ASGI lifespan events (startup/shutdown)."""
    _run_ctx = None
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            _run_ctx = session_manager.run()
            await _run_ctx.__aenter__()
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            if _run_ctx:
                await _run_ctx.__aexit__(None, None, None)
            await send({"type": "lifespan.shutdown.complete"})
            return
