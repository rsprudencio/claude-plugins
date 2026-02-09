"""Tests for markdown chunking module."""
import pytest
from tools.chunking import (
    chunk_markdown,
    _strip_frontmatter,
    _find_heading_positions,
    _split_at_positions,
    _split_by_paragraphs,
    _merge_small_chunks,
    Chunk,
    ChunkResult,
)


class TestStripFrontmatter:
    """Tests for YAML frontmatter removal."""

    def test_removes_frontmatter(self):
        content = "---\ntitle: Test\ntype: note\n---\n# Title\n\nBody text."
        result = _strip_frontmatter(content)
        assert "---" not in result
        assert "# Title" in result
        assert "Body text." in result

    def test_no_frontmatter_unchanged(self):
        content = "# Title\n\nBody text."
        assert _strip_frontmatter(content) == content

    def test_preserves_later_hr(self):
        content = "---\ntype: note\n---\n# Title\n\n---\n\nSeparator above."
        result = _strip_frontmatter(content)
        assert "---" in result  # The HR separator remains
        assert "Separator above." in result


class TestFindHeadingPositions:
    """Tests for heading detection."""

    def test_finds_h2_and_h3(self):
        content = "# Title\n\n## Section 1\n\nText.\n\n### Sub 1.1\n\nMore.\n\n## Section 2\n\nEnd."
        positions = _find_heading_positions(content, (2, 3))
        assert len(positions) == 3
        assert positions[0][2] == "Section 1"
        assert positions[0][1] == 2
        assert positions[1][2] == "Sub 1.1"
        assert positions[1][1] == 3
        assert positions[2][2] == "Section 2"

    def test_ignores_h1(self):
        content = "# Title\n\n## Real Section\n\nText."
        positions = _find_heading_positions(content, (2, 3))
        assert len(positions) == 1
        assert positions[0][2] == "Real Section"

    def test_skips_headings_in_code_blocks(self):
        content = "## Real Heading\n\nText.\n\n```markdown\n## Fake Heading\n\nCode example.\n```\n\n## Another Real\n\nEnd."
        positions = _find_heading_positions(content, (2, 3))
        headings = [p[2] for p in positions]
        assert "Real Heading" in headings
        assert "Another Real" in headings
        assert "Fake Heading" not in headings

    def test_tilde_code_blocks(self):
        content = "## Before\n\n~~~\n## Inside Tilde Block\n~~~\n\n## After"
        positions = _find_heading_positions(content, (2, 3))
        headings = [p[2] for p in positions]
        assert "Inside Tilde Block" not in headings
        assert len(headings) == 2

    def test_no_headings_returns_empty(self):
        content = "Just some text without any headings."
        positions = _find_heading_positions(content, (2, 3))
        assert positions == []

    def test_only_requested_levels(self):
        content = "## H2\n\n### H3\n\n#### H4\n\n"
        positions = _find_heading_positions(content, (2,))
        assert len(positions) == 1
        assert positions[0][2] == "H2"


class TestSplitAtPositions:
    """Tests for position-based splitting."""

    def test_preamble_preserved(self):
        content = "Intro text.\n\n## Section 1\n\nBody 1.\n\n## Section 2\n\nBody 2."
        positions = _find_heading_positions(content, (2, 3))
        chunks = _split_at_positions(content, positions)
        assert chunks[0][0] == ""  # preamble has empty heading
        assert "Intro text." in chunks[0][2]
        assert chunks[1][0] == "Section 1"
        assert chunks[2][0] == "Section 2"

    def test_no_preamble(self):
        content = "## Section 1\n\nBody.\n\n## Section 2\n\nMore."
        positions = _find_heading_positions(content, (2, 3))
        chunks = _split_at_positions(content, positions)
        assert chunks[0][0] == "Section 1"
        assert len(chunks) == 2

    def test_section_includes_heading_line(self):
        content = "## Section\n\nBody text here."
        positions = _find_heading_positions(content, (2, 3))
        chunks = _split_at_positions(content, positions)
        assert "## Section" in chunks[0][2]
        assert "Body text here." in chunks[0][2]


class TestSplitByParagraphs:
    """Tests for paragraph-based splitting."""

    def test_basic_paragraph_split(self):
        content = "Para 1 text.\n\nPara 2 text.\n\nPara 3 text."
        chunks = _split_by_paragraphs(content, max_chars=30)
        assert len(chunks) >= 2

    def test_small_content_single_chunk(self):
        content = "Short text."
        chunks = _split_by_paragraphs(content, max_chars=1000)
        assert len(chunks) == 1

    def test_respects_max_chars(self):
        # Each paragraph is about 30 chars
        content = "A" * 100 + "\n\n" + "B" * 100 + "\n\n" + "C" * 100
        chunks = _split_by_paragraphs(content, max_chars=150)
        for chunk in chunks:
            assert len(chunk) <= 200  # Some tolerance for boundary

    def test_empty_content(self):
        chunks = _split_by_paragraphs("", max_chars=100)
        assert chunks == []


