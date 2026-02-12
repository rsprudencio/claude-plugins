"""File format abstraction for Markdown and Org-mode support.

Central dispatch for all format-dependent parsing. The read path detects
format from file extension (mixed vaults work). The write path uses the
configured format from ~/.jarvis/config.json.

Adding a new format requires:
1. Add extension to EXTENSION_MAP
2. Implement _parse_<fmt>_frontmatter, _find_<fmt>_headings, etc.
3. Add format name to VALID_FORMATS
4. Create defaults/formats/<name>.md reference file
"""
import os
import re
from typing import List, Optional, Tuple

from . import config as _config_mod

# --- Format registry ---

VALID_FORMATS = ("md", "org")

EXTENSION_MAP = {
    ".md": "markdown",
    ".org": "org",
}

INDEXABLE_EXTENSIONS = tuple(EXTENSION_MAP.keys())


def detect_format(filename: str) -> str:
    """Detect format from file extension.

    Returns 'markdown' or 'org'. Falls back to 'markdown' for unknown extensions.
    """
    ext = os.path.splitext(filename)[1].lower()
    return EXTENSION_MAP.get(ext, "markdown")


def is_indexable(filename: str) -> bool:
    """True if the file has an indexable extension (.md or .org)."""
    ext = os.path.splitext(filename)[1].lower()
    return ext in EXTENSION_MAP


def get_write_extension() -> str:
    """Return the file extension for new files based on config.

    Returns '.md' or '.org'.
    """
    config = _config_mod.get_config()
    fmt = config.get("file_format", "md")
    if fmt == "org":
        return ".org"
    return ".md"


def get_write_format() -> str:
    """Return the format name for new files based on config.

    Returns 'markdown' or 'org'.
    """
    config = _config_mod.get_config()
    fmt = config.get("file_format", "md")
    if fmt == "org":
        return "org"
    return "markdown"


# --- Frontmatter / property parsing ---

def parse_frontmatter(content: str, fmt: str) -> dict:
    """Parse metadata from content based on format.

    Markdown: YAML frontmatter between --- delimiters.
    Org: :PROPERTIES: drawer at the start of the file.
    """
    if fmt == "org":
        return _parse_org_properties(content)
    return _parse_yaml_frontmatter(content)


def strip_frontmatter(content: str, fmt: str) -> str:
    """Remove the metadata block from content."""
    if fmt == "org":
        return _strip_org_properties(content)
    return _strip_yaml_frontmatter(content)


def generate_frontmatter(metadata: dict, fmt: str) -> str:
    """Generate a metadata block string from a dict.

    Markdown: YAML frontmatter. Org: :PROPERTIES: drawer.
    """
    if fmt == "org":
        return _generate_org_properties(metadata)
    return _generate_yaml_frontmatter(metadata)


# --- Title extraction ---

def extract_title(content: str, filename: str, fmt: str) -> str:
    """Extract title from first heading or fall back to filename."""
    if fmt == "org":
        return _extract_org_title(content, filename)
    return _extract_md_title(content, filename)


# --- Heading detection ---

def find_heading_positions(
    content: str, heading_levels: tuple, fmt: str
) -> List[Tuple[int, int, str]]:
    """Find heading positions, skipping code blocks.

    Returns list of (offset, level, heading_text) sorted by offset.
    """
    if fmt == "org":
        return _find_org_heading_positions(content, heading_levels)
    return _find_md_heading_positions(content, heading_levels)


# --- Code block ranges (for heading detection) ---

def find_code_block_ranges(content: str, fmt: str) -> List[Tuple[int, int]]:
    """Find code block ranges to exclude headings inside them."""
    if fmt == "org":
        return _find_org_code_block_ranges(content)
    return _find_md_code_block_ranges(content)


# =========================================================================
# Markdown implementations
# =========================================================================

def _parse_yaml_frontmatter(content: str) -> dict:
    """Extract YAML frontmatter from markdown content."""
    match = re.match(r'^---\s*\n(.*?)\n---\s*\n', content, re.DOTALL)
    if not match:
        return {}
    fm = {}
    for line in match.group(1).split('\n'):
        if ':' in line and not line.strip().startswith('-'):
            key, _, value = line.partition(':')
            fm[key.strip()] = value.strip().strip('"').strip("'")
    # Extract list-style tags
    tag_match = re.search(r'tags:\s*\n((?:\s+-\s+.*\n)*)', match.group(1) + '\n')
    if tag_match:
        tags = re.findall(r'-\s+(.+)', tag_match.group(1))
        fm['tags'] = ','.join(t.strip().strip('"').strip("'") for t in tags)
    return fm


def _strip_yaml_frontmatter(content: str) -> str:
    """Remove YAML frontmatter from markdown content."""
    return re.sub(r'^---\s*\n.*?\n---\s*\n', '', content, count=1, flags=re.DOTALL)


def _generate_yaml_frontmatter(metadata: dict) -> str:
    """Generate YAML frontmatter string from dict."""
    lines = ["---"]
    for key, value in metadata.items():
        if isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"  - {item}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


