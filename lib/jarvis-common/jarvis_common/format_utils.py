"""Minimal format utilities shared across Jarvis MCP servers.

The full format support module (format_support.py) lives in jarvis-core.
This provides only the subset needed by jarvis-obsidian (e.g., is_indexable).
"""

import os

# Map of file extensions to format names
EXTENSION_MAP = {
    ".md": "markdown",
    ".org": "org",
}

INDEXABLE_EXTENSIONS = tuple(EXTENSION_MAP.keys())


def is_indexable(filename: str) -> bool:
    """True if the file has an indexable extension (.md or .org)."""
    ext = os.path.splitext(filename)[1].lower()
    return ext in EXTENSION_MAP
