"""Tests for memory indexing module."""

import os
import pytest
from tools.memory import (
    _parse_frontmatter_for_file,
    _extract_title_for_file,
    _build_metadata,
    _sanitize_timestamp,
    _should_skip,
    index_vault,
    index_file,
)


class TestSanitizeTimestamp:
    """Tests for frontmatter timestamp sanitization.

    Obsidian writes non-standard timezone suffixes that PostgreSQL's
    ::timestamptz cast rejects; these must be normalized before insert.
    """

    @pytest.mark.parametrize(
        "raw,expected",
        [
            # Common case: zero offset → Z (unchanged legacy behavior)
            ("2026-03-02T14:23:17 (UTC +00:00)", "2026-03-02T14:23:17Z"),
            ("2026-03-02T14:23:17 (GMT +00:00)", "2026-03-02T14:23:17Z"),
            # Bare zone → Z
            ("2026-03-02T14:23:17 (UTC)", "2026-03-02T14:23:17Z"),
            ("2026-03-02T14:23:17 (GMT)", "2026-03-02T14:23:17Z"),
            # Real offsets preserved (normalized to ±HH:MM)
            ("2026-03-02T14:23:17 (UTC +1)", "2026-03-02T14:23:17+01:00"),
            ("2026-03-02T14:23:17 (UTC +05:30)", "2026-03-02T14:23:17+05:30"),
            ("2026-03-02T14:23:17 (UTC -05:00)", "2026-03-02T14:23:17-05:00"),
            ("2026-03-02T14:23:17 (GMT -8)", "2026-03-02T14:23:17-08:00"),
            # No suffix → returned unchanged (stripped)
            ("2026-03-02T14:23:17Z", "2026-03-02T14:23:17Z"),
            ("2026-03-02T14:23:17+02:00", "2026-03-02T14:23:17+02:00"),
        ],
    )
    def test_timezone_suffix_variants(self, raw, expected):
        assert _sanitize_timestamp(raw, "DEFAULT") == expected

    def test_unparseable_string_passes_through(self):
        # Not a recognized suffix → left as-is so the per-row savepoint in
        # _upsert_batch can reject it in isolation (honest failure, not a
        # poisoned batch).
        assert (
            _sanitize_timestamp("totally-not-a-timestamp", "DEFAULT")
            == "totally-not-a-timestamp"
        )

    def test_does_not_strip_unrelated_trailing_paren(self):
        # End-anchored to UTC/GMT only — a non-timezone trailing paren stays.
        val = "2026-03-02T14:23:17 (revised)"
        assert _sanitize_timestamp(val, "DEFAULT") == val

    def test_timezone_only_value_returns_default(self):
        # A value that is nothing but a timezone suffix is not a real
        # timestamp — fall back to default, not a bare offset string.
        assert _sanitize_timestamp("(UTC +05:30)", "DEFAULT") == "DEFAULT"

    def test_none_returns_default(self):
        assert _sanitize_timestamp(None, "DEFAULT") == "DEFAULT"

    def test_unix_millis_converted(self):
        # 1700000000000 ms = 2023-11-14T22:13:20Z
        assert _sanitize_timestamp(1700000000000, "DEFAULT").startswith(
            "2023-11-14T"
        )

    def test_digit_string_treated_as_millis(self):
        assert _sanitize_timestamp("1700000000000", "DEFAULT").startswith(
            "2023-11-14T"
        )


