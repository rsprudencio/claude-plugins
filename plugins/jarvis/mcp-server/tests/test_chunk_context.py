"""Unit tests for the contextual chunk augmentation builder.

Pure-function tests: title extraction, heading trail, cap, determinism, and the
config-off / not-a-chunk passthrough behavior of the augment choke point.
"""

from tools.chunk_context import (
    DEFAULT_MAX_CHARS,
    augment_chunk_for_model,
    build_chunk_context,
)


class TestBuildChunkContext:
    def test_path_title_and_heading(self):
        prefix = build_chunk_context(
            "journal/jarvis/2026/07/mandate.md",
            "Vulnerability management mandate from Igor",
            "Original Request",
        )
        assert prefix == (
            "Document: journal/jarvis/2026/07/mandate.md — "
            "Vulnerability management mandate from Igor › Original Request\n\n"
        )
        # Always ends in a blank-line separator.
        assert prefix.endswith("\n\n")

    def test_path_only(self):
        prefix = build_chunk_context("notes/thing.md")
        # Title falls back to the filename stem, which differs from the path,
        # so it is included.
        assert prefix == "Document: notes/thing.md — thing\n\n"

    def test_title_derived_from_filename_when_absent(self):
        prefix = build_chunk_context("notes/quarterly-review.md", title="")
        assert "— quarterly-review" in prefix

    def test_explicit_title_wins_over_filename(self):
        prefix = build_chunk_context("notes/qr.md", "Quarterly Review")
        assert "— Quarterly Review" in prefix
        assert "qr" not in prefix.split("—", 1)[1]

    def test_title_equal_to_path_not_duplicated(self):
        prefix = build_chunk_context("readme", title="readme")
        # No " — readme" duplicate clause.
        assert prefix == "Document: readme\n\n"

    def test_heading_trail_as_list(self):
        prefix = build_chunk_context(
            "notes/a.md", "A", ["Section One", "Sub A"]
        )
        assert "› Section One › Sub A\n\n" in prefix

    def test_heading_trail_filters_empty(self):
        prefix = build_chunk_context("notes/a.md", "A", ["", "  ", "Real"])
        assert "› Real\n\n" in prefix
        assert "›  ›" not in prefix

    def test_empty_heading_string_omits_clause(self):
        prefix = build_chunk_context("notes/a.md", "A", "")
        assert "›" not in prefix

    def test_no_path_no_title_returns_empty(self):
        assert build_chunk_context("", title="") == ""
        assert build_chunk_context(None, None, None) == ""

    def test_cap_enforced(self):
        long_title = "x" * 500
        prefix = build_chunk_context("notes/a.md", long_title)
        body = prefix.rstrip("\n")
        assert len(body) <= DEFAULT_MAX_CHARS
        assert body.endswith("…")

    def test_custom_cap(self):
        prefix = build_chunk_context("notes/a.md", "A" * 100, max_chars=40)
        assert len(prefix.rstrip("\n")) <= 40

    def test_whitespace_collapsed_to_single_line(self):
        prefix = build_chunk_context(
            "notes/a.md", "Title\nwith  newline", "Head\ting"
        )
        body = prefix.rstrip("\n")
        assert "\n" not in body
        assert "  " not in body

    def test_deterministic(self):
        args = ("notes/a.md", "A Title", ["H1", "H2"])
        assert build_chunk_context(*args) == build_chunk_context(*args)


class TestAugmentChunkForModel:
    def test_chunk_gets_prefix(self):
        out = augment_chunk_for_model(
            "body text",
            path="notes/a.md",
            title="A",
            heading_trail="Intro",
            is_chunk=True,
            enabled=True,
        )
        assert out.startswith("Document: notes/a.md — A › Intro\n\n")
        assert out.endswith("body text")

    def test_disabled_returns_raw(self):
        out = augment_chunk_for_model(
            "body text",
            path="notes/a.md",
            title="A",
            is_chunk=True,
            enabled=False,
        )
        assert out == "body text"

    def test_not_a_chunk_returns_raw(self):
        out = augment_chunk_for_model(
            "whole doc",
            path="notes/a.md",
            title="A",
            is_chunk=False,
            enabled=True,
        )
        assert out == "whole doc"

    def test_no_identity_returns_raw(self):
        out = augment_chunk_for_model(
            "body",
            path="",
            title="",
            is_chunk=True,
            enabled=True,
        )
        assert out == "body"

    def test_none_document_becomes_empty_with_prefix(self):
        out = augment_chunk_for_model(
            None,
            path="notes/a.md",
            title="A",
            is_chunk=True,
            enabled=True,
        )
        assert out.startswith("Document: notes/a.md")
        assert out.endswith("\n\n")

    def test_does_not_mutate_caller_document(self):
        # The choke point returns a new string; the raw stored text elsewhere is
        # untouched (immutable str — this just documents the contract).
        raw = "body text"
        out = augment_chunk_for_model(
            raw, path="notes/a.md", title="A", is_chunk=True, enabled=True
        )
        assert raw == "body text"
        assert out != raw
