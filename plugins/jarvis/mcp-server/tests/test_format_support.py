"""Tests for file format support module (Markdown + Org-mode)."""
import pytest
from tools.format_support import (
    detect_format,
    is_indexable,
    get_write_extension,
    get_write_format,
    parse_frontmatter,
    strip_frontmatter,
    generate_frontmatter,
    extract_title,
    find_heading_positions,
    find_code_block_ranges,
    INDEXABLE_EXTENSIONS,
    EXTENSION_MAP,
    VALID_FORMATS,
)


# ── Format Detection ──────────────────────────────────


class TestDetectFormat:
    """Tests for format detection from file extension."""

    def test_md_extension(self):
        assert detect_format("notes/my-note.md") == "markdown"

    def test_org_extension(self):
        assert detect_format("notes/my-note.org") == "org"

    def test_uppercase_md(self):
        assert detect_format("README.MD") == "markdown"

    def test_uppercase_org(self):
        assert detect_format("notes/FILE.ORG") == "org"

    def test_unknown_extension_defaults_to_markdown(self):
        assert detect_format("file.txt") == "markdown"

    def test_no_extension_defaults_to_markdown(self):
        assert detect_format("Makefile") == "markdown"

    def test_path_with_dots(self):
        assert detect_format("some.project/notes.md") == "markdown"


class TestIsIndexable:
    """Tests for indexable extension check."""

    def test_md_is_indexable(self):
        assert is_indexable("notes/note.md") is True

    def test_org_is_indexable(self):
        assert is_indexable("notes/note.org") is True

    def test_txt_not_indexable(self):
        assert is_indexable("file.txt") is False

    def test_no_extension_not_indexable(self):
        assert is_indexable("Makefile") is False

    def test_json_not_indexable(self):
        assert is_indexable("config.json") is False


class TestGetWriteExtension:
    """Tests for config-driven write extension."""

    def test_default_is_md(self, mock_config):
        assert get_write_extension() == ".md"

    def test_org_config(self, mock_config):
        mock_config.set(file_format="org")
        assert get_write_extension() == ".org"

    def test_md_config(self, mock_config):
        mock_config.set(file_format="md")
        assert get_write_extension() == ".md"

    def test_invalid_format_defaults_to_md(self, mock_config):
        mock_config.set(file_format="asciidoc")
        assert get_write_extension() == ".md"


class TestGetWriteFormat:
    """Tests for config-driven write format name."""

    def test_default_is_markdown(self, mock_config):
        assert get_write_format() == "markdown"

    def test_org_config(self, mock_config):
        mock_config.set(file_format="org")
        assert get_write_format() == "org"


# ── Constants ─────────────────────────────────────────


class TestConstants:

    def test_indexable_extensions_include_md(self):
        assert ".md" in INDEXABLE_EXTENSIONS

    def test_indexable_extensions_include_org(self):
        assert ".org" in INDEXABLE_EXTENSIONS

    def test_extension_map_has_md(self):
        assert EXTENSION_MAP[".md"] == "markdown"

    def test_extension_map_has_org(self):
        assert EXTENSION_MAP[".org"] == "org"

    def test_valid_formats(self):
        assert "md" in VALID_FORMATS
        assert "org" in VALID_FORMATS


# ── Markdown Parsing ──────────────────────────────────


class TestMarkdownFrontmatter:
    """Tests for YAML frontmatter parsing."""

    def test_basic_frontmatter(self):
        content = "---\ntitle: Test\ntype: note\n---\n# Title\nBody."
        fm = parse_frontmatter(content, "markdown")
        assert fm["title"] == "Test"
        assert fm["type"] == "note"

    def test_no_frontmatter(self):
        content = "# Title\nBody."
        fm = parse_frontmatter(content, "markdown")
        assert fm == {}

    def test_tags_list(self):
        content = "---\ntags:\n  - foo\n  - bar\n---\nBody."
        fm = parse_frontmatter(content, "markdown")
        assert "foo" in fm.get("tags", "")
        assert "bar" in fm.get("tags", "")

    def test_strip_frontmatter(self):
        content = "---\ntitle: Test\n---\n# Title\nBody."
        stripped = strip_frontmatter(content, "markdown")
        assert "---" not in stripped
        assert "# Title" in stripped


