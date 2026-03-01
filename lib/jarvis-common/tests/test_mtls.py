"""Tests for the mTLS module (jarvis_common.mtls).

Covers CN extraction from client certificates and the uvicorn transport patch.
"""

from unittest.mock import MagicMock

import pytest

from jarvis_common.mtls import extract_client_cn, patch_uvicorn_transport


# ── Helpers ──────────────────────────────────────────────────────────────


def _make_scope_with_cert(subject_tuples):
    """Build a mock ASGI scope with a transport carrying a peer cert.

    subject_tuples: the value for peer_cert["subject"], e.g.
        ((("commonName", "raph"),),)
    """
    ssl_object = MagicMock()
    ssl_object.getpeercert.return_value = {"subject": subject_tuples}

    transport = MagicMock()
    transport.get_extra_info.return_value = ssl_object

    return {"type": "http", "transport": transport}


def _make_scope_no_cert():
    """Scope with TLS transport but no client cert (CERT_OPTIONAL, no cert presented)."""
    ssl_object = MagicMock()
    ssl_object.getpeercert.return_value = {}  # empty dict = no cert

    transport = MagicMock()
    transport.get_extra_info.return_value = ssl_object

    return {"type": "http", "transport": transport}


# ── extract_client_cn ────────────────────────────────────────────────────


class TestExtractClientCn:
    def test_no_transport_in_scope(self):
        """No transport key → None (non-TLS connection)."""
        assert extract_client_cn({"type": "http"}) is None

    def test_no_ssl_object(self):
        """Transport without SSL → None (plain HTTP)."""
        transport = MagicMock()
        transport.get_extra_info.return_value = None
        assert extract_client_cn({"type": "http", "transport": transport}) is None

    def test_no_peer_cert(self):
        """CERT_OPTIONAL with no cert presented → None."""
        assert extract_client_cn(_make_scope_no_cert()) is None

    def test_valid_cn(self):
        """Simple valid CN extracted correctly."""
        scope = _make_scope_with_cert(((("commonName", "raph"),),))
        assert extract_client_cn(scope) == "raph"

    def test_cn_with_dots_and_hyphens(self):
        """CN with dots and hyphens is valid."""
        scope = _make_scope_with_cert(((("commonName", "raph.test-user"),),))
        assert extract_client_cn(scope) == "raph.test-user"

    def test_cn_with_underscores(self):
        """CN with underscores is valid."""
        scope = _make_scope_with_cert(((("commonName", "test_user"),),))
        assert extract_client_cn(scope) == "test_user"

    def test_multiple_subject_fields(self):
        """CN extracted among O, OU, etc."""
        scope = _make_scope_with_cert(
            (
                (("organizationName", "Acme Corp"),),
                (("organizationalUnitName", "Engineering"),),
                (("commonName", "alice"),),
            )
        )
        assert extract_client_cn(scope) == "alice"

    def test_no_cn_field(self):
        """Subject without CN → None."""
        scope = _make_scope_with_cert(
            (
                (("organizationName", "Acme Corp"),),
                (("organizationalUnitName", "Engineering"),),
            )
        )
        assert extract_client_cn(scope) is None

    def test_cn_uppercase_rejected(self):
        """Uppercase CN rejected (must be lowercase)."""
        scope = _make_scope_with_cert(((("commonName", "Raph"),),))
        assert extract_client_cn(scope) is None

    def test_cn_with_spaces_rejected(self):
        """CN with spaces rejected."""
        scope = _make_scope_with_cert(((("commonName", "raph user"),),))
        assert extract_client_cn(scope) is None

    def test_cn_too_long_rejected(self):
        """CN over 64 chars rejected."""
        long_cn = "a" * 65
        scope = _make_scope_with_cert(((("commonName", long_cn),),))
        assert extract_client_cn(scope) is None

    def test_cn_empty_rejected(self):
        """Empty CN rejected."""
        scope = _make_scope_with_cert(((("commonName", ""),),))
        assert extract_client_cn(scope) is None

    def test_cn_special_chars_rejected(self):
        """CN with special characters rejected."""
        for bad_cn in ["raph@evil", "raph;drop", "../raph", "raph\x00null"]:
            scope = _make_scope_with_cert(((("commonName", bad_cn),),))
            assert extract_client_cn(scope) is None, f"Should reject: {bad_cn!r}"

    def test_cn_max_length_accepted(self):
        """CN of exactly 64 chars is accepted."""
        cn = "a" * 64
        scope = _make_scope_with_cert(((("commonName", cn),),))
        assert extract_client_cn(scope) == cn

    def test_empty_subject(self):
        """Empty subject tuple → None."""
        scope = _make_scope_with_cert(())
        assert extract_client_cn(scope) is None

    def test_peer_cert_none(self):
        """getpeercert() returns None (CERT_NONE mode) → None."""
        ssl_object = MagicMock()
        ssl_object.getpeercert.return_value = None

        transport = MagicMock()
        transport.get_extra_info.return_value = ssl_object

        assert extract_client_cn({"type": "http", "transport": transport}) is None


# ── patch_uvicorn_transport ──────────────────────────────────────────────


class TestPatchUvicornTransport:
    def test_patch_idempotent(self):
        """Calling patch_uvicorn_transport() twice is safe."""
        # First call may or may not succeed depending on uvicorn presence
        result1 = patch_uvicorn_transport()
        result2 = patch_uvicorn_transport()
        # Second call should return same result (idempotent)
        assert result1 == result2

    def test_patch_returns_bool(self):
        """patch_uvicorn_transport() returns a boolean."""
        result = patch_uvicorn_transport()
        assert isinstance(result, bool)