def _extract_md_title(content: str, filename: str) -> str:
    """Get title from first H1 heading or filename."""
    match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    if match:
        return match.group(1).strip()
    return os.path.splitext(os.path.basename(filename))[0].replace('-', ' ').title()


def _find_md_heading_positions(
    content: str, heading_levels: tuple
) -> List[Tuple[int, int, str]]:
    """Find Markdown heading positions outside code blocks."""
    code_ranges = _find_md_code_block_ranges(content)

    def in_code_block(pos: int) -> bool:
        for start, end in code_ranges:
            if start <= pos < end:
                return True
        return False

    levels_pattern = '|'.join(f'{"#" * lvl}' for lvl in sorted(heading_levels))
    pattern = rf'^({levels_pattern})\s+(.+)$'

    positions = []
    for m in re.finditer(pattern, content, re.MULTILINE):
        if not in_code_block(m.start()):
            level = len(m.group(1))
            text = m.group(2).strip()
            positions.append((m.start(), level, text))

    return positions


def _find_md_code_block_ranges(content: str) -> List[Tuple[int, int]]:
    """Find fenced code block ranges in Markdown."""
    ranges = []
    for m in re.finditer(r'^(`{3,}|~{3,}).*?\n.*?^\1\s*$', content, re.MULTILINE | re.DOTALL):
        ranges.append((m.start(), m.end()))
    return ranges


# =========================================================================
# Org-mode implementations
# =========================================================================

def _parse_org_properties(content: str) -> dict:
    """Extract :PROPERTIES: drawer from org content.

    Format:
        :PROPERTIES:
        :KEY: value
        :END:
    """
    match = re.match(
        r'^\s*:PROPERTIES:\s*\n(.*?):END:\s*\n',
        content,
        re.DOTALL | re.MULTILINE,
    )
    if not match:
        return {}
    props = {}
    for line in match.group(1).split('\n'):
        line = line.strip()
        prop_match = re.match(r'^:([^:]+):\s*(.*)$', line)
        if prop_match:
            key = prop_match.group(1).strip().lower()
            value = prop_match.group(2).strip()
            props[key] = value
    return props


def _strip_org_properties(content: str) -> str:
    """Remove :PROPERTIES: drawer from org content."""
    return re.sub(
        r'^\s*:PROPERTIES:\s*\n.*?:END:\s*\n',
        '', content, count=1, flags=re.DOTALL | re.MULTILINE,
    )


def _generate_org_properties(metadata: dict) -> str:
    """Generate :PROPERTIES: drawer string from dict.

    Tags are stored as a space-separated :TAGS: property (Org convention).
    """
    lines = [":PROPERTIES:"]
    for key, value in metadata.items():
        if key == "tags" and isinstance(value, list):
            lines.append(f":{key.upper()}: {' '.join(value)}")
        else:
            lines.append(f":{key.upper()}: {value}")
    lines.append(":END:")
    return "\n".join(lines) + "\n"


def _extract_org_title(content: str, filename: str) -> str:
    """Get title from #+TITLE or first top-level heading or filename.

    Org files may use #+TITLE: keyword or * Heading for titles.
    """
    # Try #+TITLE first
    title_match = re.search(r'^#\+TITLE:\s*(.+)$', content, re.MULTILINE | re.IGNORECASE)
    if title_match:
        return title_match.group(1).strip()
    # Try first top-level heading
    heading_match = re.search(r'^\*\s+(.+)$', content, re.MULTILINE)
    if heading_match:
        return heading_match.group(1).strip()
    return os.path.splitext(os.path.basename(filename))[0].replace('-', ' ').title()


def _find_org_heading_positions(
    content: str, heading_levels: tuple
) -> List[Tuple[int, int, str]]:
    """Find Org heading positions outside code blocks.

    Org headings use leading asterisks: * Level 1, ** Level 2, etc.
    """
    code_ranges = _find_org_code_block_ranges(content)

    def in_code_block(pos: int) -> bool:
        for start, end in code_ranges:
            if start <= pos < end:
                return True
        return False

    positions = []
    for m in re.finditer(r'^(\*+)\s+(.+)$', content, re.MULTILINE):
        level = len(m.group(1))
        if level in heading_levels and not in_code_block(m.start()):
            text = m.group(2).strip()
            positions.append((m.start(), level, text))

    return positions


def _find_org_code_block_ranges(content: str) -> List[Tuple[int, int]]:
    """Find #+BEGIN_SRC...#+END_SRC block ranges in Org."""
    ranges = []
    for m in re.finditer(
        r'^#\+BEGIN_SRC.*?\n.*?^#\+END_SRC\s*$',
        content,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    ):
        ranges.append((m.start(), m.end()))
    # Also match #+BEGIN_EXAMPLE...#+END_EXAMPLE and #+BEGIN_QUOTE...#+END_QUOTE
    for m in re.finditer(
        r'^#\+BEGIN_(?:EXAMPLE|QUOTE).*?\n.*?^#\+END_(?:EXAMPLE|QUOTE)\s*$',
        content,
        re.MULTILINE | re.DOTALL | re.IGNORECASE,
    ):
        ranges.append((m.start(), m.end()))
    return sorted(ranges, key=lambda r: r[0])
