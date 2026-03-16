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
from typing import Any

# Mirror the sys.path setup from server.py so all tool imports resolve
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jarvis_common.auth import authenticate, current_user
from jarvis_common.mtls import patch_uvicorn_transport
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from server import server
from system_prompt import _version as _VERSION

logger = logging.getLogger("jarvis-core")

# Patch uvicorn to expose transport in ASGI scope (required for mTLS CN extraction)
_mtls_patch_ok = patch_uvicorn_transport()
if os.environ.get("JARVIS_TLS_CA") and not _mtls_patch_ok:
    logger.error("JARVIS_TLS_CA is set but uvicorn transport patch failed — cannot verify client certs")
    sys.exit(1)

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


async def _read_request_body(receive) -> bytes:
    """Read full HTTP request body from ASGI receive channel."""
    chunks = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            continue
        body = message.get("body", b"")
        if body:
            chunks.append(body)
        if not message.get("more_body", False):
            break
    return b"".join(chunks)


async def _read_json_body(receive) -> tuple[dict[str, Any] | None, str]:
    """Read and decode JSON body from request.

    Returns:
        (data, error_message). Exactly one is non-empty.
    """
    raw = await _read_request_body(receive)
    if not raw:
        return None, "Request body is required"
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, "Malformed JSON body"
    if not isinstance(data, dict):
        return None, "JSON body must be an object"
    return data, ""


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


# --- Endpoint handlers ---


async def health_response(scope, receive, send):
    """Minimal liveness check — no DB queries, no secrets, no auth required."""
    await _json_response(send, {
        "status": "ok",
        "server": "jarvis-core",
        "version": _VERSION,
    })


async def not_found(scope, receive, send):
    await _json_response(send, {"error": "Not found"}, status=404)


async def telemetry_response(scope, receive, send):
    """GET /telemetry — full operational status (authenticated)."""
    try:
        from jarvis_common.auth import get_auth_config
        from tools.config import get_postgres_config, get_sync_config

        # --- PostgreSQL status ---
        cfg = get_postgres_config()
        url = cfg["url"]
        display_url = url.split("@")[-1] if "@" in url else url

        pg_status = "ok"
        pg_info = {"host": display_url}
        try:
            from tools.schema import execute_query
            count_result = execute_query(
                "SELECT count(*) AS cnt FROM local.memories WHERE status = 'active'",
                fetch="one",
            )
            pg_info["doc_count"] = count_result["cnt"] if count_result else 0
        except Exception as e:
            pg_status = "disconnected"
            pg_info["error"] = str(e)

        data = {
            "status": "ok" if pg_status == "ok" else "degraded",
            "server": "jarvis-core",
            "version": _VERSION,
            "postgres": {**pg_info, "status": pg_status},
        }

        # --- Sync status ---
        sync_cfg = get_sync_config()
        if sync_cfg.get("enabled"):
            from tools.sync_queue import get_queue_stats
            from tools.schema import _get_pool

            remotes = sync_cfg.get("remotes", {})
            try:
                pool = _get_pool()
                queue_stats = get_queue_stats(pool)
            except Exception:
                queue_stats = {"error": "unavailable"}

            data["sync"] = {
                "enabled": True,
                "strategy": sync_cfg.get("strategy", "first-match"),
                "worker_interval_seconds": sync_cfg.get("worker_interval_seconds", 30),
                "remotes": {name: {"configured": True} for name in remotes},
                "queue": queue_stats,
            }
        else:
            data["sync"] = {"enabled": False}

        # --- Auth status ---
        auth_cfg = get_auth_config()
        if auth_cfg is not None:
            tokens = auth_cfg.get("tokens", {})
            mtls_configured = bool(os.environ.get("JARVIS_TLS_CA"))
            data["auth"] = {
                "enabled": True,
                "users": len(tokens) if isinstance(tokens, dict) else 0,
                "mtls": mtls_configured and _mtls_patch_ok,
            }
        else:
            data["auth"] = {"enabled": False}

        await _json_response(send, data)
    except Exception as e:
        await _json_response(send, {"error": str(e)}, status=500)


async def hook_prompt_context_response(scope, receive, send):
    """POST /hook/prompt-context."""
    body, err = await _read_json_body(receive)
    if err:
        await _json_response(send, {"success": False, "error": err}, status=400)
        return

    prompt = body.get("prompt", "")
    if not isinstance(prompt, str):
        await _json_response(
            send,
            {"success": False, "error": "'prompt' must be a string"},
            status=400,
        )
        return

    try:
        from tools.hook_endpoints import get_prompt_context

        response = get_prompt_context(prompt)
    except Exception as e:
        await _json_response(send, {"success": False, "error": str(e)}, status=500)
        return

    await _json_response(send, response)


