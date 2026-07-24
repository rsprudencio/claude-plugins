"""Stdlib HTTP client for local Jarvis hook endpoints.

This module intentionally avoids any `tools.*` imports so hook handlers can run
without path-coupling to the MCP server package.
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib import error, parse, request

DEFAULT_BASE_URL = "http://localhost:8741"
DEFAULT_TIMEOUT_SECONDS = 2.5


def _strip_mcp_suffix(url: str) -> str:
    """Normalize configured MCP URL to the server base URL.

    Example:
      http://localhost:8741/mcp -> http://localhost:8741
    """
    parsed = parse.urlsplit(url.strip())
    if not parsed.scheme or not parsed.netloc:
        return DEFAULT_BASE_URL

    path = parsed.path or ""
    if path.endswith("/mcp"):
        path = path[: -len("/mcp")]
    elif path.endswith("/mcp/"):
        path = path[: -len("/mcp/")]
    normalized = parse.urlunsplit(
        (parsed.scheme, parsed.netloc, path.rstrip("/"), "", "")
    )
    return normalized or DEFAULT_BASE_URL


def resolve_base_url(mcp_json_path: str | Path | None = None) -> str:
    """Resolve local Jarvis core base URL from `.mcp.json`.

    Falls back to `http://localhost:8741` on any error.
    """
    if mcp_json_path is None:
        mcp_json_path = Path(__file__).resolve().parent.parent / ".mcp.json"

    path = Path(mcp_json_path)
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        core = data.get("core", {})
        url = core.get("url")
        if isinstance(url, str) and url.strip():
            return _strip_mcp_suffix(url)
    except Exception:
        pass
    return DEFAULT_BASE_URL


def _join_url(base_url: str, endpoint_path: str) -> str:
    return f"{base_url.rstrip('/')}/{endpoint_path.lstrip('/')}"


def _request_json(
    method: str,
    endpoint_path: str,
    payload: dict,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    mcp_json_path: str | Path | None = None,
) -> dict:
    """Send JSON to a local hook endpoint.

    Returns a normalized contract:
      {"success": bool, "data": dict|None, "error": str}
    """
    base_url = resolve_base_url(mcp_json_path)
    url = _join_url(base_url, endpoint_path)

    body = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url=url,
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )

    try:
        with request.urlopen(req, timeout=timeout_seconds) as resp:
            raw = resp.read()
    except error.HTTPError as exc:
        raw = b""
        try:
            raw = exc.read() or b""
        except Exception:
            pass
        detail = ""
        if raw:
            try:
                parsed_body = json.loads(raw.decode("utf-8"))
                detail = parsed_body.get("error") or parsed_body.get("message") or ""
            except Exception:
                detail = raw.decode("utf-8", errors="replace").strip()
        msg = f"http_error:{exc.code}"
        if detail:
            msg = f"{msg}:{detail}"
        return {"success": False, "data": None, "error": msg}
    except Exception as exc:
        return {"success": False, "data": None, "error": str(exc)}

    try:
        decoded = json.loads(raw.decode("utf-8"))
    except Exception:
        return {"success": False, "data": None, "error": "invalid_json_response"}

    if not isinstance(decoded, dict):
        return {"success": False, "data": None, "error": "invalid_response_shape"}
    if decoded.get("success") is False:
        detail = decoded.get("error") or "request_failed"
        return {"success": False, "data": decoded, "error": str(detail)}
    return {"success": True, "data": decoded, "error": ""}


def post_json(
    endpoint_path: str,
    payload: dict,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    mcp_json_path: str | Path | None = None,
) -> dict:
    return _request_json(
        "POST", endpoint_path, payload, timeout_seconds, mcp_json_path
    )


def put_json(
    endpoint_path: str,
    payload: dict,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    mcp_json_path: str | Path | None = None,
) -> dict:
    return _request_json(
        "PUT", endpoint_path, payload, timeout_seconds, mcp_json_path
    )
