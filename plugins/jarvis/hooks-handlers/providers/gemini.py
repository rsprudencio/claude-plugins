"""Gemini provider adapter.

Implements the ProviderAdapter protocol for Google's Gemini.
Supports both Gemini CLI (Google account SSO) and Gemini API
(GEMINI_API_KEY env var).

Resolution order:
    1. Gemini CLI (Google account SSO, sandbox mode)
    2. Gemini API (GEMINI_API_KEY env var, http.client stdlib)
    3. Error
"""

import os
from typing import Optional

from .base import ProviderAdapter, ProviderResult, which, invoke_cli, invoke_api

# API constants
_GEMINI_HOST = "generativelanguage.googleapis.com"
_GEMINI_DEFAULT_MODEL = "gemini-2.0-flash"
_GEMINI_API_KEY_ENV = "GEMINI_API_KEY"


class GeminiProvider:
    """Provider adapter for Gemini CLI with API fallback."""

    name: str = "gemini"
    _binary_name: str = "gemini"

    def is_available(self) -> tuple[bool, Optional[str]]:
        """Check if the gemini binary is available."""
        path = which(self._binary_name)
        return (path is not None, path)

    def has_api_key(self) -> bool:
        """Check if the Gemini API key is available."""
        key = os.environ.get(_GEMINI_API_KEY_ENV, "").strip()
        return len(key) > 0

    def build_command(
        self,
        binary: str,
        output_path: str,
        schema_path: str,
        working_dir: str,
        model: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> list[str]:
        """Build the gemini CLI command line.

        Gemini CLI uses a similar exec pattern to Codex.
        Adjust flags based on actual gemini CLI interface.
        """
        cmd = [
            binary,
            "exec",
            "--color", "never",
            "--output-last-message", output_path,
            "--output-schema", schema_path,
            "--sandbox", "read-only",
            "--ephemeral",
            "--skip-git-repo-check",
            "--cd", working_dir,
        ]

        if model:
            cmd.extend(["--model", model])
        if profile:
            cmd.extend(["--profile", profile])

        cmd.append("-")  # read prompt from stdin
        return cmd

    def read_response(self, output_path: str, stdout: str) -> str:
        """Read the Gemini response, preferring output file."""
        if os.path.isfile(output_path):
            with open(output_path, "r", encoding="utf-8") as f:
                text = f.read().strip()
                if text:
                    return text
        return stdout.strip()

    def availability_error(self) -> str:
        """Human-readable error for when neither CLI nor API is available."""
        return (
            f"'{self._binary_name}' not found in PATH and "
            f"{_GEMINI_API_KEY_ENV} not set. "
            f"Install Gemini CLI or set {_GEMINI_API_KEY_ENV} to use the '{self.name}' provider."
        )

    def _invoke_api(self, prompt: str, timeout: int, model: Optional[str] = None) -> ProviderResult:
        """Invoke via Gemini REST API.

        Uses the generateContent endpoint with API key in query param
        (Google's convention) — key is never in error messages.
        """
        api_key = os.environ.get(_GEMINI_API_KEY_ENV, "").strip()
        if not api_key:
            return ProviderResult(error="API key not found or invalid")

        model_name = model or _GEMINI_DEFAULT_MODEL
        path = f"/v1beta/models/{model_name}:generateContent?key={api_key}"

        payload = {
            "contents": [
                {
                    "parts": [{"text": prompt}]
                }
            ],
            "generationConfig": {
                "temperature": 0.3,
                "responseMimeType": "application/json",
            },
        }

        # Gemini uses query param auth, not Bearer token, but invoke_api
        # sends a Bearer header. We use a direct invocation instead.
        return _gemini_api_call(
            host=_GEMINI_HOST,
            path=path,
            payload=payload,
            timeout=timeout,
        )

    def invoke(
        self,
        prompt: str,
        schema: dict,
        working_dir: str,
        timeout: int,
        model: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> ProviderResult:
        """Invoke Gemini: CLI first, API second, error third."""
        # Try CLI first
        available, _ = self.is_available()
        if available:
            result = invoke_cli(self, prompt, schema, working_dir, timeout, model, profile)
            if result.error is None or result.raw_text:
                return result

        # Try API fallback
        if self.has_api_key():
            return self._invoke_api(prompt, timeout, model)

        # Neither available
        return ProviderResult(error=self.availability_error())


def _gemini_api_call(
    host: str,
    path: str,
    payload: dict,
    timeout: int,
) -> ProviderResult:
    """Direct Gemini API call using query param auth (Google convention).

    The API key is embedded in the path query string — never in headers
    or error messages.
    """
    import http.client
    import json

    headers = {"Content-Type": "application/json"}
    body = json.dumps(payload).encode("utf-8")

    try:
        conn = http.client.HTTPSConnection(host, timeout=timeout)
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        resp_body = resp.read().decode("utf-8")
        conn.close()

        if resp.status >= 400:
            return ProviderResult(
                error=f"API returned HTTP {resp.status}",
                returncode=resp.status,
            )

        # Parse Gemini response format
        try:
            data = json.loads(resp_body)
            if "candidates" in data:
                parts = data["candidates"][0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts)
                return ProviderResult(raw_text=text)
            return ProviderResult(raw_text=resp_body)
        except (json.JSONDecodeError, KeyError, IndexError):
            return ProviderResult(raw_text=resp_body)

    except (OSError, http.client.HTTPException) as e:
        return ProviderResult(error=f"API connection failed: {type(e).__name__}")
