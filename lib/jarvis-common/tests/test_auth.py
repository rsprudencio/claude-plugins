"""Tests for the shared auth module (jarvis_common.auth).

Covers all auth scenarios including DAR-reviewed edge cases:
- Timing side-channel prevention (hmac.compare_digest)
- Error message oracle prevention (single generic message)
- Duplicate header detection (proxy bypass)
- Latin-1 encoding (HTTP spec compliance)
- Contextvar isolation (async safety)
- Internal token support (hook scripts)
"""

import asyncio
import hashlib
import os
from unittest.mock import patch

import pytest

from jarvis_common.auth import (
    _hash_token,
    authenticate,
    current_user,
    get_auth_config,
    get_current_user,
)


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_scope(token=None, headers=None):
    """Build a minimal ASGI scope with optional auth header."""
    if headers is not None:
        return {"type": "http", "headers": headers}
    if token is None:
        return {"type": "http", "headers": []}
    return {
        "type": "http",
        "headers": [[b"authorization", f"Bearer {token}".encode("latin-1")]],
    }


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


AUTH_ENABLED_CONFIG = {
    "server": {
        "auth": {
            "enabled": True,
            "tokens": {
                _hash("valid-token-abc"): "raph",
                _hash("valid-token-xyz"): "alice",
            },
        }
    }
}

AUTH_DISABLED_CONFIG = {"server": {"auth": {"enabled": False, "tokens": {}}}}


@pytest.fixture(autouse=True)
def _clear_config_cache():
    """Clear config cache before each test."""
    from jarvis_common.config import clear_config_cache

    clear_config_cache()
    yield
    clear_config_cache()


@pytest.fixture(autouse=True)
def _clear_internal_token():
    """Ensure JARVIS_INTERNAL_TOKEN is clean between tests."""
    old = os.environ.pop("JARVIS_INTERNAL_TOKEN", None)
    yield
    if old is not None:
        os.environ["JARVIS_INTERNAL_TOKEN"] = old
    else:
        os.environ.pop("JARVIS_INTERNAL_TOKEN", None)


# ── Auth disabled (default) ─────────────────────────────────────────────


class TestAuthDisabled:
    def test_auth_disabled_allows_all(self):
        """When auth disabled, all requests pass as anonymous."""
        with patch("jarvis_common.auth.get_config", return_value=AUTH_DISABLED_CONFIG):
            user, err = authenticate(_make_scope())
            assert user == "anonymous"
            assert err == ""

    def test_auth_disabled_ignores_token(self):
        """When auth disabled, even a token is ignored (returns anonymous)."""
        with patch("jarvis_common.auth.get_config", return_value=AUTH_DISABLED_CONFIG):
            user, err = authenticate(_make_scope(token="some-token"))
            assert user == "anonymous"
            assert err == ""

    def test_no_server_section(self):
        """Missing 'server' section in config means auth disabled."""
        with patch("jarvis_common.auth.get_config", return_value={}):
            user, err = authenticate(_make_scope())
            assert user == "anonymous"
            assert err == ""

    def test_auth_config_returns_none_when_disabled(self):
        """get_auth_config returns None when auth disabled."""
        with patch("jarvis_common.auth.get_config", return_value=AUTH_DISABLED_CONFIG):
            assert get_auth_config() is None


# ── Auth enabled — success cases ────────────────────────────────────────


