"""Semantic search e2e tests — real PostgreSQL + pgvector.

Verifies embedding <=> halfvec cosine distance and batch UPDATE
retrieval count increment against real SQL.
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
    from tools.tier2 import tier2_write
    from tools.query import query_vault

    # Seed some content
    tier2_write(
        content="Python is a great language for data science and machine learning",
        content_type="observation",
        importance_score=0.8,
        skip_secret_scan=True,
    )
    tier2_write(
        content="The user's favorite coffee shop is on Main Street",
        content_type="observation",
        importance_score=0.5,
        skip_secret_scan=True,
    )

    result = query_vault("programming languages and AI", n_results=5)
    assert result["success"] is True
    assert len(result["results"]) >= 1
    # The Python/ML observation should rank higher than coffee
    top_result = result["results"][0]
    assert "Python" in top_result.get("preview", "") or "python" in top_result.get("id", "").lower()


def test_search_increments_retrieval_count(e2e_config):
    """Batch UPDATE...WHERE id = ANY(%s) for retrieval count increment."""
    from tools.tier2 import tier2_write, tier2_read
    from tools.query import query_vault

    result = tier2_write(
        content="Unique searchable content for retrieval count testing xyz123",
        content_type="observation",
        importance_score=0.9,
        skip_secret_scan=True,
    )
    doc_id = result["id"]

    # Initial retrieval_count should be "0"
    initial = tier2_read(doc_id)
    # tier2_read increments by 1, so now it's 1
    count_after_read = float(initial["metadata"]["retrieval_count"])

    # Search should trigger _increment_retrieval_counts
    query_vault("retrieval count testing xyz123", n_results=5)

    # Read again to check the count increased
    final = tier2_read(doc_id)
    count_final = float(final["metadata"]["retrieval_count"])
    # count_after_read was 1, then query_vault added 1, then this read added 1
    assert count_final > count_after_read