class TestFlushBatchAccounting:
    """_flush_batch must clean up partial files and count all their chunks.

    Guards the regression where per-row isolation could leave a multi-chunk
    file half-indexed (and miscount chunks_total) when one chunk failed.
    """

    def test_partial_file_failure_counts_all_chunks_and_cleans_up(
        self, monkeypatch
    ):
        import tools.memory as memory_mod

        # Batch: file A (2 chunks, succeed) + file B (3 chunks, 1 fails).
        batch_ids = [
            "vault::A.md#chunk-0",
            "vault::A.md#chunk-1",
            "vault::B.md#chunk-0",
            "vault::B.md#chunk-1",
            "vault::B.md#chunk-2",
        ]
        batch_docs = ["a0", "a1", "b0", "b1", "b2"]
        batch_meta = [
            {"parent_file": "A.md"},
            {"parent_file": "A.md"},
            {"parent_file": "B.md"},
            {"parent_file": "B.md"},
            {"parent_file": "B.md"},
        ]
        errors = []

        # Simulate per-row isolation: only B's middle chunk raised.
        monkeypatch.setattr(
            memory_mod,
            "_upsert_batch",
            lambda ids, docs, metas, summaries=None: [
                {
                    "id": "vault::B.md#chunk-1",
                    "parent_file": "B.md",
                    "error": "bad ts",
                }
            ],
        )
        deleted = []
        monkeypatch.setattr(
            memory_mod, "_delete_existing_chunks", lambda p: deleted.append(p)
        )

        files_failed, chunks_failed = memory_mod._flush_batch(
            batch_ids, batch_docs, batch_meta, errors
        )

        assert files_failed == 1  # only file B
        assert chunks_failed == 3  # ALL of B's chunks, not just the 1 that raised
        assert deleted == ["B.md"]  # partial commit cleaned up
        assert errors == [{"file": "B.md", "error": "bad ts"}]  # real path, one entry

    def test_clean_batch_returns_zero_and_no_cleanup(self, monkeypatch):
        import tools.memory as memory_mod

        monkeypatch.setattr(
            memory_mod, "_upsert_batch", lambda i, d, m, s=None: []
        )
        deleted = []
        monkeypatch.setattr(
            memory_mod, "_delete_existing_chunks", lambda p: deleted.append(p)
        )
        errors = []

        result = memory_mod._flush_batch(
            ["vault::A.md#chunk-0"], ["a"], [{"parent_file": "A.md"}], errors
        )

        assert result == (0, 0)
        assert deleted == []
        assert errors == []

    def test_empty_batch_is_noop(self):
        import tools.memory as memory_mod

        assert memory_mod._flush_batch([], [], [], []) == (0, 0)


class TestParseFrontmatter:
    """Tests for YAML frontmatter parsing."""

    def test_basic_frontmatter(self):
        content = "---\ntype: note\nimportance: high\n---\n# Title\nBody"
        fm = _parse_frontmatter_for_file(content, "test.md")
        assert fm["type"] == "note"
        assert fm["importance"] == "high"

    def test_frontmatter_with_tags_list(self):
        content = "---\ntags:\n  - jarvis\n  - work\n  - python\n---\n# Title"
        fm = _parse_frontmatter_for_file(content, "test.md")
        assert "tags" in fm
        assert "jarvis" in fm["tags"]
        assert "work" in fm["tags"]

    def test_no_frontmatter(self):
        content = "# Just a title\n\nSome body text."
        fm = _parse_frontmatter_for_file(content, "test.md")
        assert fm == {}

    def test_quoted_values(self):
        content = '---\njarvis_id: "20260206143052"\ntitle: "My Note"\n---\n'
        fm = _parse_frontmatter_for_file(content, "test.md")
        assert fm["jarvis_id"] == "20260206143052"
        assert fm["title"] == "My Note"


class TestExtractTitle:
    """Tests for title extraction."""

    def test_h1_heading(self):
        content = "---\ntype: note\n---\n# My Great Title\n\nBody here."
        title = _extract_title_for_file(content, "my-file.md")
        assert title == "My Great Title"

    def test_fallback_to_filename(self):
        content = "No heading here, just text."
        title = _extract_title_for_file(content, "my-great-note.md")
        assert title == "My Great Note"

    def test_h2_not_used(self):
        content = "## This is H2, not H1\n\nBody."
        title = _extract_title_for_file(content, "fallback-name.md")
        assert title == "Fallback Name"


class TestBuildMetadata:
    """Tests for metadata construction."""

    def test_universal_fields_present(self):
        meta = _build_metadata({}, "notes/test.md")
        assert meta["source"] == "vault-index"
        assert "created_at" in meta
        assert "updated_at" in meta
        assert meta["chunk_index"] == 0
        assert meta["chunk_total"] == 1

    def test_vault_type_from_frontmatter(self):
        fm = {"type": "incident-log", "tags": "jarvis,work", "importance": "high"}
        meta = _build_metadata(fm, "journal/jarvis/2026/01/entry.md")
        # Old frontmatter type is preserved as vault_type
        assert meta["vault_type"] == "incident-log"
        assert meta["tags"] == "jarvis,work"
        assert meta["importance_score"] == 0.8
        assert meta["directory"] == "journal"
        assert meta["has_frontmatter"] == "true"

    def test_vault_type_inferred_from_directory(self):
        meta = _build_metadata({}, "notes/my-note.md")
        assert meta["vault_type"] == "note"
        assert meta["importance_score"] == 0.5
        assert meta["has_frontmatter"] == "false"

    def test_directory_inference(self):
        assert _build_metadata({}, "journal/test.md")["vault_type"] == "journal"
        assert _build_metadata({}, "work/test.md")["vault_type"] == "work"
        assert _build_metadata({}, "inbox/test.md")["vault_type"] == "inbox"
        assert _build_metadata({}, "random/test.md")["vault_type"] == "random"

    def test_all_inferred_have_vault_type(self):
        """All vault metadata must have a vault_type."""
        for path in ("notes/a.md", "journal/b.md", "work/c.md"):
            meta = _build_metadata({}, path)
            assert "vault_type" in meta

    def test_vault_type_directory_fallback(self):
        """Unknown directories use directory name as vault_type."""
        assert _build_metadata({}, "roadmaps/plan.md")["vault_type"] == "roadmaps"
        assert _build_metadata({}, "docs/readme.md")["vault_type"] == "docs"
        assert (
            _build_metadata({}, ".jarvis/strategic/traj.md")["vault_type"]
            == "strategic"
        )

    def test_vault_type_root_level_file(self):
        """Root-level files (no directory) get vault_type 'document'."""
        assert _build_metadata({}, "README.md")["vault_type"] == "document"


