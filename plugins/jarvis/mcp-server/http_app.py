"""
Streamable HTTP transport for Jarvis Tools MCP Server.

Thin ASGI wrapper around the existing stdio-based server.py,
enabling Docker deployment via uvicorn.

Usage:
    uvicorn http_app:app --host 0.0.0.0 --port 8741
"""
import contextlib
import json
import os
import sys

# Mirror the sys.path setup from server.py so all tool imports resolve
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from server import server

session_manager = StreamableHTTPSessionManager(
    app=server, stateless=True, json_response=True,
)


async def health_response(scope, receive, send):
    """Minimal ASGI response for /health endpoint."""
    body = json.dumps({"status": "ok", "server": "jarvis-tools"}).encode()
    await send({
        "type": "http.response.start",
        "status": 200,
        "headers": [[b"content-type", b"application/json"]],
    })
    await send({"type": "http.response.body", "body": body})


async def not_found(scope, receive, send):
    body = json.dumps({"error": "Not found"}).encode()
    await send({
        "type": "http.response.start",
        "status": 404,
        "headers": [[b"content-type", b"application/json"]],
    })
    await send({"type": "http.response.body", "body": body})


async def app(scope, receive, send):
    """ASGI application with path-based routing.

    Routes:
        GET  /health  -> health check
        *    /mcp     -> MCP Streamable HTTP (initialize, tool calls, etc.)
    """
    if scope["type"] == "lifespan":
        await _handle_lifespan(scope, receive, send)
        return

    path = scope.get("path", "")

    if path == "/health" and scope.get("method") == "GET":
        await health_response(scope, receive, send)
    elif path == "/mcp" or path.startswith("/mcp/"):
        await session_manager.handle_request(scope, receive, send)
    else:
        await not_found(scope, receive, send)


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
