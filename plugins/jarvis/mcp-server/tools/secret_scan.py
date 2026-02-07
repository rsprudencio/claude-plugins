"""Secret detection for memory content.

Scans content for common secret patterns (API keys, tokens, credentials)
before writing to prevent accidental secret storage.

Similar to file_ops.FORBIDDEN_COMPONENTS, this is a pre-write safety gate.
"""
import re
from typing import Optional

# Patterns for common secrets (compiled lazily)
SECRET_PATTERNS = {
    "aws_key": r"AKIA[0-9A-Z]{16}",
    "github_token": r"gh[ps]_[A-Za-z0-9_]{36,}",
    "private_key": r"-----BEGIN (RSA|EC|DSA|OPENSSH) PRIVATE KEY-----",
    "jwt_token": r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}",
    "generic_secret": (
        r"(?i)(api[_-]?key|secret|password|auth_token)\s*[:=]\s*"
        r"['\"]?([A-Za-z0-9_/+=-]{8,})"
    ),
    "connection_string": r"(?i)(postgres|mysql|mongodb|redis)://[^\s]+",
    "bearer_token": r"(?i)Bearer\s+[A-Za-z0-9_.\-]{20,}",
}

_compiled: Optional[dict] = None


def _get_compiled() -> dict:
    """Compile patterns once on first use."""
    global _compiled
    if _compiled is None:
        _compiled = {
            name: re.compile(pattern)
            for name, pattern in SECRET_PATTERNS.items()
        }
    return _compiled


def redact(match_text: str, visible_chars: int = 4) -> str:
    """Show first/last N chars, mask middle.

    Args:
        match_text: The matched secret text
        visible_chars: Number of chars to show at each end

    Returns:
        Redacted string like "AKIA****WXYZ"
    """
    if len(match_text) <= visible_chars * 2:
        return "*" * len(match_text)
    start = match_text[:visible_chars]
    end = match_text[-visible_chars:]
    masked = "*" * min(len(match_text) - visible_chars * 2, 20)
    return f"{start}{masked}{end}"


def scan_for_secrets(content: str) -> list[dict]:
    """Scan content for potential secrets.

    Args:
        content: Text content to scan

    Returns:
        List of detections: [{type, line, redacted_preview}]
    """
    compiled = _get_compiled()
    detections = []
    lines = content.split("\n")

    for line_num, line in enumerate(lines, start=1):
        for secret_type, pattern in compiled.items():
            for match in pattern.finditer(line):
                matched_text = match.group(0)
                detections.append({
                    "type": secret_type,
                    "line": line_num,
                    "redacted_preview": redact(matched_text),
                })

    return detections