class TestShouldSkip:
    """Tests for file skip logic."""

    def test_skip_obsidian(self):
        assert _should_skip(".obsidian/plugins/foo.md", False) is True

    def test_skip_git(self):
        assert _should_skip(".git/config", False) is True

    def test_skip_templates(self):
        assert _should_skip("templates/daily.md", False) is True

    def test_skip_sensitive_by_default(self):
        assert _should_skip("documents/passport.md", False) is True
        assert _should_skip("people/john.md", False) is True

    def test_include_sensitive_when_requested(self):
        assert _should_skip("documents/passport.md", True) is False
        assert _should_skip("people/john.md", True) is False

    def test_allow_normal_dirs(self):
        assert _should_skip("notes/my-note.md", False) is False
        assert _should_skip("journal/jarvis/2026/01/entry.md", False) is False

    def test_skip_jarvis_strategic(self):
        """DAR F17: .jarvis/strategic/ is skipped to prevent duplicates."""
        assert _should_skip(".jarvis/strategic/test.md", False) is True
        # Non-strategic .jarvis subdirs are still indexed
        assert _should_skip(".jarvis/config/test.md", False) is False


class TestIndexVault:
    """Integration tests for bulk vault indexing."""

    def test_index_vault_requires_config(self, no_config):
        result = index_vault()
        assert result["success"] is False
        assert "no vault_path" in result["error"].lower()

    def test_index_vault_with_files(self, mock_config):
        """Should index .md files with namespaced IDs."""
        # Create test files
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "test-note.md").write_text("# Test Note\n\nSome content here.")
        (notes_dir / "another.md").write_text(
            "---\ntype: idea\n---\n# Another\n\nIdea content."
        )

        result = index_vault()
        assert result["success"] is True
        assert result["files_indexed"] >= 2
        assert result["collection_total"] >= 2

        # Verify IDs have vault:: prefix
        db = mock_config.db
        for doc_id in db.vault_rows.keys():
            assert doc_id.startswith("vault::"), f"ID {doc_id} missing vault:: prefix"

        # Verify vault rows have proper columns and metadata
        for row in db.vault_rows.values():
            assert "vault_type" in row  # column
            assert "parent_file" in row  # column
            assert "created_at" in row["metadata"]

    def test_index_vault_skips_templates(self, mock_config):
        """Should skip templates directory."""
        templates_dir = mock_config.vault_path / "templates"
        templates_dir.mkdir(exist_ok=True)
        (templates_dir / "daily.md").write_text("# Template\n\nContent")

        result = index_vault()
        assert result["success"] is True
        assert result["files_skipped"] >= 1

    def test_index_vault_includes_dot_directories(self, mock_config):
        """Should index files in dot-prefixed directories like .jarvis/ (non-strategic)."""
        dot_dir = mock_config.vault_path / ".jarvis" / "config"
        dot_dir.mkdir(parents=True, exist_ok=True)
        (dot_dir / "test-config.md").write_text("# Test Config\n\nSome config content here.")

        result = index_vault(force=True)
        assert result["success"] is True
        assert result["files_indexed"] >= 1

        # Verify the file is in vault_rows with correct parent_file column
        matching = [
            r for r in mock_config.db.vault_rows.values()
            if r.get("parent_file") == ".jarvis/config/test-config.md"
        ]
        assert len(matching) > 0

    def test_index_vault_skips_strategic_dir(self, mock_config):
        """DAR F17: .jarvis/strategic/ is skipped to prevent duplicates with memory_crud."""
        strategic_dir = mock_config.vault_path / ".jarvis" / "strategic"
        strategic_dir.mkdir(parents=True, exist_ok=True)
        (strategic_dir / "test-values.md").write_text("# Test Values\n\nStrategic content.")

        result = index_vault(force=True)
        assert result["success"] is True
        assert result["files_skipped"] >= 1

        # Verify the strategic file is NOT in vault_rows
        matching = [
            r for r in mock_config.db.vault_rows.values()
            if r.get("parent_file", "").startswith(".jarvis/strategic/")
        ]
        assert len(matching) == 0

    def test_index_vault_skips_serena(self, mock_config):
        """Should skip .serena directory (deprecated Serena memories)."""
        serena_dir = mock_config.vault_path / ".serena" / "memories"
        serena_dir.mkdir(parents=True, exist_ok=True)
        (serena_dir / "old-file.md").write_text("# Old Serena Memory\n\nStale content.")

        result = index_vault()
        assert result["success"] is True
        assert result["files_skipped"] >= 1

    def test_index_vault_skips_files_with_secrets(self, mock_config):
        """Should skip files containing secrets during bulk indexing."""
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "clean.md").write_text("# Clean File\n\nNo secrets here.")
        (notes_dir / "has-secret.md").write_text(
            "# Config\n\naws_key = AKIAIOSFODNN7EXAMPLE\n"
        )

        result = index_vault(force=True)
        assert result["success"] is True
        assert result["files_indexed"] >= 1  # clean.md indexed
        assert result["secrets_skipped"] == 1  # has-secret.md skipped