class TestMarkdownTitle:
    """Tests for Markdown title extraction."""

    def test_h1_title(self):
        content = "---\ntitle: FM\n---\n# My Title\nBody."
        assert extract_title(content, "test.md", "markdown") == "My Title"

    def test_no_heading_uses_filename(self):
        content = "Just body text."
        assert extract_title(content, "my-note.md", "markdown") == "My Note"

    def test_filename_with_path(self):
        content = "Body text."
        assert extract_title(content, "notes/cool-idea.md", "markdown") == "Cool Idea"


class TestMarkdownHeadings:
    """Tests for Markdown heading detection."""

    def test_finds_h2_h3(self):
        content = "# Title\n\n## Section\n\nText.\n\n### Sub\n\nMore."
        positions = find_heading_positions(content, (2, 3), "markdown")
        assert len(positions) == 2
        assert positions[0][1] == 2  # level
        assert positions[0][2] == "Section"
        assert positions[1][1] == 3
        assert positions[1][2] == "Sub"

    def test_skips_code_blocks(self):
        content = "## Real\n\n```\n## Not a heading\n```\n\n## Also Real"
        positions = find_heading_positions(content, (2,), "markdown")
        assert len(positions) == 2
        assert positions[0][2] == "Real"
        assert positions[1][2] == "Also Real"


class TestMarkdownCodeBlocks:
    """Tests for Markdown code block range detection."""

    def test_fenced_blocks(self):
        content = "Text\n\n```python\ncode\n```\n\nMore text"
        ranges = find_code_block_ranges(content, "markdown")
        assert len(ranges) == 1

    def test_no_code_blocks(self):
        content = "Just text, no code."
        ranges = find_code_block_ranges(content, "markdown")
        assert len(ranges) == 0


class TestMarkdownFrontmatterGeneration:
    """Tests for YAML frontmatter generation."""

    def test_basic_generation(self):
        result = generate_frontmatter({"type": "note", "title": "Test"}, "markdown")
        assert result.startswith("---\n")
        assert result.endswith("---\n")
        assert "type: note" in result
        assert "title: Test" in result

    def test_tags_list(self):
        result = generate_frontmatter({"tags": ["foo", "bar"]}, "markdown")
        assert "tags:" in result
        assert "  - foo" in result
        assert "  - bar" in result


# ── Org-mode Parsing ──────────────────────────────────


class TestOrgProperties:
    """Tests for Org-mode :PROPERTIES: drawer parsing."""

    def test_basic_properties(self):
        content = ":PROPERTIES:\n:TYPE: note\n:TITLE: Test\n:END:\n* Title\nBody."
        props = parse_frontmatter(content, "org")
        assert props["type"] == "note"
        assert props["title"] == "Test"

    def test_no_properties(self):
        content = "* Title\nBody."
        props = parse_frontmatter(content, "org")
        assert props == {}

    def test_empty_value(self):
        content = ":PROPERTIES:\n:KEY:\n:END:\nBody."
        props = parse_frontmatter(content, "org")
        assert props["key"] == ""

    def test_value_with_spaces(self):
        content = ":PROPERTIES:\n:TITLE: My Great Title\n:END:\nBody."
        props = parse_frontmatter(content, "org")
        assert props["title"] == "My Great Title"

    def test_strip_properties(self):
        content = ":PROPERTIES:\n:TYPE: note\n:END:\n* Title\nBody."
        stripped = strip_frontmatter(content, "org")
        assert ":PROPERTIES:" not in stripped
        assert ":END:" not in stripped
        assert "* Title" in stripped

    def test_keys_lowercased(self):
        content = ":PROPERTIES:\n:IMPORTANCE: high\n:END:\nBody."
        props = parse_frontmatter(content, "org")
        assert "importance" in props
        assert props["importance"] == "high"


class TestOrgTitle:
    """Tests for Org-mode title extraction."""

    def test_title_keyword(self):
        content = "#+TITLE: My Org Note\n* Heading\nBody."
        assert extract_title(content, "test.org", "org") == "My Org Note"

    def test_title_keyword_case_insensitive(self):
        content = "#+title: lowercase title\n* Heading"
        assert extract_title(content, "test.org", "org") == "lowercase title"

    def test_first_heading(self):
        content = "* My Heading\n\nBody text."
        assert extract_title(content, "test.org", "org") == "My Heading"

    def test_no_title_uses_filename(self):
        content = "Just body text with no headings."
        assert extract_title(content, "my-note.org", "org") == "My Note"

    def test_title_preferred_over_heading(self):
        content = "#+TITLE: Title Wins\n* Heading Loses\nBody."
        assert extract_title(content, "test.org", "org") == "Title Wins"


