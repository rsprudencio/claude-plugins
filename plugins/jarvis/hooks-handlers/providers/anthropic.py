"""Anthropic provider adapter.

Implements the ProviderAdapter protocol for Anthropic's Claude models.
Supports both Anthropic Messages API (ANTHROPIC_API_KEY env var) and
Claude CLI fallback (`claude -p --model`).

Resolution order (API-first, unlike Codex/Gemini which are CLI-first):
    1. Anthropic Messages API (ANTHROPIC_API_KEY env var, http.client stdlib)
    2. Claude CLI (`claude -p --model <model>`, OAuth from Keychain)
    3. Error
"""

import http.client
import json
import os
import subprocess
import shutil
from typing import Optional

from .base import ProviderAdapter, ProviderResult, which

# API constants
_ANTHROPIC_HOST = "api.anthropic.com"
_ANTHROPIC_PATH = "/v1/messages"
_ANTHROPIC_API_VERSION = "2023-06-01"
_ANTHROPIC_DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"
_ANTHROPIC_MAX_TOKENS = 1000


class AnthropicProvider:
    """Provider adapter for Anthropic Claude with CLI fallback.

    Unlike Codex/Gemini which prefer CLI-first, Anthropic is API-first
    because the Anthropic SDK/API is the primary interface, while the
    Claude CLI is a secondary fallback via OAuth.
    """

    name: str = "anthropic"
    _binary_name: str = "claude"

    def is_available(self) -> tuple[bool, Optional[str]]:
        """Check if the claude binary is available."""
        path = which(self._binary_name)
        return (path is not None, path)

    def has_api_key(self) -> bool:
        """Check if the Anthropic API key is available."""
        key = os.environ.get(_ANTHROPIC_API_KEY_ENV, "").strip()
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
        """Build the claude CLI command line for headless extraction.

        Uses `claude -p --model <model>` in non-interactive mode.
        Anti-recursion is handled by the caller (JARVIS_EXTRACTING env var).
        """
        cmd = [
            binary,
            "-p",
            "--model", model or "haiku",
            "--no-session-persistence",
        ]
        return cmd

    def read_response(self, output_path: str, stdout: str) -> str:
        """Read Claude CLI response from stdout (no file output)."""
        return stdout.strip()

    def availability_error(self) -> str:
        """Human-readable error for when neither API nor CLI is available."""
        return (
            f"{_ANTHROPIC_API_KEY_ENV} not set and "
            f"'{self._binary_name}' not found in PATH. "
            f"Set {_ANTHROPIC_API_KEY_ENV} or install Claude CLI to use the '{self.name}' provider."
        )

    def _invoke_api(
        self, prompt: str, timeout: int, model: Optional[str] = None,
        max_tokens: int = _ANTHROPIC_MAX_TOKENS,
    ) -> ProviderResult:
        """Invoke via Anthropic Messages API.

        Uses stdlib http.client (zero external deps). The Anthropic API
        uses x-api-key header (not Bearer token) and returns a different
        response shape than OpenAI, so we use a direct implementation
        rather than the shared invoke_api() helper.
        """
        api_key = os.environ.get(_ANTHROPIC_API_KEY_ENV, "").strip()
        if not api_key:
            return ProviderResult(error="API key not found or invalid")

        payload = {
            "model": model or _ANTHROPIC_DEFAULT_MODEL,
            "max_tokens": max_tokens,
            "temperature": 0,
            "messages": [{"role": "user", "content": prompt}],
        }

        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_API_VERSION,
        }
        body = json.dumps(payload).encode("utf-8")

        try:
            conn = http.client.HTTPSConnection(_ANTHROPIC_HOST, timeout=timeout)
            conn.request("POST", _ANTHROPIC_PATH, body=body, headers=headers)
            resp = conn.getresponse()
            resp_body = resp.read().decode("utf-8")
            conn.close()

            if resp.status >= 400:
                return ProviderResult(
                    error=f"API returned HTTP {resp.status}",
                    returncode=resp.status,
                )

            data = json.loads(resp_body)

            # Extract text from Anthropic Messages response
            raw_text = ""
            content_blocks = data.get("content", [])
            for block in content_blocks:
                if block.get("type") == "text":
                    raw_text += block.get("text", "")

            # Extract token usage for telemetry
            usage = data.get("usage", {})
            input_tokens = usage.get("input_tokens", 0)
            output_tokens = usage.get("output_tokens", 0)

            result = ProviderResult(raw_text=raw_text)
            # Attach usage metadata for telemetry (not part of base protocol)
            result._usage = {  # type: ignore[attr-defined]
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
            return result

        except (json.JSONDecodeError, KeyError, IndexError):
            return ProviderResult(raw_text=resp_body)
        except (OSError, http.client.HTTPException) as e:
            return ProviderResult(error=f"API connection failed: {type(e).__name__}")

    def _invoke_cli(
        self, prompt: str, timeout: int, model: Optional[str] = None,
    ) -> ProviderResult:
        """Invoke via Claude CLI in headless mode.

        Uses `claude -p --model haiku` with JARVIS_EXTRACTING=1 env var
        to prevent infinite recursion (Stop hook checks this).
        """
        binary = which(self._binary_name)
        if not binary:
            return ProviderResult(error=f"'{self._binary_name}' not found in PATH")

        cmd = [
            binary,
            "-p",
            "--model", model or "haiku",
            "--no-session-persistence",
        ]

        env = os.environ.copy()
        env["JARVIS_EXTRACTING"] = "1"

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
                env=env,
            )
        except subprocess.TimeoutExpired:
            return ProviderResult(
                timed_out=True,
                error=f"CLI timed out after {timeout}s",
            )

        raw_text = result.stdout.strip()

        if result.returncode != 0 and not raw_text:
            stderr_snippet = (result.stderr or "")[:500]
            return ProviderResult(
                error=f"CLI exited with code {result.returncode}: {stderr_snippet}",
                returncode=result.returncode,
            )

        # Estimate token usage (CLI doesn't expose exact counts)
        est_input = len(prompt) // 4
        est_output = len(raw_text) // 4

        pr = ProviderResult(raw_text=raw_text, returncode=result.returncode)
        pr._usage = {  # type: ignore[attr-defined]
            "input_tokens": est_input,
            "output_tokens": est_output,
        }
        return pr

    def invoke(
        self,
        prompt: str,
        schema: dict,
        working_dir: str,
        timeout: int,
        model: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> ProviderResult:
        """Invoke Anthropic: API first, CLI second, error third.

        Unlike Codex/Gemini which are CLI-first, Anthropic prefers API
        because it's the primary interface with exact token counts.
        """
        # Try API first (fast, exact token counts)
        if self.has_api_key():
            result = self._invoke_api(prompt, timeout, model)
            if result.error is None or result.raw_text:
                return result

        # Try CLI fallback (uses OAuth from Keychain)
        available, _ = self.is_available()
        if available:
            return self._invoke_cli(prompt, timeout, model)

        # Neither available
        return ProviderResult(error=self.availability_error())