async def hook_auto_extract_context_response(scope, receive, send):
    """POST /hook/auto-extract/context."""
    body, err = await _read_json_body(receive)
    if err:
        await _json_response(send, {"success": False, "error": err}, status=400)
        return

    workstream_limit = body.get("workstream_limit", 30)
    try:
        workstream_limit = int(workstream_limit)
    except (TypeError, ValueError):
        await _json_response(
            send,
            {"success": False, "error": "'workstream_limit' must be an integer"},
            status=400,
        )
        return

    try:
        from tools.hook_endpoints import get_auto_extract_context

        response = get_auto_extract_context(workstream_limit=workstream_limit)
    except Exception as e:
        await _json_response(send, {"success": False, "error": str(e)}, status=500)
        return

    await _json_response(send, response)


async def hook_auto_extract_ingest_response(scope, receive, send):
    """POST /hook/auto-extract/ingest."""
    body, err = await _read_json_body(receive)
    if err:
        await _json_response(send, {"success": False, "error": err}, status=400)
        return

    # Keep required shape explicit for easier client-side debugging.
    if "observations" in body and not isinstance(body.get("observations"), list):
        await _json_response(
            send,
            {"success": False, "error": "'observations' must be a list"},
            status=400,
        )
        return
    if "worklog" in body and body.get("worklog") is not None and not isinstance(
        body.get("worklog"), dict
    ):
        await _json_response(
            send,
            {"success": False, "error": "'worklog' must be an object or null"},
            status=400,
        )
        return
    if "context" in body and not isinstance(body.get("context"), dict):
        await _json_response(
            send,
            {"success": False, "error": "'context' must be an object"},
            status=400,
        )
        return
    if "dedup" in body and not isinstance(body.get("dedup"), dict):
        await _json_response(
            send,
            {"success": False, "error": "'dedup' must be an object"},
            status=400,
        )
        return

    try:
        from tools.hook_endpoints import ingest_auto_extract

        response = ingest_auto_extract(body)
    except Exception as e:
        await _json_response(send, {"success": False, "error": str(e)}, status=500)
        return

    await _json_response(send, response)


# --- ASGI app ---


async def app(scope, receive, send):
    """ASGI application with path-based routing and opt-in auth.

    Routes:
        GET  /health  -> health check (always open — Docker healthcheck needs it)
        *    /mcp     -> MCP Streamable HTTP
        POST /hook/*  -> hook endpoints
    """
    if scope["type"] == "lifespan":
        await _handle_lifespan(scope, receive, send)
        return

    path = scope.get("path", "")
    method = scope.get("method", "")

    # Health check always open (Docker healthcheck, monitoring)
    if path == "/health" and method == "GET":
        await health_response(scope, receive, send)
        return

    # Auth check for all other endpoints
    username, err = authenticate(scope)
    if err:
        await _send_401(send, err)
        return

    # Set contextvar for downstream use, reset on completion
    token = current_user.set(username)
    try:
        if path == "/telemetry" and method == "GET":
            await telemetry_response(scope, receive, send)
        elif path == "/hook/prompt-context" and method == "POST":
            await hook_prompt_context_response(scope, receive, send)
        elif path == "/hook/auto-extract/context" and method == "POST":
            await hook_auto_extract_context_response(scope, receive, send)
        elif path == "/hook/auto-extract/ingest" and method == "POST":
            await hook_auto_extract_ingest_response(scope, receive, send)
        elif path == "/mcp" or path.startswith("/mcp/"):
            await session_manager.handle_request(scope, receive, send)
        else:
            await not_found(scope, receive, send)
    finally:
        current_user.reset(token)


async def _handle_lifespan(scope, receive, send):
    """Handle ASGI lifespan events (startup/shutdown) with graceful drain."""
    import asyncio
    from server import get_background_tasks

    _run_ctx = None
    _bg_tasks = []
    while True:
        message = await receive()
        if message["type"] == "lifespan.startup":
            # Initialize pgvector schema (idempotent)
            try:
                from tools.schema import ensure_schema, check_model_consistency, ModelMismatchError
                ensure_schema()
                try:
                    check_model_consistency()
                except ModelMismatchError as mme:
                    logger.critical("FATAL: %s", mme)
                    raise SystemExit(1) from mme
            except SystemExit:
                raise
            except Exception as e:
                logger.warning("Schema initialization deferred: %s", e)

            # D6: Rebuild schema registry, auto-discovering existing remote_* schemas
            try:
                from tools.schema_registry import rebuild_registry
                rebuild_registry()
            except Exception as e:
                logger.warning("Schema registry rebuild deferred: %s", e)

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