class TestIndexFile:
    """Tests for single file indexing."""

    def test_index_single_file(self, mock_config):
        """Should index a single file with namespaced ID."""
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "single.md").write_text(
            "# Single File\n\nTest content for indexing."
        )

        result = index_file("notes/single.md")
        assert result["success"] is True
        assert result["id"] == "vault::notes/single.md"
        assert result["title"] == "Single File"
        assert result["chunks"] == 1
        assert result["metadata"]["vault_type"] == "note"

    def test_index_nonexistent_file(self, mock_config):
        result = index_file("notes/does-not-exist.md")
        assert result["success"] is False
        assert "not found" in result["error"].lower()

    def test_index_file_with_secret_skips(self, mock_config):
        """Should refuse to index a file containing secrets."""
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "has-secret.md").write_text(
            "# Config\n\naws_key = AKIAIOSFODNN7EXAMPLE\n"
        )

        result = index_file("notes/has-secret.md")
        assert result["success"] is False
        assert result["error"] == "SECRET_DETECTED"
        assert len(result["detections"]) >= 1

    def test_index_file_secret_detection_disabled(self, mock_config):
        """Should index files with secrets when secret_detection is disabled."""
        mock_config.set(
            memory={
                "secret_detection": False,
            }
        )

        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "has-secret.md").write_text(
            "# Config\n\naws_key = AKIAIOSFODNN7EXAMPLE\n"
        )

        result = index_file("notes/has-secret.md")
        assert result["success"] is True
        assert result["chunks"] >= 1


