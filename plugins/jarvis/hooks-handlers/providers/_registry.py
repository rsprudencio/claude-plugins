"""Provider registry — maps provider names to adapter instances.

The registry is the single source of truth for which providers are
available. New providers are added here and automatically become
available to the adversarial review system.
"""

from .codex import CodexProvider
from .gemini import GeminiProvider

# Provider name -> adapter instance
REGISTRY: dict[str, object] = {
    "codex": CodexProvider(),
    "gemini": GeminiProvider(),
}


def resolve_provider(name: str | None = None):
    """Look up a provider adapter by name, or auto-select the first available.

    When name is None, returns the first provider with an available CLI
    or API key. Falls back to the first registered provider if none are
    fully available (let the caller handle the error).

    Returns:
        The provider adapter instance, or None if not found.
    """
    if name is not None:
        return REGISTRY.get(name)

    # Auto-select: prefer providers with a CLI available
    for adapter in REGISTRY.values():
        available, _ = adapter.is_available()
        if available:
            return adapter

    # Fallback: prefer providers with an API key
    for adapter in REGISTRY.values():
        if adapter.has_api_key():
            return adapter

    # Last resort: return first registered (will error at invocation)
    return next(iter(REGISTRY.values()), None)
