"""E2E corner case tests — things InMemoryDB mocks cannot catch.

These test real PostgreSQL constraint enforcement, type casting,
trigger behavior, and SQL edge cases that only manifest against
a real database.
"""

import time

import psycopg
import pytest


# ── Constraint enforcement ──────────────────────────────────────────


class TestConstraintEnforcement:
    """Verify CHECK constraints reject invalid data at the SQL level."""

    def test_invalid_category_rejected(self, e2e_config):
        """CHECK (category IN (...)) rejects unknown category values."""
        conn = psycopg.connect(e2e_config["db_url"], autocommit=True)
        emb = e2e_config["embedding_service"].encode("test")

        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """INSERT INTO local.memories
                   (id, document, embedding, category)
                   VALUES (%s, %s, %s::halfvec, %s)""",
                ("test::invalid-cat", "test", str(emb), "INVALID_CATEGORY"),
            )
        conn.close()

    def test_invalid_status_rejected(self, e2e_config):
        """CHECK (status IN (...)) rejects unknown status values."""
        conn = psycopg.connect(e2e_config["db_url"], autocommit=True)
        emb = e2e_config["embedding_service"].encode("test")

        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """INSERT INTO local.memories
                   (id, document, embedding, status)
                   VALUES (%s, %s, %s::halfvec, %s)""",
                ("test::invalid-status", "test", str(emb), "archived"),
            )
        conn.close()

    def test_importance_out_of_range_rejected(self, e2e_config):
        """CHECK (importance_score >= 0.0 AND <= 1.0) rejects out-of-range."""
        conn = psycopg.connect(e2e_config["db_url"], autocommit=True)
        emb = e2e_config["embedding_service"].encode("test")

        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """INSERT INTO local.memories
                   (id, document, embedding, importance_score)
                   VALUES (%s, %s, %s::halfvec, %s)""",
                ("test::bad-importance", "test", str(emb), 1.5),
            )
        conn.close()

    def test_importance_boundary_values_accepted(self, e2e_config):
        """Boundary values 0.0 and 1.0 are valid."""
        from tools.content import content_write

        r0 = content_write(
            content="min importance",
            content_type="observation",
            importance_score=0.0,
            skip_secret_scan=True,
        )
        assert r0["success"] is True

        r1 = content_write(
            content="max importance",
            content_type="observation",
            importance_score=1.0,
            skip_secret_scan=True,
        )
        assert r1["success"] is True

    def test_scope_project_requires_project_column(self, e2e_config):
        """CHECK: scope='project' with NULL project violates constraint."""
        conn = psycopg.connect(e2e_config["db_url"], autocommit=True)
        emb = e2e_config["embedding_service"].encode("test")

        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """INSERT INTO local.memories
                   (id, document, embedding, scope, project)
                   VALUES (%s, %s, %s::halfvec, 'project', NULL)""",
                ("test::no-project", "test", str(emb)),
            )
        conn.close()

    def test_scope_project_with_project_succeeds(self, e2e_config):
        """scope='project' + project='my-proj' satisfies constraint."""
        from tools.content import content_write

        result = content_write(
            content="project-scoped content",
            content_type="observation",
            extra_metadata={"scope": "project", "project": "my-proj"},
            skip_secret_scan=True,
        )
        assert result["success"] is True

    def test_superseded_requires_superseded_by(self, e2e_config):
        """CHECK: status='superseded' without superseded_by violates constraint."""
        conn = psycopg.connect(e2e_config["db_url"], autocommit=True)
        emb = e2e_config["embedding_service"].encode("test")

        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                """INSERT INTO local.memories
                   (id, document, embedding, status, superseded_by)
                   VALUES (%s, %s, %s::halfvec, 'superseded', NULL)""",
                ("test::bad-supersede", "test", str(emb)),
            )
        conn.close()


# ── Trigger behavior ──────────────────────────────────────────────


