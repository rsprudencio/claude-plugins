"""Provider adapter package for external AI CLI tools.

Public API:
    resolve_provider(name) — Look up a provider adapter by name
    ProviderAdapter — Protocol that all adapters implement
    ProviderResult — Result dataclass from provider invocations
    REGISTRY — Dict of provider name -> adapter instance
    invoke_cli — Shared CLI invocation helper
    invoke_api — Shared HTTP API invocation helper
"""

from .base import (
    ProviderAdapter,
    ProviderResult,
    which,
    get_vault_path,
    resolve_working_directory,
    invoke_cli,
    invoke_api,
)
from ._registry import REGISTRY, resolve_provider

__all__ = [
    "ProviderAdapter",
    "ProviderResult",
    "REGISTRY",
    "resolve_provider",
    "which",
    "get_vault_path",
    "resolve_working_directory",
    "invoke_cli",
    "invoke_api",
]