class TestChunkingIntegration:
    """Tests for chunking integration in the indexing pipeline."""

    def test_index_file_with_headings_creates_chunks(self, mock_config):
        """A file with H2 headings should produce multiple chunks."""
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        content = (
            "---\ntype: note\n---\n# Big Doc\n\n"
            "## Section One\n\n" + "Content A. " * 60 + "\n\n"
            "## Section Two\n\n" + "Content B. " * 60 + "\n\n"
            "## Section Three\n\n" + "Content C. " * 60
        )
        (notes_dir / "chunked.md").write_text(content)

        result = index_file("notes/chunked.md")
        assert result["success"] is True
        assert result["chunks"] >= 2

        # Verify chunk IDs in database
        chunk_ids = [rid for rid in mock_config.db.vault_rows.keys() if "chunked.md" in rid]
        assert len(chunk_ids) >= 2

        # Verify chunk columns
        for rid, row in mock_config.db.vault_rows.items():
            if "chunked.md" in rid:
                assert row["parent_file"] == "notes/chunked.md"
                assert "chunk_heading" in row
                assert int(row["chunk_total"]) >= 2

    def test_index_file_without_headings_single_doc(self, mock_config):
        """Short file without headings should produce a single document."""
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "short.md").write_text("# Short Note\n\nJust a brief note.")

        result = index_file("notes/short.md")
        assert result["success"] is True
        assert result["chunks"] == 1
        assert result["id"] == "vault::notes/short.md"

    def test_index_file_chunk_ids_format(self, mock_config):
        """Multi-chunk IDs should use vault::path#chunk-N format."""
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        content = (
            "## Section A\n\n" + "Alpha content. " * 60 + "\n\n"
            "## Section B\n\n" + "Beta content. " * 60
        )
        (notes_dir / "multi.md").write_text(content)

        result = index_file("notes/multi.md")
        assert result["success"] is True
        assert result["chunks"] >= 2

        multi_ids = [rid for rid in mock_config.db.vault_rows.keys() if "multi.md" in rid]
        for doc_id in multi_ids:
            assert doc_id.startswith("vault::notes/multi.md#chunk-")

    def test_reindex_updates_chunk_count(self, mock_config):
        """Re-indexing a file should clean up old chunks and create new ones."""
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)

        # First index: 3 sections
        content_v1 = "\n\n".join(
            [f"## Section {i}\n\n" + f"Content {i}. " * 60 for i in range(3)]
        )
        (notes_dir / "evolving.md").write_text(content_v1)
        result1 = index_file("notes/evolving.md")

        # Re-index: 2 sections
        content_v2 = "\n\n".join(
            [f"## Section {i}\n\n" + f"Updated content {i}. " * 60 for i in range(2)]
        )
        (notes_dir / "evolving.md").write_text(content_v2)
        result2 = index_file("notes/evolving.md")

        # Old chunks should be cleaned up
        evolving_ids = [rid for rid in mock_config.db.vault_rows.keys() if "evolving.md" in rid]
        assert len(evolving_ids) == result2["chunks"]

    def test_index_vault_with_chunking(self, mock_config):
        """Bulk indexing should also produce chunks."""
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        content = (
            "## Part 1\n\n" + "First part. " * 60 + "\n\n"
            "## Part 2\n\n" + "Second part. " * 60
        )
        (notes_dir / "bulk-test.md").write_text(content)

        result = index_vault()
        assert result["success"] is True
        assert result["files_indexed"] == 1  # 1 file
        assert result["chunks_total"] >= 2  # Multiple chunks from headings

    def test_importance_score_in_metadata(self, mock_config):
        """Indexed files should have importance_score float in metadata."""
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "scored.md").write_text(
            "---\nimportance: high\n---\n# Important Decision\n\nArchitecture decision content."
        )

        result = index_file("notes/scored.md")
        assert result["success"] is True
        meta = result["metadata"]
        assert "importance_score" in meta
        score = float(meta["importance_score"])
        assert 0.0 <= score <= 1.0
        # High frontmatter + "decision"/"architecture" concepts should yield good score
        assert score >= 0.7

    def test_per_chunk_importance_scoring(self, mock_config):
        """Chunks should get individual importance scores based on their content."""
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        # Chunk 0: has "architecture decision" concepts -> higher score
        # Chunk 1: generic filler content -> lower score
        content = (
            "## Architecture Decision\n\n"
            + "This is a critical architecture decision about the system. " * 30
            + "\n\n"
            "## Shopping List\n\n" + "Buy milk and eggs from the store. " * 30
        )
        (notes_dir / "mixed-importance.md").write_text(content)

        index_file("notes/mixed-importance.md")

        # Group scores by heading prefix (chunk_heading and importance_score are columns)
        arch_scores = [
            float(r["importance_score"])
            for r in mock_config.db.vault_rows.values()
            if r.get("chunk_heading", "").startswith("Architecture Decision")
        ]
        shopping_scores = [
            float(r["importance_score"])
            for r in mock_config.db.vault_rows.values()
            if r.get("chunk_heading", "").startswith("Shopping List")
        ]
        assert len(arch_scores) >= 1
        assert len(shopping_scores) >= 1
        # "architecture decision" concepts should score higher than generic filler
        assert max(arch_scores) > max(shopping_scores)

    def test_parent_file_column(self, mock_config):
        """All indexed chunks should have parent_file as a column."""
        notes_dir = mock_config.vault_path / "notes"
        notes_dir.mkdir(exist_ok=True)
        (notes_dir / "parent-test.md").write_text("# Simple\n\nJust content.")

        index_file("notes/parent-test.md")

        for row in mock_config.db.vault_rows.values():
            assert row.get("parent_file") == "notes/parent-test.md"


class TestVaultDocumentColumns:
    """Tests for vault-specific columns in indexed documents."""

    def test_index_file_has_vault_type_column(self, mock_config):
        """Indexed files should have vault_type as a column."""
        test_file = mock_config.vault_path / "notes" / "test-col.md"
        test_file.parent.mkdir(parents=True, exist_ok=True)
        test_file.write_text("# Test Column\nTesting vault columns")

        result = index_file("notes/test-col.md")
        assert result["success"]
        assert result["metadata"]["vault_type"] == "note"