class TestTriggerBehavior:
    """Verify the updated_at trigger fires correctly."""

    def test_trigger_fires_on_upsert_conflict(self, e2e_config):
        """ON CONFLICT DO UPDATE triggers updated_at refresh."""
        from tools.content import content_write

        r1 = content_write(
            content="original version",
            content_type="pattern",
            name="trigger-upsert",
            skip_secret_scan=True,
        )
        assert r1["success"] is True

        conn = psycopg.connect(e2e_config["db_url"], autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT updated_at FROM local.memories WHERE id = %s",
                (r1["id"],),
            )
            ts1 = cur.fetchone()[0]

        # Small delay to ensure timestamp difference
        time.sleep(0.05)

        # Upsert same ID with new content (ON CONFLICT fires trigger)
        r2 = content_write(
            content="updated version",
            content_type="pattern",
            name="trigger-upsert",
            skip_secret_scan=True,
        )
        assert r2["success"] is True
        assert r2["id"] == r1["id"]

        with conn.cursor() as cur:
            cur.execute(
                "SELECT updated_at FROM local.memories WHERE id = %s",
                (r2["id"],),
            )
            ts2 = cur.fetchone()[0]

        assert ts2 > ts1, f"updated_at should advance on upsert: {ts2} <= {ts1}"
        conn.close()

    def test_trigger_fires_on_direct_update(self, e2e_config):
        """Direct UPDATE statement triggers updated_at refresh."""
        conn = psycopg.connect(e2e_config["db_url"], autocommit=True)
        emb = e2e_config["embedding_service"].encode("test")

        conn.execute(
            """INSERT INTO local.memories
               (id, document, embedding, category)
               VALUES (%s, %s, %s::halfvec, 'observation')""",
            ("test::trigger-direct", "original", str(emb)),
        )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT updated_at FROM local.memories WHERE id = %s",
                ("test::trigger-direct",),
            )
            ts1 = cur.fetchone()[0]

        time.sleep(0.05)

        conn.execute(
            "UPDATE local.memories SET document = 'changed' WHERE id = %s",
            ("test::trigger-direct",),
        )

        with conn.cursor() as cur:
            cur.execute(
                "SELECT updated_at FROM local.memories WHERE id = %s",
                ("test::trigger-direct",),
            )
            ts2 = cur.fetchone()[0]

        assert ts2 > ts1
        conn.close()


# ── Soft delete & active view ─────────────────────────────────────


class TestSoftDeleteBehavior:
    """Verify soft delete semantics and active_memories view filtering."""

    def test_soft_deleted_excluded_from_search(self, e2e_config):
        """Soft-deleted records should not appear in query_vault results."""
        from tools.content import content_write, content_delete
        from tools.query import query_vault

        r = content_write(
            content="unique unicorn rainbow content for deletion test",
            content_type="observation",
            importance_score=0.9,
            skip_secret_scan=True,
        )
        assert r["success"] is True

        # Verify it's found before deletion
        result_before = query_vault("unicorn rainbow", n_results=10)
        ids_before = [x["id"] for x in result_before.get("results", [])]
        assert r["id"] in ids_before

        # Soft delete
        content_delete(r["id"], hard=False)

        # Should NOT appear in search results
        result_after = query_vault("unicorn rainbow", n_results=10)
        ids_after = [x["id"] for x in result_after.get("results", [])]
        assert r["id"] not in ids_after

    def test_soft_deleted_still_in_base_table(self, e2e_config):
        """Soft-deleted records still exist in local.memories (not via view)."""
        from tools.content import content_write, content_delete

        r = content_write(
            content="still in base table",
            content_type="observation",
            skip_secret_scan=True,
        )
        content_delete(r["id"], hard=False)

        conn = psycopg.connect(e2e_config["db_url"], autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM local.memories WHERE id = %s",
                (r["id"],),
            )
            row = cur.fetchone()
            assert row is not None, "Row should exist in base table"
            assert row[0] == "deleted"

            # Not in view
            cur.execute(
                "SELECT id FROM local.active_memories WHERE id = %s",
                (r["id"],),
            )
            assert cur.fetchone() is None, "Should not appear in active view"
        conn.close()

    def test_hard_delete_then_reinsert(self, e2e_config):
        """After hard delete, same ID can be reused (PK freed)."""
        from tools.content import content_write, content_read, content_delete

        r = content_write(
            content="will be hard deleted",
            content_type="pattern",
            name="reinsert-test",
            skip_secret_scan=True,
        )
        doc_id = r["id"]
        content_delete(doc_id, hard=True)

        # Verify gone
        read = content_read(doc_id)
        assert read.get("found") is False

        # Re-insert with same ID
        r2 = content_write(
            content="reinserted content",
            content_type="pattern",
            name="reinsert-test",
            skip_secret_scan=True,
        )
        assert r2["success"] is True
        assert r2["id"] == doc_id

        read2 = content_read(doc_id)
        assert read2["found"] is True
        assert read2["content"] == "reinserted content"


# ── Halfvec edge cases ────────────────────────────────────────────


