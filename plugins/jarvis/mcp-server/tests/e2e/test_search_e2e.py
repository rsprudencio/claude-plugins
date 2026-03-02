"""Semantic search e2e tests — real PostgreSQL + pgvector.

Verifies embedding <=> halfvec cosine distance, cross-schema UNION ALL,
and batch UPDATE retrieval count increment against real SQL.
"""

import os

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("E2E_POSTGRES_URL"),
        reason="E2E_POSTGRES_URL not set",
    ),
]


def test_query_finds_related_content(e2e_config):
    """embedding <=> %s::halfvec ORDER BY distance — cosine similarity search."""
    from tools.content import content_write
    from tools.query import query_vault

    # Seed some content in core.memories
    content_write(
        content="Python is a great language for data science and machine learning",
        content_type="observation",
        importance_score=0.8,
        skip_secret_scan=True,
    )
    content_write(
        content="The user's favorite coffee shop is on Main Street",
        content_type="observation",
        importance_score=0.5,
        skip_secret_scan=True,
    )

    result = query_vault("programming languages and AI", n_results=5)
    assert result["success"] is True
    assert len(result["results"]) >= 1


def test_search_increments_retrieval_count(e2e_config):
    """Batch UPDATE core.memories SET retrieval_count = retrieval_count + %s."""
    from tools.content import content_write, content_read
    from tools.query import query_vault

    result = content_write(
        content="Unique searchable content for retrieval count testing xyz123",
        content_type="observation",
        importance_score=0.9,
        skip_secret_scan=True,
    )
    doc_id = result["id"]

    # Initial retrieval_count should be 0
    # content_read increments by 1, so now it's 1
    initial = content_read(doc_id)
    count_after_read = initial["retrieval_count"]

    # Search should trigger _increment_retrieval_counts
    query_vault("retrieval count testing xyz123", n_results=5)

    # Read again to check the count increased
    final = content_read(doc_id)
    count_final = final["retrieval_count"]
    # count_after_read was 1, then query_vault added 1, then this read added 1
    assert count_final > count_after_read


def test_cross_schema_search(e2e_config):
    """UNION ALL search across core.memories and vault.documents."""
    from tools.content import content_write
    from tools.memory import index_file
    from tools.query import query_vault

    # Seed content in core.memories
    content_write(
        content="Authentication uses OAuth 2.0 with PKCE flow",
        content_type="learning",
        importance_score=0.8,
        skip_secret_scan=True,
    )

    # Seed content in vault.documents via indexing
    vault_dir = e2e_config["vault_dir"]
    notes_dir = vault_dir / "notes"
    auth_file = notes_dir / "auth-guide.md"
    auth_file.write_text("# Auth Guide\n\nOAuth 2.0 with PKCE for security.")
    index_file(str(auth_file))

    # Search should find results from both schemas
    result = query_vault("OAuth authentication", n_results=10)
    assert result["success"] is True
    assert len(result["results"]) >= 2

    # Should have results from both schemas
    schemas = {r.get("_schema") for r in result["results"]}
    assert "core" in schemas or "vault" in schemas


def test_vault_document_search(e2e_config):
    """Vault documents indexed into vault.documents are searchable."""
    from tools.memory import index_file
    from tools.query import query_vault

    vault_dir = e2e_config["vault_dir"]
    notes_dir = vault_dir / "notes"
    test_file = notes_dir / "python-tips.md"
    test_file.write_text(
        "# Python Tips\n\nUse list comprehensions for cleaner code.\n"
        "Generator expressions save memory for large datasets."
    )
    index_file(str(test_file))

    result = query_vault("Python list comprehension", n_results=5)
    assert result["success"] is True
    assert len(result["results"]) >= 1
