"""
Streamable HTTP transport for Jarvis Core MCP Server.

Thin ASGI wrapper around the existing stdio-based server.py,
enabling Docker deployment via uvicorn.

Usage:
    uvicorn http_app:app --host 0.0.0.0 --port 8741
"""

import json
import logging
import os
import sys

# Mirror the sys.path setup from server.py so all tool imports resolve
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from server import server

logger = logging.getLogger("jarvis-core")


def _get_version():
    """Get plugin version from package metadata or JARVIS_VERSION env var."""
    try:
        from importlib.metadata import version

        return version("jarvis-core")
    except Exception:
        return os.environ.get("JARVIS_VERSION", "unknown")


_VERSION = _get_version()

session_manager = StreamableHTTPSessionManager(
    app=server,
    stateless=True,
    json_response=True,
)


# --- ASGI helpers ---


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


# --- Endpoint handlers ---


async def health_response(scope, receive, send):
    """ASGI response for /health endpoint with ChromaDB status."""
    from tools.chroma_telemetry import chromadb_health
    from tools.config import get_chroma_config

    cfg = get_chroma_config()
    data = {
        "status": "ok",
        "server": "jarvis-core",
        "version": _VERSION,
        "chromadb": {
            "host": cfg["host"],
            "port": cfg["port"],
            **{k: v for k, v in chromadb_health.items() if v is not None},
        },
    }
    # Degrade top-level status if ChromaDB is unhealthy
    if chromadb_health.get("status") in ("degraded", "disconnected"):
        data["status"] = "degraded"
    await _json_response(send, data)


async def not_found(scope, receive, send):
    await _json_response(send, {"error": "Not found"}, status=404)


# --- ASGI app ---


async def app(scope, receive, send):
    """ASGI application with path-based routing.

    Routes:
        GET  /health  -> health check
        *    /mcp     -> MCP Streamable HTTP
    """
    if scope["type"] == "lifespan":
        await _handle_lifespan(scope, receive, send)
        return

    path = scope.get("path", "")
    method = scope.get("method", "")

    if path == "/health" and method == "GET":
        await health_response(scope, receive, send)
    elif path == "/mcp" or path.startswith("/mcp/"):
        await session_manager.handle_request(scope, receive, send)
    else:
        await not_found(scope, receive, send)


async def _handle_lifespan(scope, receive, send):
    """Handle ASGI lifespan events (startup/shutdown) with graceful drain."""
    import asyncio
    from server import get_background_tasks

    _run_ctx = None
    _bg_tasks = []
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            _run_ctx = session_manager.run()
            await _run_ctx.__aenter__()
            _bg_tasks = [asyncio.create_task(t) for t in get_background_tasks()]
            await send({"type": "lifespan.startup.complete"})
        elif message["type"] == "lifespan.shutdown":
            logger.info("[jarvis] Shutting down — cancelling background tasks...")

            # Cancel background tasks (pattern detection, health probe, etc.)
            for task in _bg_tasks:
                if not task.done():
                    task.cancel()

            if _run_ctx:
                await _run_ctx.__aexit__(None, None, None)
            await send({"type": "lifespan.shutdown.complete"})
            return