class TestHalfvecEdgeCases:
    """Verify halfvec type behavior with real PostgreSQL."""

    def test_dimension_mismatch_rejected(self, e2e_config):
        """Inserting wrong-dimension embedding causes PG error."""
        conn = psycopg.connect(e2e_config["db_url"], autocommit=True)
        # Schema expects 384d, send a 10d vector
        wrong_dims = [0.1] * 10

        with pytest.raises(psycopg.errors.DataException):
            conn.execute(
                """INSERT INTO local.memories
                   (id, document, embedding, category)
                   VALUES (%s, %s, %s::halfvec, 'observation')""",
                ("test::wrong-dims", "test", str(wrong_dims)),
            )
        conn.close()

    def test_zero_vector_distance(self, e2e_config):
        """Zero vector has defined cosine distance behavior."""
        conn = psycopg.connect(e2e_config["db_url"], autocommit=True)
        # All-zeros vector — cosine distance is NaN (undefined)
        zero_vec = [0.0] * 384
        emb = e2e_config["embedding_service"].encode("some content")

        conn.execute(
            """INSERT INTO local.memories
               (id, document, embedding, category)
               VALUES (%s, %s, %s::halfvec, 'observation')""",
            ("test::zero-vec", "zero vector doc", str(zero_vec)),
        )

        # Query should not crash even with zero vector in table
        with conn.cursor() as cur:
            cur.execute(
                """SELECT id, embedding <=> %s::halfvec AS distance
                   FROM local.memories WHERE id = %s""",
                (str(emb), "test::zero-vec"),
            )
            row = cur.fetchone()
            assert row is not None
            # Distance may be NaN for zero vector — just verify no crash
        conn.close()


# ── Reserved metadata field names ──────────────────────────────────


class TestMetadataFieldCollisions:
    """Verify column fields in extra_metadata don't leak into JSONB."""

    def test_reserved_fields_not_in_jsonb(self, e2e_config):
        """Fields that are columns should NOT appear in metadata JSONB."""
        from tools.content import content_write

        result = content_write(
            content="field collision test",
            content_type="observation",
            extra_metadata={
                "category": "should-be-stripped",
                "scope": "global",
                "importance_score": 0.99,
                "status": "should-be-stripped",
                "custom_field": "should-remain",
            },
            skip_secret_scan=True,
        )
        assert result["success"] is True

        conn = psycopg.connect(e2e_config["db_url"], autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT metadata, category FROM local.memories WHERE id = %s",
                (result["id"],),
            )
            row = cur.fetchone()
            metadata = row[0]  # JSONB
            category = row[1]  # column

            # Reserved fields stripped from JSONB
            assert "category" not in metadata
            assert "scope" not in metadata
            assert "importance_score" not in metadata
            assert "status" not in metadata

            # Custom field preserved
            assert metadata["custom_field"] == "should-remain"

            # Column value is from content_type, not extra_metadata
            assert category == "observation"
        conn.close()


# ── Retrieval count edge cases ────────────────────────────────────


class TestRetrievalCountEdgeCases:
    """Verify retrieval count increment SQL behavior."""

    def test_increment_only_active_records(self, e2e_config):
        """Deleted/superseded records should NOT have count incremented."""
        from tools.content import content_write, content_delete
        from tools.query import _increment_retrieval_counts

        r = content_write(
            content="will be deleted for count test",
            content_type="observation",
            skip_secret_scan=True,
        )
        content_delete(r["id"], hard=False)

        # Try to increment — should be no-op since status != 'active'
        _increment_retrieval_counts([r["id"]])

        conn = psycopg.connect(e2e_config["db_url"], autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT retrieval_count FROM local.memories WHERE id = %s",
                (r["id"],),
            )
            count = cur.fetchone()[0]
            assert count == 0.0, f"Deleted record count should stay 0, got {count}"
        conn.close()

    def test_increment_with_mixed_ids(self, e2e_config):
        """Mix of core and vault IDs — vault IDs silently ignored."""
        from tools.content import content_write
        from tools.query import _increment_retrieval_counts

        r = content_write(
            content="core record for mixed test",
            content_type="observation",
            skip_secret_scan=True,
        )

        # Mix of core ID + vault ID (vault should be ignored)
        _increment_retrieval_counts([r["id"], "vault::notes/fake.md"])

        conn = psycopg.connect(e2e_config["db_url"], autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT retrieval_count FROM local.memories WHERE id = %s",
                (r["id"],),
            )
            count = cur.fetchone()[0]
            # Should have incremented only the core record
            assert count > 0
        conn.close()


# ── Nested JSONB metadata ─────────────────────────────────────────


