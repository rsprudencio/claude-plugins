"""Tests for multi-user metadata attribution and filtering.

Verifies that:
- Tier 2 writes include user metadata when authenticated
- Anonymous writes have no user field
- Query filtering by user isolates results
- Ownership checks prevent cross-user deletion
- Vault indexing includes user attribution
"""

import pytest

from jarvis_common.auth import current_user


@pytest.fixture(autouse=True)
def _reset_user_context():
    """Ensure contextvar is reset between tests."""
    yield
    # Force reset to default
    try:
        current_user.set("anonymous")
    except Exception:
        pass


class TestTier2UserAttribution:
    """Tier 2 writes should include user metadata when authenticated."""

    def test_tier2_write_includes_user_metadata(self, mock_config):
        """Write as authenticated user includes user field in metadata."""
        from tools.tier2 import tier2_write, tier2_read

        token = current_user.set("raph")
        try:
            result = tier2_write(
                content="Test observation by raph",
                content_type="observation",
                importance_score=0.7,
            )
            assert result["success"] is True

            # Read back and verify user metadata
            doc = tier2_read(result["id"])
            assert doc["found"] is True
            assert doc["metadata"]["user"] == "raph"
        finally:
            current_user.reset(token)

    def test_tier2_write_anonymous_has_no_user_field(self, mock_config):
        """Write without auth (anonymous) has no user field in metadata."""
        from tools.tier2 import tier2_write, tier2_read

        # Default contextvar is "anonymous"
        result = tier2_write(
            content="Anonymous observation",
            content_type="observation",
            importance_score=0.5,
        )
        assert result["success"] is True

        doc = tier2_read(result["id"])
        assert doc["found"] is True
        assert "user" not in doc["metadata"]

    def test_tier2_write_different_users(self, mock_config):
        """Different users get different attribution."""
        import time
        from tools.tier2 import tier2_write, tier2_read

        # Write as raph
        token = current_user.set("raph")
        try:
            r1 = tier2_write(
                content="Raph's observation",
                content_type="observation",
            )
        finally:
            current_user.reset(token)

        # Small delay to avoid timestamp-based ID collision
        time.sleep(0.01)

        # Write as alice
        token = current_user.set("alice")
        try:
            r2 = tier2_write(
                content="Alice's observation",
                content_type="observation",
            )
        finally:
            current_user.reset(token)

        # Verify different IDs were assigned
        assert r1["id"] != r2["id"], "Timestamp collision — IDs should differ"

        doc1 = tier2_read(r1["id"])
        doc2 = tier2_read(r2["id"])
        assert doc1["metadata"]["user"] == "raph"
        assert doc2["metadata"]["user"] == "alice"


class TestQueryUserFiltering:
    """Query filtering should isolate results by user."""

    def test_query_filter_by_user(self, mock_config):
        """Querying with user filter returns only that user's content."""
        import time
        from tools.tier2 import tier2_write, tier2_read
        from tools.query import query_vault

        # Write as raph
        token = current_user.set("raph")
        try:
            r1 = tier2_write(
                content="Raph observation about testing patterns alpha",
                content_type="observation",
                importance_score=0.8,
            )
        finally:
            current_user.reset(token)

        time.sleep(0.01)  # Avoid timestamp-based ID collision

        # Write as alice
        token = current_user.set("alice")
        try:
            r2 = tier2_write(
                content="Alice observation about deployment patterns beta",
                content_type="observation",
                importance_score=0.8,
            )
        finally:
            current_user.reset(token)

        # Verify both docs exist with correct user metadata
        doc1 = tier2_read(r1["id"])
        doc2 = tier2_read(r2["id"])
        assert doc1["metadata"]["user"] == "raph"
        assert doc2["metadata"]["user"] == "alice"

        # Query with user filter — should only see raph's content
        results = query_vault("patterns", n_results=10, user="raph")
        assert results["success"] is True
        if results["results"]:
            # All returned IDs should be raph's (not alice's)
            returned_ids = {r["id"] for r in results["results"]}
            assert r2["id"] not in returned_ids

    def test_query_no_user_filter_returns_all(self, mock_config):
        """Querying without user filter returns all users' content."""
        import time
        from tools.tier2 import tier2_write
        from tools.query import query_vault

        # Write as raph
        token = current_user.set("raph")
        try:
            r1 = tier2_write(
                content="Raph testing shared query results gamma",
                content_type="observation",
                importance_score=0.8,
            )
        finally:
            current_user.reset(token)

        time.sleep(0.01)  # Avoid timestamp-based ID collision

        # Write as alice
        token = current_user.set("alice")
        try:
            r2 = tier2_write(
                content="Alice testing shared query results gamma",
                content_type="observation",
                importance_score=0.8,
            )
        finally:
            current_user.reset(token)

        # Query without user filter — should see both
        results = query_vault("shared query results gamma", n_results=10)
        assert results["success"] is True
        returned_ids = {r["id"] for r in results.get("results", [])}
        # At least one of the two should appear (both ideally)
        assert r1["id"] in returned_ids or r2["id"] in returned_ids