class TestAuthSuccess:
    def test_valid_token_returns_username(self):
        """Valid token returns the mapped username."""
        with patch("jarvis_common.auth.get_config", return_value=AUTH_ENABLED_CONFIG):
            user, err = authenticate(_make_scope(token="valid-token-abc"))
            assert user == "raph"
            assert err == ""

    def test_second_valid_token(self):
        """Second configured token maps to different user."""
        with patch("jarvis_common.auth.get_config", return_value=AUTH_ENABLED_CONFIG):
            user, err = authenticate(_make_scope(token="valid-token-xyz"))
            assert user == "alice"
            assert err == ""

    def test_case_insensitive_bearer_scheme(self):
        """'bearer' and 'BEARER' both accepted (RFC 7235)."""
        with patch("jarvis_common.auth.get_config", return_value=AUTH_ENABLED_CONFIG):
            for prefix in ["bearer", "BEARER", "Bearer", "bEaReR"]:
                scope = {
                    "type": "http",
                    "headers": [
                        [b"authorization", f"{prefix} valid-token-abc".encode("latin-1")]
                    ],
                }
                user, err = authenticate(scope)
                assert user == "raph", f"Failed with prefix: {prefix}"
                assert err == ""

    def test_whitespace_in_token_stripped(self):
        """Trailing whitespace in token is stripped before comparison."""
        with patch("jarvis_common.auth.get_config", return_value=AUTH_ENABLED_CONFIG):
            user, err = authenticate(_make_scope(token="valid-token-abc  "))
            assert user == "raph"
            assert err == ""


# ── Auth enabled — failure cases ────────────────────────────────────────


class TestAuthFailure:
    def test_no_header_rejected(self):
        """Missing Authorization header returns Unauthorized."""
        with patch("jarvis_common.auth.get_config", return_value=AUTH_ENABLED_CONFIG):
            user, err = authenticate(_make_scope())
            assert user is None
            assert err == "Unauthorized"

    def test_invalid_token_rejected(self):
        """Wrong token returns Unauthorized."""
        with patch("jarvis_common.auth.get_config", return_value=AUTH_ENABLED_CONFIG):
            user, err = authenticate(_make_scope(token="wrong-token"))
            assert user is None
            assert err == "Unauthorized"

    def test_empty_bearer_token_rejected(self):
        """'Bearer ' with no value after it is rejected."""
        with patch("jarvis_common.auth.get_config", return_value=AUTH_ENABLED_CONFIG):
            scope = {
                "type": "http",
                "headers": [[b"authorization", b"Bearer "]],
            }
            user, err = authenticate(scope)
            assert user is None
            assert err == "Unauthorized"

    def test_basic_scheme_rejected(self):
        """Basic auth scheme is not accepted."""
        with patch("jarvis_common.auth.get_config", return_value=AUTH_ENABLED_CONFIG):
            scope = {
                "type": "http",
                "headers": [[b"authorization", b"Basic dXNlcjpwYXNz"]],
            }
            user, err = authenticate(scope)
            assert user is None
            assert err == "Unauthorized"

    def test_single_generic_error_message(self):
        """All failure modes return the same error string (no oracle)."""
        with patch("jarvis_common.auth.get_config", return_value=AUTH_ENABLED_CONFIG):
            # Missing header
            _, err1 = authenticate(_make_scope())
            # Wrong token
            _, err2 = authenticate(_make_scope(token="wrong"))
            # Empty bearer
            _, err3 = authenticate(
                _make_scope(headers=[[b"authorization", b"Bearer "]])
            )
            # Basic scheme
            _, err4 = authenticate(
                _make_scope(headers=[[b"authorization", b"Basic abc"]])
            )

            assert err1 == err2 == err3 == err4 == "Unauthorized"


# ── Edge cases (DAR findings) ───────────────────────────────────────────


