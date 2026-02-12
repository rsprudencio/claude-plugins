"""Document chunking for granular semantic search.

Splits documents into heading-based chunks for more precise embeddings.
Supports Markdown and Org-mode formats via format_support module.
Falls back to paragraph-based splitting for headingless files.

Algorithm:
1. Strip frontmatter/properties
2. If total content < min_chunk_chars: return as single chunk
3. Find heading positions (code-block aware, format-specific)
4. Split at headings, then split oversized sections at paragraph boundaries
5. If no headings: split at paragraph boundaries
6. Merge undersized chunks into predecessor
7. Assign sequential indices
"""
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple


@dataclass
class Chunk:
    """A single chunk of a markdown document."""
    index: int
    heading: str
    heading_level: int
    content: str
    char_count: int


@dataclass
class ChunkResult:
    """Result of chunking a markdown document."""
    chunks: List[Chunk]
    total: int
    source_chars: int
    was_chunked: bool


# Default config values
_DEFAULT_MIN_CHARS = 200
_DEFAULT_MAX_CHARS = 1500
_DEFAULT_HEADING_LEVELS = (2, 3)


def chunk_document(content: str, config: Optional[dict] = None,
                   fmt: str = "markdown") -> ChunkResult:
    """Split a document into heading-based chunks (format-aware).

    Args:
        content: Full document text
        config: Optional overrides for min_chunk_chars, max_chunk_chars, heading_levels
        fmt: Format string ('markdown' or 'org')

    Returns:
        ChunkResult with list of Chunk objects
    """
    from .format_support import strip_frontmatter, find_heading_positions

    config = config or {}
    min_chars = config.get("min_chunk_chars", _DEFAULT_MIN_CHARS)
    max_chars = config.get("max_chunk_chars", _DEFAULT_MAX_CHARS)
    heading_levels = tuple(config.get("heading_levels", _DEFAULT_HEADING_LEVELS))
    enabled = config.get("enabled", True)

    source_chars = len(content)
    stripped = strip_frontmatter(content, fmt)

    # If disabled or content too short, return as single chunk
    if not enabled or len(stripped.strip()) < min_chars:
        return ChunkResult(
            chunks=[Chunk(index=0, heading="", heading_level=0,
                          content=stripped.strip(), char_count=len(stripped.strip()))],
            total=1,
            source_chars=source_chars,
            was_chunked=False,
        )

    # Find heading positions (format-aware)
    positions = find_heading_positions(stripped, heading_levels, fmt)

    if positions:
        raw_chunks = _split_at_positions(stripped, positions)
        # Split oversized chunks at paragraph boundaries
        split_chunks = []
        for heading, level, text in raw_chunks:
            if len(text) > max_chars:
                para_chunks = _split_by_paragraphs(text, max_chars)
                for i, para_text in enumerate(para_chunks):
                    h = heading if i == 0 else f"{heading} (cont.)" if heading else ""
                    split_chunks.append((h, level, para_text))
            else:
                split_chunks.append((heading, level, text))
        raw_chunks = split_chunks
    else:
        # No headings: paragraph-based splitting
        para_chunks = _split_by_paragraphs(stripped, max_chars)
        raw_chunks = [("", 0, text) for text in para_chunks]

    # Merge undersized chunks
    merged = _merge_small_chunks(raw_chunks, min_chars)

    # Build Chunk objects
    chunks = []
    for i, (heading, level, text) in enumerate(merged):
        text = text.strip()
        if text:  # skip empty
            chunks.append(Chunk(
                index=i,
                heading=heading,
                heading_level=level,
                content=text,
                char_count=len(text),
            ))

    # Re-index sequentially
    for i, chunk in enumerate(chunks):
        chunk.index = i

    if not chunks:
        # Fallback: return stripped content as single chunk
        return ChunkResult(
            chunks=[Chunk(index=0, heading="", heading_level=0,
                          content=stripped.strip(), char_count=len(stripped.strip()))],
            total=1,
            source_chars=source_chars,
            was_chunked=False,
        )

    was_chunked = len(chunks) > 1
    return ChunkResult(
        chunks=chunks,
        total=len(chunks),
        source_chars=source_chars,
        was_chunked=was_chunked,
    )


