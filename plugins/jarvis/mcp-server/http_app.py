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


async def _read_body(receive) -> bytes:
    """Read the full request body from ASGI receive."""
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    return body


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
    """Minimal ASGI response for /health endpoint."""
    await _json_response(
        send, {"status": "ok", "server": "jarvis-core", "version": _VERSION}
    )


async def not_found(scope, receive, send):
    await _json_response(send, {"error": "Not found"}, status=404)


async def bump_retrieval_handler(scope, receive, send):
    """Internal endpoint for hook processes to request retrieval count bumps.

    POST /internal/bump-retrieval
    Body: {"ids": [...], "increment": 0.01}

    Fire-and-forget: hooks call this but don't need the response data.
    The write happens under the chroma_write_lock via _increment_retrieval_counts.
    """
    try:
        body = await _read_body(receive)
        data = json.loads(body)
        ids = data.get("ids", [])
        increment = data.get("increment", 0.01)

        if not ids:
            await _json_response(send, {"ok": True, "bumped": 0})
            return

        from tools.query import _increment_retrieval_counts
        from tools.memory import _get_collection

        collection = _get_collection()
        _increment_retrieval_counts(collection, ids, increment=increment)

        await _json_response(send, {"ok": True, "bumped": len(ids)})
    except Exception as e:
        logger.warning(f"bump-retrieval failed: {e}")
        await _json_response(send, {"ok": False, "error": str(e)}, status=500)


async def store_tier2_handler(scope, receive, send):
    """Internal endpoint for hook processes to store Tier 2 content.

    POST /internal/store-tier2
    Body: {"content": ..., "content_type": ..., ...}  (tier2_write kwargs)

    Used by extract_observation.py to route writes through the MCP server
    instead of writing to ChromaDB directly.
    """
    try:
        body = await _read_body(receive)
        data = json.loads(body)

        from tools.tier2 import tier2_write

        result = tier2_write(**data)
        status = 200 if result.get("success") else 500
        await _json_response(send, result, status=status)
    except Exception as e:
        logger.warning(f"store-tier2 failed: {e}")
        await _json_response(send, {"success": False, "error": str(e)}, status=500)


# --- ASGI app ---


async def app(scope, receive, send):
    """ASGI application with path-based routing.

    Routes:
        GET  /health                    -> health check
        *    /mcp                       -> MCP Streamable HTTP
        POST /internal/bump-retrieval   -> retrieval count bumps (from hooks)
        POST /internal/store-tier2      -> tier2 writes (from hooks)
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
    elif path == "/internal/bump-retrieval" and method == "POST":
        await bump_retrieval_handler(scope, receive, send)
    elif path == "/internal/store-tier2" and method == "POST":
        await store_tier2_handler(scope, receive, send)
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
            from tools.chroma_lock import begin_shutdown

            logger.info("[jarvis] Initiating graceful shutdown — draining writes...")
            begin_shutdown()

            # Cancel background tasks (pattern detection, etc.)
            for task in _bg_tasks:
                if not task.done():
                    task.cancel()

            # Brief wait for any in-flight lock holders to release
            await asyncio.sleep(1.0)
            logger.info("[jarvis] All writes drained — safe to shutdown")

            if _run_ctx:
                await _run_ctx.__aexit__(None, None, None)
            await send({"type": "lifespan.shutdown.complete"})
            return
