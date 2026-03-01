"""mTLS support for Jarvis MCP servers.

Provides uvicorn transport monkey-patching (needed because uvicorn doesn't
implement the ASGI TLS extension) and client certificate CN extraction.

The patch injects the raw asyncio transport into the ASGI scope so that
downstream middleware can call transport.get_extra_info("ssl_object") to
access the peer certificate.
"""

import logging
import re

logger = logging.getLogger("jarvis-auth")

_patched = False

# CN must be lowercase alphanumeric + dots/underscores/hyphens, max 64 chars
_CN_PATTERN = re.compile(r"^[a-z0-9._-]{1,64}$")


def patch_uvicorn_transport() -> bool:
    """Monkey-patch uvicorn to expose the asyncio transport in ASGI scope.

    Patches both h11 and httptools protocol implementations. Silently skips
    whichever is absent. Must be called at import time (before uvicorn
    processes any requests).

    Strategy differs per implementation because they build scopes differently:
    - h11: patches handle_events() (synchronous method that builds scope dict)
    - httptools: patches on_headers_complete() (callback after headers parsed)

    After the original method runs and self.scope is populated, we inject
    self.transport into it. The transport is already set by connection_made()
    which runs before any request processing.

    Returns True if at least one protocol was patched, False otherwise.
    """
    global _patched
    if _patched:
        return True

    patched_any = False

    # h11: scope is built inside handle_events()
    try:
        import importlib

        mod = importlib.import_module("uvicorn.protocols.http.h11_impl")
        cls = getattr(mod, "H11Protocol")
        original = cls.handle_events

        def _patched_h11_handle(self, _orig=original):
            _orig(self)
            if getattr(self, "scope", None) is not None and hasattr(self, "transport"):
                self.scope["transport"] = self.transport

        cls.handle_events = _patched_h11_handle
        patched_any = True
        logger.debug("Patched h11_impl.H11Protocol.handle_events for mTLS transport")
    except (ImportError, AttributeError) as e:
        logger.debug(f"Skipping h11_impl: {e}")

    # httptools: scope is built in on_message_begin(), finalized in on_headers_complete()
    try:
        import importlib

        mod = importlib.import_module("uvicorn.protocols.http.httptools_impl")
        cls = getattr(mod, "HttpToolsProtocol")
        original = cls.on_headers_complete

        def _patched_httptools_headers(self, _orig=original):
            _orig(self)
            if getattr(self, "scope", None) is not None and hasattr(self, "transport"):
                self.scope["transport"] = self.transport

        cls.on_headers_complete = _patched_httptools_headers
        patched_any = True
        logger.debug(
            "Patched httptools_impl.HttpToolsProtocol.on_headers_complete for mTLS transport"
        )
    except (ImportError, AttributeError) as e:
        logger.debug(f"Skipping httptools_impl: {e}")

    _patched = patched_any
    return patched_any


def extract_client_cn(scope: dict) -> str | None:
    """Extract and validate the Common Name from a client certificate.

    Reads scope["transport"] → ssl_object → getpeercert() → CN field.
    Returns None if any link in the chain is missing (non-TLS, no client
    cert presented, no CN field, or CN fails validation).

    CN validation: must match ^[a-z0-9._-]{1,64}$ to prevent injection
    of special characters into usernames.
    """
    transport = scope.get("transport")
    if transport is None:
        return None

    ssl_object = transport.get_extra_info("ssl_object")
    if ssl_object is None:
        return None

    # CERT_OPTIONAL: getpeercert() returns {} if no cert presented,
    # None if no cert and CERT_NONE, or the cert dict if presented
    peer_cert = ssl_object.getpeercert()
    if not peer_cert:
        return None

    # peer_cert["subject"] is a tuple of tuples:
    # ((('commonName', 'raph'),), (('organizationName', 'Acme'),), ...)
    subject = peer_cert.get("subject")
    if not subject:
        return None

    for field_group in subject:
        for field_name, field_value in field_group:
            if field_name == "commonName":
                cn = field_value
                if not _CN_PATTERN.match(cn):
                    logger.warning(
                        f"Client cert CN rejected (invalid format): {cn!r}"
                    )
                    return None
                return cn

    return None