def _strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter from markdown content."""
    return re.sub(r'^---\s*\n.*?\n---\s*\n', '', content, count=1, flags=re.DOTALL)


def _find_heading_positions(content: str, heading_levels: tuple) -> List[Tuple[int, int, str]]:
    """Find heading positions that are not inside fenced code blocks.

    Returns list of (offset, level, heading_text) tuples sorted by offset.
    """
    # Find all fenced code block ranges
    code_ranges = []
    for m in re.finditer(r'^(`{3,}|~{3,}).*?\n.*?^\1\s*$', content, re.MULTILINE | re.DOTALL):
        code_ranges.append((m.start(), m.end()))

    def in_code_block(pos: int) -> bool:
        for start, end in code_ranges:
            if start <= pos < end:
                return True
        return False

    # Build heading pattern for requested levels
    levels_pattern = '|'.join(f'{"#" * lvl}' for lvl in sorted(heading_levels))
    pattern = rf'^({levels_pattern})\s+(.+)$'

    positions = []
    for m in re.finditer(pattern, content, re.MULTILINE):
        if not in_code_block(m.start()):
            level = len(m.group(1))
            text = m.group(2).strip()
            positions.append((m.start(), level, text))

    return positions


def _split_at_positions(content: str, positions: List[Tuple[int, int, str]]) -> List[Tuple[str, int, str]]:
    """Split content at heading positions into (heading, level, text) tuples.

    The text before the first heading becomes a preamble chunk with empty heading.
    """
    chunks = []

    # Preamble (content before first heading)
    if positions and positions[0][0] > 0:
        preamble = content[:positions[0][0]]
        if preamble.strip():
            chunks.append(("", 0, preamble))

    for i, (offset, level, heading) in enumerate(positions):
        # Text runs from this heading to the next heading (or end)
        end = positions[i + 1][0] if i + 1 < len(positions) else len(content)
        text = content[offset:end]
        chunks.append((heading, level, text))

    return chunks


def _split_by_paragraphs(content: str, max_chars: int) -> List[str]:
    """Split content at paragraph boundaries (double newlines).

    Merges paragraphs greedily up to max_chars per chunk.
    """
    paragraphs = re.split(r'\n\n+', content)
    paragraphs = [p.strip() for p in paragraphs if p.strip()]

    if not paragraphs:
        return [content] if content.strip() else []

    chunks = []
    current = paragraphs[0]

    for para in paragraphs[1:]:
        candidate = current + "\n\n" + para
        if len(candidate) <= max_chars:
            current = candidate
        else:
            if current.strip():
                chunks.append(current)
            current = para

    if current.strip():
        chunks.append(current)

    return chunks


def _merge_small_chunks(
    raw_chunks: List[Tuple[str, int, str]],
    min_chars: int,
) -> List[Tuple[str, int, str]]:
    """Merge undersized chunks into their predecessor.

    Walks the list and merges any chunk smaller than min_chars with the
    previous chunk. The merged chunk keeps the predecessor's heading.
    """
    if not raw_chunks:
        return []

    merged = [raw_chunks[0]]

    for heading, level, text in raw_chunks[1:]:
        if len(text.strip()) < min_chars and merged:
            # Merge into predecessor
            prev_heading, prev_level, prev_text = merged[-1]
            merged[-1] = (prev_heading, prev_level, prev_text + "\n\n" + text)
        else:
            merged.append((heading, level, text))

    return merged


# Backward-compatible alias
def chunk_markdown(content: str, config: Optional[dict] = None) -> ChunkResult:
    """Split a markdown document into heading-based chunks.

    Backward-compatible wrapper around chunk_document(). New code should
    use chunk_document() directly with an explicit fmt parameter.
    """
    return chunk_document(content, config, fmt="markdown")