class TestMergeSmallChunks:
    """Tests for undersized chunk merging."""

    def test_merges_tiny_into_predecessor(self):
        raw = [("H1", 2, "Long enough content here."), ("H2", 2, "Tiny")]
        merged = _merge_small_chunks(raw, min_chars=10)
        assert len(merged) == 1
        assert "Tiny" in merged[0][2]

    def test_keeps_large_chunks_separate(self):
        raw = [("H1", 2, "Content A " * 30), ("H2", 2, "Content B " * 30)]
        merged = _merge_small_chunks(raw, min_chars=10)
        assert len(merged) == 2

    def test_empty_input(self):
        assert _merge_small_chunks([], min_chars=10) == []

    def test_single_chunk_unchanged(self):
        raw = [("H1", 2, "Only chunk")]
        merged = _merge_small_chunks(raw, min_chars=100)
        assert len(merged) == 1


class TestChunkMarkdown:
    """Integration tests for the full chunking pipeline."""

    def test_short_document_no_split(self):
        content = "# Title\n\nShort body."
        result = chunk_markdown(content)
        assert result.total == 1
        assert result.was_chunked is False
        assert result.chunks[0].heading == ""

    def test_document_with_headings_splits(self):
        sections = ["## Section {}\n\n{}".format(i, "Content " * 50)
                     for i in range(5)]
        content = "---\ntype: note\n---\n# Title\n\n" + "\n\n".join(sections)
        result = chunk_markdown(content)
        assert result.was_chunked is True
        assert result.total >= 3
        # Frontmatter should be stripped
        for chunk in result.chunks:
            assert "---" not in chunk.content or "## " in chunk.content

    def test_headingless_document_paragraph_split(self):
        paragraphs = ["Paragraph {} content here. " * 20 for _ in range(10)]
        content = "\n\n".join(paragraphs)
        result = chunk_markdown(content, config={"max_chunk_chars": 500})
        assert result.was_chunked is True
        assert result.total > 1
        for chunk in result.chunks:
            assert chunk.heading == ""  # No headings to detect

    def test_disabled_returns_single_chunk(self):
        content = "## Section\n\nLong content. " * 100
        result = chunk_markdown(content, config={"enabled": False})
        assert result.total == 1
        assert result.was_chunked is False

    def test_chunk_indices_sequential(self):
        sections = ["## Section {}\n\n{}".format(i, "Word " * 80)
                     for i in range(4)]
        content = "\n\n".join(sections)
        result = chunk_markdown(content)
        for i, chunk in enumerate(result.chunks):
            assert chunk.index == i

    def test_chunk_metadata_populated(self):
        content = "Preamble text here.\n\n## Overview\n\nOverview content goes here and is long enough.\n\n## Details\n\nDetails content goes here and is also quite long enough."
        result = chunk_markdown(content, config={"min_chunk_chars": 20})
        for chunk in result.chunks:
            assert isinstance(chunk.char_count, int)
            assert chunk.char_count > 0
            assert isinstance(chunk.index, int)
            assert isinstance(chunk.heading_level, int)

    def test_source_chars_matches_input(self):
        content = "Hello world " * 50
        result = chunk_markdown(content)
        assert result.source_chars == len(content)

    def test_oversized_section_gets_paragraph_split(self):
        # One huge section that exceeds max_chars
        big_section = "## Big Section\n\n" + "\n\n".join(
            ["Paragraph {} content. " * 10 for _ in range(20)]
        )
        result = chunk_markdown(big_section, config={"max_chunk_chars": 300, "min_chunk_chars": 50})
        assert result.was_chunked is True
        assert result.total > 1

    def test_code_block_headings_not_split(self):
        content = (
            "## Real Section\n\nSome text.\n\n"
            "```python\n## Not a heading\ncode_here()\n```\n\n"
            "## Another Section\n\nMore text."
        )
        result = chunk_markdown(content, config={"min_chunk_chars": 10})
        headings = [c.heading for c in result.chunks if c.heading]
        assert "Not a heading" not in headings

    def test_single_chunk_passthrough(self):
        content = "## Title\n\nJust some text."
        result = chunk_markdown(content, config={"min_chunk_chars": 5})
        assert result.total >= 1
        # Content preserved
        full_text = " ".join(c.content for c in result.chunks)
        assert "Just some text." in full_text
