"""Codex CLI provider adapter.

Implements the ProviderAdapter protocol for the OpenAI Codex CLI tool.
Handles binary discovery, command construction, response reading,
and API fallback via OpenAI Chat Completions endpoint.

Resolution order:
    1. Codex CLI (subscription auth, sandbox mode)
    2. OpenAI API (OPENAI_API_KEY env var, http.client stdlib)
    3. Error
"""

import os
from typing import Optional

from .base import ProviderAdapter, ProviderResult, which, invoke_cli, invoke_api

# API constants
_OPENAI_HOST = "api.openai.com"
_OPENAI_PATH = "/v1/chat/completions"
_OPENAI_DEFAULT_MODEL = "gpt-4o"
_OPENAI_API_KEY_ENV = "OPENAI_API_KEY"


class CodexProvider:
    """Provider adapter for the Codex CLI with OpenAI API fallback."""

    name: str = "codex"
    _binary_name: str = "codex"

    def is_available(self) -> tuple[bool, Optional[str]]:
        """Check if the codex binary is available."""
        path = which(self._binary_name)
        return (path is not None, path)

    def has_api_key(self) -> bool:
        """Check if the OpenAI API key is available."""
        key = os.environ.get(_OPENAI_API_KEY_ENV, "").strip()
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
        """Build the codex exec command line."""
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
        # Best-effort MCP disable
        for override in [
            "mcp_servers.plugin_jarvis_core.enabled=false",
            "mcp_servers.plugin_jarvis-todoist_api.enabled=false",
        ]:
            cmd.extend(["-c", override])

        if model:
            cmd.extend(["--model", model])
        if profile:
            cmd.extend(["--profile", profile])

        cmd.append("-")  # read prompt from stdin
        return cmd

    def read_response(self, output_path: str, stdout: str) -> str:
        """Read the Codex response, preferring --output-last-message file."""
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
            f"{_OPENAI_API_KEY_ENV} not set. "
            f"Install Codex CLI or set {_OPENAI_API_KEY_ENV} to use the '{self.name}' provider."
        )

    def _invoke_api(self, prompt: str, timeout: int, model: Optional[str] = None) -> ProviderResult:
        """Invoke via OpenAI Chat Completions API."""
        api_key = os.environ.get(_OPENAI_API_KEY_ENV, "").strip()
        if not api_key:
            return ProviderResult(error="API key not found or invalid")

        payload = {
            "model": model or _OPENAI_DEFAULT_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
            "response_format": {"type": "json_object"},
        }

        return invoke_api(
            host=_OPENAI_HOST,
            path=_OPENAI_PATH,
            api_key=api_key,
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
        """Invoke Codex: CLI first, OpenAI API second, error third."""
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