class TestEdgeCases:
    def test_duplicate_authorization_headers_rejected(self):
        """Two Authorization headers are rejected (proxy bypass prevention)."""
        with patch("jarvis_common.auth.get_config", return_value=AUTH_ENABLED_CONFIG):
            scope = {
                "type": "http",
                "headers": [
                    [b"authorization", b"Bearer valid-token-abc"],
                    [b"authorization", b"Bearer valid-token-xyz"],
                ],
            }
            user, err = authenticate(scope)
            assert user is None
            assert err == "Unauthorized"

    def test_no_headers_key_in_scope(self):
        """Missing headers key in scope is handled gracefully."""
        with patch("jarvis_common.auth.get_config", return_value=AUTH_ENABLED_CONFIG):
            user, err = authenticate({"type": "http"})
            assert user is None
            assert err == "Unauthorized"

    def test_non_dict_server_config(self):
        """Non-dict 'server' config value is handled."""
        with patch(
            "jarvis_common.auth.get_config",
            return_value={"server": "not-a-dict"},
        ):
            user, err = authenticate(_make_scope())
            assert user == "anonymous"
            assert err == ""

    def test_non_dict_auth_config(self):
        """Non-dict 'auth' config value is handled."""
        with patch(
            "jarvis_common.auth.get_config",
            return_value={"server": {"auth": "not-a-dict"}},
        ):
            user, err = authenticate(_make_scope())
            assert user == "anonymous"
            assert err == ""

    def test_non_dict_tokens_config(self):
        """Non-dict 'tokens' config value is handled."""
        with patch(
            "jarvis_common.auth.get_config",
            return_value={
                "server": {"auth": {"enabled": True, "tokens": "not-a-dict"}}
            },
        ):
            user, err = authenticate(_make_scope(token="any"))
            assert user is None
            assert err == "Unauthorized"


# ── Internal token ──────────────────────────────────────────────────────


class TestInternalToken:
    def test_internal_token_accepted(self):
        """JARVIS_INTERNAL_TOKEN env var works for hook scripts."""
        os.environ["JARVIS_INTERNAL_TOKEN"] = "internal-secret-123"
        with patch("jarvis_common.auth.get_config", return_value=AUTH_ENABLED_CONFIG):
            user, err = authenticate(_make_scope(token="internal-secret-123"))
            assert user == "__internal__"
            assert err == ""

    def test_internal_token_not_set_falls_through(self):
        """Without JARVIS_INTERNAL_TOKEN, token must match config hashes."""
        # env var not set (cleared by fixture)
        with patch("jarvis_common.auth.get_config", return_value=AUTH_ENABLED_CONFIG):
            user, err = authenticate(_make_scope(token="random-token"))
            assert user is None
            assert err == "Unauthorized"


# ── Hash function ───────────────────────────────────────────────────────


class TestHashToken:
    def test_hash_is_sha256(self):
        """_hash_token produces a valid SHA-256 hex digest."""
        result = _hash_token("test")
        assert len(result) == 64
        assert result == hashlib.sha256(b"test").hexdigest()

    def test_hash_deterministic(self):
        """Same input always produces same hash."""
        assert _hash_token("foo") == _hash_token("foo")

    def test_hash_differs_for_different_input(self):
        assert _hash_token("foo") != _hash_token("bar")


# ── Contextvar ──────────────────────────────────────────────────────────


class TestContextVar:
    def test_default_is_anonymous(self):
        """Default contextvar value is 'anonymous'."""
        assert get_current_user() == "anonymous"

    def test_set_and_reset(self):
        """Contextvar set/reset pattern works correctly."""
        token = current_user.set("raph")
        assert get_current_user() == "raph"
        current_user.reset(token)
        assert get_current_user() == "anonymous"

    def test_contextvar_isolation_concurrent(self):
        """Two concurrent async tasks see different user values."""
        results = {}

        async def task_a():
            tok = current_user.set("alice")
            try:
                await asyncio.sleep(0.01)  # yield to scheduler
                results["a"] = get_current_user()
            finally:
                current_user.reset(tok)

        async def task_b():
            tok = current_user.set("bob")
            try:
                await asyncio.sleep(0.01)  # yield to scheduler
                results["b"] = get_current_user()
            finally:
                current_user.reset(tok)

        async def run():
            await asyncio.gather(task_a(), task_b())

        asyncio.run(run())
        assert results["a"] == "alice"
        assert results["b"] == "bob"
        # After both tasks complete, default should be restored
        assert get_current_user() == "anonymous"