class TestOrgHeadings:
    """Tests for Org-mode heading detection."""

    def test_finds_level_2_and_3(self):
        content = "* Title\n\n** Section\n\nText.\n\n*** Sub\n\nMore."
        positions = find_heading_positions(content, (2, 3), "org")
        assert len(positions) == 2
        assert positions[0][1] == 2  # level
        assert positions[0][2] == "Section"
        assert positions[1][1] == 3
        assert positions[1][2] == "Sub"

    def test_ignores_wrong_levels(self):
        content = "* Title\n** Keep\n*** Ignore\n**** Ignore"
        positions = find_heading_positions(content, (2,), "org")
        assert len(positions) == 1
        assert positions[0][2] == "Keep"

    def test_skips_src_blocks(self):
        content = "** Real\n\n#+BEGIN_SRC python\n** Not a heading\n#+END_SRC\n\n** Also Real"
        positions = find_heading_positions(content, (2,), "org")
        assert len(positions) == 2
        assert positions[0][2] == "Real"
        assert positions[1][2] == "Also Real"


class TestOrgCodeBlocks:
    """Tests for Org-mode code block range detection."""

    def test_src_blocks(self):
        content = "Text\n\n#+BEGIN_SRC python\ncode\n#+END_SRC\n\nMore"
        ranges = find_code_block_ranges(content, "org")
        assert len(ranges) == 1

    def test_example_blocks(self):
        content = "Text\n\n#+BEGIN_EXAMPLE\nexample\n#+END_EXAMPLE\n\nMore"
        ranges = find_code_block_ranges(content, "org")
        assert len(ranges) == 1

    def test_case_insensitive(self):
        content = "#+begin_src python\ncode\n#+end_src"
        ranges = find_code_block_ranges(content, "org")
        assert len(ranges) == 1

    def test_no_blocks(self):
        content = "Just text."
        ranges = find_code_block_ranges(content, "org")
        assert len(ranges) == 0


class TestOrgFrontmatterGeneration:
    """Tests for Org :PROPERTIES: drawer generation."""

    def test_basic_generation(self):
        result = generate_frontmatter({"type": "note", "title": "Test"}, "org")
        assert result.startswith(":PROPERTIES:\n")
        assert ":END:" in result
        assert ":TYPE: note" in result
        assert ":TITLE: Test" in result

    def test_tags_space_separated(self):
        result = generate_frontmatter({"tags": ["foo", "bar"]}, "org")
        assert ":TAGS: foo bar" in result


# ── Cross-Format Dispatch ─────────────────────────────


class TestCrossFormatDispatch:
    """Tests verifying format dispatch works correctly."""

    def test_parse_frontmatter_dispatches_markdown(self):
        content = "---\ntype: note\n---\nBody."
        fm = parse_frontmatter(content, "markdown")
        assert fm["type"] == "note"

    def test_parse_frontmatter_dispatches_org(self):
        content = ":PROPERTIES:\n:TYPE: note\n:END:\nBody."
        fm = parse_frontmatter(content, "org")
        assert fm["type"] == "note"

    def test_strip_dispatches_markdown(self):
        content = "---\ntype: note\n---\nBody."
        result = strip_frontmatter(content, "markdown")
        assert "---" not in result

    def test_strip_dispatches_org(self):
        content = ":PROPERTIES:\n:TYPE: note\n:END:\nBody."
        result = strip_frontmatter(content, "org")
        assert ":PROPERTIES:" not in result

    def test_title_dispatches_markdown(self):
        assert extract_title("# MD Title\nBody", "f.md", "markdown") == "MD Title"

    def test_title_dispatches_org(self):
        assert extract_title("* Org Title\nBody", "f.org", "org") == "Org Title"

    def test_headings_dispatches_markdown(self):
        positions = find_heading_positions("## H2\n### H3", (2, 3), "markdown")
        assert len(positions) == 2

    def test_headings_dispatches_org(self):
        positions = find_heading_positions("** H2\n*** H3", (2, 3), "org")
        assert len(positions) == 2
