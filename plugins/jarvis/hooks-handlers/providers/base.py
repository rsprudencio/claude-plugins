"""Provider adapter protocol and shared utilities.

Defines the contract that all provider adapters must implement, plus
shared helper functions for binary discovery and vault path resolution.
"""

import http.client
import json
import os
import platform
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Provider result dataclass
# ---------------------------------------------------------------------------


@dataclass
class ProviderResult:
    """Result from a provider invocation (subprocess or API)."""

    raw_text: str = ""
    error: Optional[str] = None
    returncode: Optional[int] = None
    timed_out: bool = False


# ---------------------------------------------------------------------------
# Provider adapter protocol
# ---------------------------------------------------------------------------


@runtime_checkable
class ProviderAdapter(Protocol):
    """Contract that all provider adapters must satisfy.

    Providers handle the mechanics of invoking an external AI tool
    (binary discovery, command construction, response reading) while
    the orchestrator handles prompt building, JSON extraction, and
    normalization.
    """

    name: str

    def is_available(self) -> tuple[bool, Optional[str]]:
        """Check if the provider binary is available.

        Returns:
            (available, binary_path) — binary_path is None if not found.
        """
        ...

    def build_command(
        self,
        binary: str,
        output_path: str,
        schema_path: str,
        working_dir: str,
        model: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> list[str]:
        """Build the CLI command to invoke the provider.

        Args:
            binary: Resolved path to the provider binary.
            output_path: Path where the provider should write its response.
            schema_path: Path to the JSON schema file for structured output.
            working_dir: Working directory for the subprocess.
            model: Optional model override.
            profile: Optional profile override.

        Returns:
            Command as a list of strings suitable for subprocess.run().
        """
        ...

    def read_response(self, output_path: str, stdout: str) -> str:
        """Read the provider's response, preferring file output over stdout.

        Args:
            output_path: Path to the response file written by the provider.
            stdout: Captured stdout from the subprocess.

        Returns:
            The response text (may be empty if nothing was produced).
        """
        ...

    def availability_error(self) -> str:
        """Human-readable error message when the provider is unavailable."""
        ...

    def invoke(
        self,
        prompt: str,
        schema: dict,
        working_dir: str,
        timeout: int,
        model: Optional[str] = None,
        profile: Optional[str] = None,
    ) -> "ProviderResult":
        """Invoke the provider with the given prompt.

        Resolution order: CLI first, API second, error third.
        Each provider implements its own resolution strategy.

        Args:
            prompt: The full prompt text to send.
            schema: JSON schema for structured output.
            working_dir: Working directory for CLI sandbox.
            timeout: Timeout in seconds.
            model: Optional model override.
            profile: Optional profile override.

        Returns:
            ProviderResult with raw_text or error.
        """
        ...


# ---------------------------------------------------------------------------
# Shared invocation helpers
# ---------------------------------------------------------------------------


def invoke_cli(
    adapter: "ProviderAdapter",
    prompt: str,
    schema: dict,
    working_dir: str,
    timeout: int,
    model: Optional[str] = None,
    profile: Optional[str] = None,
) -> ProviderResult:
    """Shared CLI invocation logic for any provider.

    Handles temp dir creation, schema writing, subprocess execution,
    and response reading via the adapter's build_command/read_response.
    """
    available, binary_path = adapter.is_available()
    if not available or binary_path is None:
        return ProviderResult(error=adapter.availability_error())

    with tempfile.TemporaryDirectory() as tmpdir:
        output_path = os.path.join(tmpdir, "response.md")
        schema_path = os.path.join(tmpdir, "review_schema.json")

        with open(schema_path, "w", encoding="utf-8") as f:
            json.dump(schema, f)

        cmd = adapter.build_command(
            binary=binary_path,
            output_path=output_path,
            schema_path=schema_path,
            working_dir=working_dir,
            model=model,
            profile=profile,
        )

        try:
            result = subprocess.run(
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ProviderResult(timed_out=True, error=f"CLI timed out after {timeout}s")

        raw_text = adapter.read_response(output_path, result.stdout)

        if result.returncode != 0 and not raw_text:
            stderr_snippet = (result.stderr or "")[:500]
            return ProviderResult(
                error=f"CLI exited with code {result.returncode}: {stderr_snippet}",
                returncode=result.returncode,
            )

        return ProviderResult(raw_text=raw_text, returncode=result.returncode)


def invoke_api(
    host: str,
    path: str,
    api_key: str,
    payload: dict,
    timeout: int = 120,
    use_ssl: bool = True,
) -> ProviderResult:
    """Invoke a provider via its HTTP API using stdlib http.client.

    Zero external dependencies. Auth via Authorization header only —
    key never in URL, args, or error messages.

    Args:
        host: API hostname (e.g. "api.openai.com").
        path: API endpoint path (e.g. "/v1/chat/completions").
        api_key: API key (from env var, never logged).
        payload: JSON request body.
        timeout: HTTP timeout in seconds.
        use_ssl: Whether to use HTTPS (default True).

    Returns:
        ProviderResult with raw_text from API response or error.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    body = json.dumps(payload).encode("utf-8")

    try:
        conn_class = http.client.HTTPSConnection if use_ssl else http.client.HTTPConnection
        conn = conn_class(host, timeout=timeout)
        conn.request("POST", path, body=body, headers=headers)
        resp = conn.getresponse()
        resp_body = resp.read().decode("utf-8")
        conn.close()

        if resp.status >= 400:
            # Never echo the API key in error messages
            return ProviderResult(
                error=f"API returned HTTP {resp.status}",
                returncode=resp.status,
            )

        # Extract content from the response
        try:
            data = json.loads(resp_body)
            # OpenAI-style response
            if "choices" in data:
                content = data["choices"][0].get("message", {}).get("content", "")
                return ProviderResult(raw_text=content)
            # Gemini-style response
            if "candidates" in data:
                parts = data["candidates"][0].get("content", {}).get("parts", [])
                text = "".join(p.get("text", "") for p in parts)
                return ProviderResult(raw_text=text)
            # Fallback: return raw body
            return ProviderResult(raw_text=resp_body)
        except (json.JSONDecodeError, KeyError, IndexError) as e:
            return ProviderResult(raw_text=resp_body)

    except (OSError, http.client.HTTPException) as e:
        return ProviderResult(error=f"API connection failed: {type(e).__name__}")


# ---------------------------------------------------------------------------
# Shared utilities
# ---------------------------------------------------------------------------


def which(cmd: str) -> Optional[str]:
    """Find command in PATH with enriched fallback locations."""
    result = shutil.which(cmd)
    if result:
        return result

    home = Path.home()
    extra_dirs: list[Path] = [home / ".local" / "bin", home / ".cargo" / "bin"]
    system = platform.system()

    if system == "Darwin":
        extra_dirs += [Path("/opt/homebrew/bin"), Path("/usr/local/bin")]
    elif system == "Windows":
        local_appdata = os.environ.get("LOCALAPPDATA")
        program_files = os.environ.get("PROGRAMFILES")
        if local_appdata:
            extra_dirs += [
                Path(local_appdata) / "Programs" / "Python",
                Path(local_appdata) / "Microsoft" / "WindowsApps",
            ]
        if program_files:
            extra_dirs += [
                Path(program_files) / "Git" / "cmd",
                Path(program_files) / "Python",
            ]

    for d in extra_dirs:
        if not d.exists() or not d.is_dir():
            continue
        candidate = d / cmd
        if system == "Windows" and not candidate.suffix:
            candidate = candidate.with_suffix(".exe")
        if candidate.exists() and candidate.is_file():
            return str(candidate)

    return None


def get_vault_path() -> str:
    """Resolve vault path: JARVIS_VAULT_PATH env -> config.json -> cwd."""
    env_vault = os.environ.get("JARVIS_VAULT_PATH")
    if env_vault and os.path.isdir(env_vault):
        return env_vault

    jarvis_home = os.environ.get("JARVIS_HOME")
    config_path = (
        Path(jarvis_home) / "config.json"
        if jarvis_home
        else Path.home() / ".jarvis" / "config.json"
    )
    if config_path.exists():
        try:
            with open(config_path) as f:
                vault_path = json.load(f).get("vault_path")
            if vault_path:
                expanded = os.path.expanduser(vault_path)
                if os.path.isdir(expanded):
                    return expanded
        except (json.JSONDecodeError, OSError):
            pass

    return os.getcwd()


def resolve_working_directory(cwd: Optional[str]) -> str:
    """Resolve and validate the working directory.

    Falls back to vault path if cwd is None, missing, or outside the vault.
    """
    vault = get_vault_path()

    if not cwd:
        return vault

    expanded = os.path.realpath(os.path.expanduser(cwd))

    if not os.path.isdir(expanded):
        return vault

    vault_real = os.path.realpath(vault)
    if not expanded.startswith(vault_real + os.sep) and expanded != vault_real:
        return vault

    return expanded
