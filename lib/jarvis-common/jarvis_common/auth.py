"""Shared authentication module for Jarvis MCP servers.

Provides opt-in authentication with:
- mTLS client certificate CN extraction (highest priority, cryptographic identity)
- SHA-256 hashed Bearer tokens at rest (fallback for non-TLS / programmatic access)
- Constant-time comparison via hmac.compare_digest (timing side-channel prevention)
- Request-scoped user identity via contextvars (async-safe, no signature pollution)
- Internal token support for hook scripts running inside the container
- Emergency CN blocklist via server.auth.denied_cns config
"""

import contextvars
import hashlib
import hmac
import logging
import os

from .config import get_config

logger = logging.getLogger("jarvis-auth")

# Request-scoped user identity — set by HTTP middleware, read by tool handlers
current_user: contextvars.ContextVar[str] = contextvars.ContextVar(
    "current_user", default="anonymous"
)


def get_auth_config() -> dict | None:
    """Load auth config. Returns None if auth disabled."""
    server = get_config().get("server", {})
    if not isinstance(server, dict):
        return None
    auth = server.get("auth", {})
    if not isinstance(auth, dict):
        return None
    if not auth.get("enabled", False):
        return None
    return auth


def _hash_token(token: str) -> str:
    """SHA-256 hash a token for storage comparison."""
    return hashlib.sha256(token.encode()).hexdigest()


def authenticate(scope: dict) -> tuple[str | None, str]:
    """Validate Bearer token from ASGI scope headers.

    Returns (username, error). If auth disabled, returns ("anonymous", "").
    Uses constant-time comparison to prevent timing side-channels.

    Security properties:
    - Single generic "Unauthorized" error for all failure modes (no oracle)
    - Iterates headers to detect duplicate Authorization (proxy bypass prevention)
    - Latin-1 decoding per HTTP spec (prevents UnicodeDecodeError)
    - Empty token explicitly rejected
    """
    auth_cfg = get_auth_config()
    if auth_cfg is None:
        return "anonymous", ""

    # --- mTLS: client certificate takes priority ---
    from .mtls import extract_client_cn

    client_cn = extract_client_cn(scope)
    if client_cn:
        # Check CN blocklist for emergency revocation
        denied_cns = auth_cfg.get("denied_cns", [])
        if isinstance(denied_cns, list) and client_cn in denied_cns:
            logger.warning(f"Auth failure: CN '{client_cn}' is denied")
            return None, "Unauthorized"
        logger.info(f"Auth success (mTLS): user={client_cn}")
        return client_cn, ""

    # --- Bearer token: fallback for non-TLS / programmatic access ---

    # Extract Authorization header from ASGI scope
    # ASGI headers are list of (name, value) byte tuples — iterate, don't dict()
    raw_headers = scope.get("headers") or []
    auth_values = []
    for name, value in raw_headers:
        if name == b"authorization":
            auth_values.append(value)

    # Reject duplicate Authorization headers (proxy bypass prevention)
    if len(auth_values) != 1:
        if not auth_values:
            logger.warning("Auth failure: missing Authorization header")
        else:
            logger.warning("Auth failure: duplicate Authorization headers")
        return None, "Unauthorized"

    # Decode header (HTTP headers are latin-1 per spec)
    try:
        auth_header = auth_values[0].decode("latin-1")
    except Exception:
        return None, "Unauthorized"

    # Case-insensitive Bearer scheme check (RFC 7235)
    if not auth_header.lower().startswith("bearer "):
        return None, "Unauthorized"

    token = auth_header[7:].strip()
    if not token:
        return None, "Unauthorized"

    # Check internal token first (for hook scripts inside the container)
    internal_token = os.environ.get("JARVIS_INTERNAL_TOKEN", "")
    if internal_token and hmac.compare_digest(token, internal_token):
        logger.info("Auth success: user=__internal__")
        return "__internal__", ""

    # Constant-time comparison against stored token hashes
    token_hash = _hash_token(token)
    tokens = auth_cfg.get("tokens", {})
    if not isinstance(tokens, dict):
        return None, "Unauthorized"

    for stored_hash, username in tokens.items():
        if hmac.compare_digest(token_hash, stored_hash):
            logger.info(f"Auth success: user={username}")
            return username, ""

    logger.warning("Auth failure: invalid token")
    return None, "Unauthorized"


def get_current_user() -> str:
    """Get the current request's username from contextvar."""
    return current_user.get()
