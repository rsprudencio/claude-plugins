"""Tests for secret detection module."""
import pytest
from tools.secret_scan import scan_for_secrets, redact


class TestRedact:
    """Tests for secret text redaction."""

    def test_long_string(self):
        result = redact("AKIAIOSFODNN7EXAMPLE")
        assert result.startswith("AKIA")
        assert result.endswith("MPLE")
        assert "*" in result

    def test_short_string(self):
        result = redact("abc")
        assert result == "***"

    def test_exact_boundary(self):
        # 8 chars with 4 visible each side = no middle to mask, all masked
        result = redact("12345678", visible_chars=4)
        assert result == "********"

    def test_custom_visible_chars(self):
        result = redact("AKIAIOSFODNN7EXAMPLE", visible_chars=2)
        assert result.startswith("AK")
        assert result.endswith("LE")


class TestScanForSecrets:
    """Tests for pattern-based secret scanning."""

    def test_aws_key(self):
        content = "aws_access_key_id = AKIAIOSFODNN7EXAMPLE"
        detections = scan_for_secrets(content)
        assert len(detections) >= 1
        types = [d["type"] for d in detections]
        assert "aws_key" in types

    def test_github_token(self):
        content = "token: ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijkl"
        detections = scan_for_secrets(content)
        types = [d["type"] for d in detections]
        assert "github_token" in types

    def test_github_secret_token(self):
        content = "GITHUB_TOKEN=ghs_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijkl"
        detections = scan_for_secrets(content)
        types = [d["type"] for d in detections]
        assert "github_token" in types

    def test_private_key(self):
        content = "-----BEGIN RSA PRIVATE KEY-----\nMIIBog..."
        detections = scan_for_secrets(content)
        types = [d["type"] for d in detections]
        assert "private_key" in types

    def test_ec_private_key(self):
        content = "-----BEGIN EC PRIVATE KEY-----"
        detections = scan_for_secrets(content)
        types = [d["type"] for d in detections]
        assert "private_key" in types

    def test_jwt_token(self):
        content = "Authorization: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.signature"
        detections = scan_for_secrets(content)
        types = [d["type"] for d in detections]
        assert "jwt_token" in types

    def test_generic_api_key(self):
        content = "api_key = 'sk_live_abcdefghij1234567890'"
        detections = scan_for_secrets(content)
        types = [d["type"] for d in detections]
        assert "generic_secret" in types

    def test_generic_password(self):
        content = 'password: "SuperSecret123456"'
        detections = scan_for_secrets(content)
        types = [d["type"] for d in detections]
        assert "generic_secret" in types

    def test_connection_string_postgres(self):
        content = "DATABASE_URL=postgres://user:pass@host:5432/db"
        detections = scan_for_secrets(content)
        types = [d["type"] for d in detections]
        assert "connection_string" in types

    def test_connection_string_mongodb(self):
        content = "MONGO_URI=mongodb://admin:pass@cluster.example.com/mydb"
        detections = scan_for_secrets(content)
        types = [d["type"] for d in detections]
        assert "connection_string" in types

    def test_bearer_token(self):
        content = "Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.test"
        detections = scan_for_secrets(content)
        types = [d["type"] for d in detections]
        assert "bearer_token" in types

    def test_line_numbers(self):
        content = "line one\nline two\napi_key = 'sk_test_12345678abcdefgh'\nline four"
        detections = scan_for_secrets(content)
        assert any(d["line"] == 3 for d in detections)

    def test_multiple_secrets(self):
        content = (
            "aws_key = AKIAIOSFODNN7EXAMPLE\n"
            "password = 'MySecretPassword123'\n"
            "postgres://user:pass@localhost/db"
        )
        detections = scan_for_secrets(content)
        assert len(detections) >= 3

    def test_redacted_previews_present(self):
        content = "api_key = 'sk_live_abcdefghij1234567890'"
        detections = scan_for_secrets(content)
        assert all("redacted_preview" in d for d in detections)
        assert all("*" in d["redacted_preview"] for d in detections)

    # --- False positive resistance ---

    def test_no_false_positive_uuid(self):
        content = "id: 550e8400-e29b-41d4-a716-446655440000"
        detections = scan_for_secrets(content)
        assert len(detections) == 0

    def test_no_false_positive_normal_text(self):
        content = "# Authentication Architecture\n\nWe use OAuth 2.0 for secure access."
        detections = scan_for_secrets(content)
        assert len(detections) == 0

    def test_no_false_positive_short_values(self):
        content = "key: value\nsecret: no"
        detections = scan_for_secrets(content)
        assert len(detections) == 0

    def test_clean_content_returns_empty(self):
        content = "# My Notes\n\nThis is a regular markdown document about cooking."
        detections = scan_for_secrets(content)
        assert detections == []

    def test_empty_content(self):
        detections = scan_for_secrets("")
        assert detections == []