class TestNestedJsonbMetadata:
    """Verify JSONB handles nested structures through round-trip."""

    def test_nested_objects_survive_roundtrip(self, e2e_config):
        """Nested dicts in extra_metadata survive PG JSONB round-trip."""
        from tools.content import content_write, content_read

        nested = {
            "relevant_files": ["/src/main.py", "/tests/test_main.py"],
            "context": {"project": "jarvis", "branch": "main"},
            "debug_info": "simple string",
        }

        result = content_write(
            content="nested metadata test",
            content_type="observation",
            extra_metadata=nested,
            skip_secret_scan=True,
        )
        assert result["success"] is True

        read = content_read(result["id"])
        meta = read["metadata"]

        assert meta["relevant_files"] == ["/src/main.py", "/tests/test_main.py"]
        assert meta["context"] == {"project": "jarvis", "branch": "main"}
        assert meta["debug_info"] == "simple string"


# ── Cross-schema search edge cases ────────────────────────────────


class TestCrossSchemaEdgeCases:
    """Verify cross-schema UNION ALL search handles edge cases."""

    def test_search_with_only_vault_results(self, e2e_config):
        """Search returns results even if local.memories is empty for query."""
        from tools.memory import index_file
        from tools.query import query_vault

        vault_dir = e2e_config["vault_dir"]
        notes_dir = vault_dir / "notes"
        test_file = notes_dir / "xyzzy-unique-vault-only.md"
        test_file.write_text("# Xyzzy\n\nThis is extremely unique vault content.")
        index_file(str(test_file))

        result = query_vault("xyzzy unique vault", n_results=10)
        assert result["success"] is True
        assert len(result["results"]) >= 1
        assert any("xyzzy" in r["id"].lower() or "xyzzy" in r.get("title", "").lower()
                    for r in result["results"])

    def test_search_with_only_core_results(self, e2e_config):
        """Search returns results even if obsidian.documents is empty for query."""
        from tools.content import content_write
        from tools.query import query_vault

        content_write(
            content="quantum entanglement is spooky action at a distance",
            content_type="learning",
            importance_score=0.9,
            skip_secret_scan=True,
        )

        result = query_vault("quantum entanglement spooky", n_results=10)
        assert result["success"] is True
        assert len(result["results"]) >= 1


# ── Vault indexing edge cases ─────────────────────────────────────


class TestVaultIndexingEdgeCases:
    """Verify vault document indexing handles edge cases."""

    def test_reindex_same_file_updates_not_duplicates(self, e2e_config):
        """Re-indexing a file should update, not create duplicate rows."""
        from tools.memory import index_file

        vault_dir = e2e_config["vault_dir"]
        notes_dir = vault_dir / "notes"
        test_file = notes_dir / "reindex-test.md"

        # Index once
        test_file.write_text("# Original\n\nOriginal content here.")
        index_file(str(test_file))

        conn = psycopg.connect(e2e_config["db_url"], autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM obsidian.documents WHERE parent_file LIKE %s",
                ("%reindex-test.md",),
            )
            count1 = cur.fetchone()[0]

        # Index again with updated content
        test_file.write_text("# Updated\n\nUpdated content replaces original.")
        index_file(str(test_file))

        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM obsidian.documents WHERE parent_file LIKE %s",
                ("%reindex-test.md",),
            )
            count2 = cur.fetchone()[0]

        assert count2 == count1, f"Re-index should not duplicate: {count2} vs {count1}"

        # Verify content is updated
        with conn.cursor() as cur:
            cur.execute(
                "SELECT document FROM obsidian.documents WHERE parent_file LIKE %s LIMIT 1",
                ("%reindex-test.md",),
            )
            doc = cur.fetchone()[0]
            assert "Updated" in doc

        conn.close()

    def test_index_file_with_frontmatter(self, e2e_config):
        """Frontmatter fields (title, type, tags) properly extracted."""
        from tools.memory import index_file

        vault_dir = e2e_config["vault_dir"]
        notes_dir = vault_dir / "notes"
        test_file = notes_dir / "with-frontmatter.md"
        test_file.write_text(
            "---\ntitle: My Test Note\ntype: reference\ntags: [python, testing]\n---\n\n"
            "# My Test Note\n\nSome body content."
        )
        index_file(str(test_file))

        conn = psycopg.connect(e2e_config["db_url"], autocommit=True)
        with conn.cursor() as cur:
            cur.execute(
                "SELECT title, vault_type, metadata FROM obsidian.documents "
                "WHERE parent_file LIKE %s LIMIT 1",
                ("%with-frontmatter.md",),
            )
            row = cur.fetchone()
            assert row is not None
            title, vault_type, metadata = row
            assert title == "My Test Note"
            assert vault_type == "reference"
        conn.close()
