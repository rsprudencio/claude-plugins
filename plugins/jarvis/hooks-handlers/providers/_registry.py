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


def resolve_provider(name: str):
    """Look up a provider adapter by name.

    Returns:
        The provider adapter instance, or None if not found.
    """
    return REGISTRY.get(name)