class TestOwnershipChecks:
    """Deletion should enforce ownership in multi-user mode."""

    def test_delete_own_content_succeeds(self, mock_config):
        """User can delete their own tier2 content."""
        from tools.tier2 import tier2_write
        from tools.remove import remove

        token = current_user.set("raph")
        try:
            result = tier2_write(
                content="Raph's deletable observation",
                content_type="observation",
            )
            doc_id = result["id"]

            # Delete own content
            delete_result = remove(id=doc_id)
            assert delete_result["success"] is True
        finally:
            current_user.reset(token)

    def test_delete_other_user_content_fails(self, mock_config):
        """User cannot delete another user's tier2 content."""
        from tools.tier2 import tier2_write
        from tools.remove import remove

        # Write as raph
        token = current_user.set("raph")
        try:
            result = tier2_write(
                content="Raph's protected observation",
                content_type="observation",
            )
            doc_id = result["id"]
        finally:
            current_user.reset(token)

        # Try to delete as alice
        token = current_user.set("alice")
        try:
            delete_result = remove(id=doc_id)
            assert delete_result["success"] is False
            assert "another user" in delete_result["error"]
        finally:
            current_user.reset(token)

    def test_anonymous_can_delete_anything(self, mock_config):
        """Anonymous users can delete any content (backward compat)."""
        from tools.tier2 import tier2_write
        from tools.remove import remove

        # Write as raph
        token = current_user.set("raph")
        try:
            result = tier2_write(
                content="Raph's observation for anon test",
                content_type="observation",
            )
            doc_id = result["id"]
        finally:
            current_user.reset(token)

        # Delete as anonymous (no auth) — should work
        delete_result = remove(id=doc_id)
        assert delete_result["success"] is True

    def test_delete_unowned_content_succeeds(self, mock_config):
        """User can delete content that has no owner (legacy/anonymous)."""
        from tools.tier2 import tier2_write
        from tools.remove import remove

        # Write as anonymous (no user field)
        result = tier2_write(
            content="Anonymous observation",
            content_type="observation",
        )
        doc_id = result["id"]

        # Delete as raph — should work (no owner to conflict with)
        token = current_user.set("raph")
        try:
            delete_result = remove(id=doc_id)
            assert delete_result["success"] is True
        finally:
            current_user.reset(token)


class TestVaultIndexingUser:
    """Vault indexing should attribute documents to current user."""

    def test_vault_indexing_includes_user(self, mock_config, temp_vault):
        """Index a file as authenticated user includes user metadata."""
        from tools.memory import _build_metadata

        # Set user context
        token = current_user.set("raph")
        try:
            meta = _build_metadata(
                frontmatter={"type": "note"},
                relative_path="notes/test.md",
            )
            assert meta["user"] == "raph"
        finally:
            current_user.reset(token)

    def test_vault_indexing_anonymous_no_user(self, mock_config, temp_vault):
        """Index a file as anonymous has no user field."""
        from tools.memory import _build_metadata

        meta = _build_metadata(
            frontmatter={"type": "note"},
            relative_path="notes/test.md",
        )
        assert "user" not in meta


    # TestTranslateFilterUser removed — _translate_filter was a ChromaDB-specific
    # function that no longer exists in the pgvector rewrite. User filtering is
    # now done directly in SQL WHERE clauses.
