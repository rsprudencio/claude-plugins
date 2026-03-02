"""Tier 2 lifecycle e2e tests — real PostgreSQL.

Verifies INSERT ON CONFLICT, retrieval_count casts, JSONB filters,
ORDER BY with ::float, and DELETE against real SQL execution.
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


def test_store_observation_retrieve_by_id(e2e_config):
    """INSERT ON CONFLICT + SELECT + JSONB read roundtrip."""
    from tools.tier2 import tier2_write, tier2_read

    result = tier2_write(
        content="User prefers dark mode for all interfaces",
        content_type="observation",
        importance_score=0.7,
        source="test",
        tags=["preference", "ui"],
        session_id="test-session-001",
        skip_secret_scan=True,
    )
    assert result["success"] is True
    doc_id = result["id"]

    # Read back
    read = tier2_read(doc_id)
    assert read["success"] is True
    assert read["found"] is True
    assert read["content"] == "User prefers dark mode for all interfaces"
    meta = read["metadata"]
    assert meta["type"] == "observation"
    assert meta["tier"] == "chromadb"
    assert meta["source"] == "test"
    assert "preference" in meta["tags"]


def test_triple_read_increments_count(e2e_config):
    """retrieval_count incremented via (metadata->>'retrieval_count')::float + 1."""
    from tools.tier2 import tier2_write, tier2_read

    result = tier2_write(
        content="Retrieval count test content",
        content_type="observation",
        importance_score=0.5,
        skip_secret_scan=True,
    )
    doc_id = result["id"]

    # Three reads should increment count from 0 → 3
    for _ in range(3):
        tier2_read(doc_id)

    final = tier2_read(doc_id)
    count = float(final["metadata"]["retrieval_count"])
    # 3 reads above + 1 final read = 4
    assert count == 4.0


def test_fractional_increment_then_read(e2e_config):
    """The exact bug: ::float cast on '0.01' — would fail with ::int."""
    from tools.tier2 import tier2_write, tier2_read
    from tools.query import _increment_retrieval_counts

    result = tier2_write(
        content="Fractional increment test",
        content_type="observation",
        importance_score=0.5,
        skip_secret_scan=True,
    )
    doc_id = result["id"]

    # Fractional increment via batch update (the code path that had ::float)
    _increment_retrieval_counts([doc_id], increment=0.01)

    # Now read — this also increments by 1, exercising the single-row path
    read = tier2_read(doc_id)
    assert read["success"] is True
    count = float(read["metadata"]["retrieval_count"])
    # 0.01 (batch) + 1 (read) = 1.01
    assert abs(count - 1.01) < 0.001


def test_list_by_session_id(e2e_config):
    """metadata->>'session_id' = %s WHERE filter."""
    from tools.tier2 import tier2_write, tier2_list

    session = "session-filter-test"

    tier2_write(
        content="Session A observation",
        content_type="observation",
        session_id=session,
        skip_secret_scan=True,
    )
    tier2_write(
        content="Session B observation",
        content_type="observation",
        session_id="other-session",
        skip_secret_scan=True,
    )

    result = tier2_list(session_id=session)
    assert result["success"] is True
    assert result["total"] == 1
    assert result["documents"][0]["metadata"]["session_id"] == session


def test_min_importance_filter(e2e_config):
    """(metadata->>'importance_score')::float >= %s filter."""
    from tools.tier2 import tier2_write, tier2_list

    tier2_write(
        content="Low importance item",
        content_type="observation",
        importance_score=0.2,
        skip_secret_scan=True,
    )
    tier2_write(
        content="High importance item",
        content_type="observation",
        importance_score=0.9,
        skip_secret_scan=True,
    )

    result = tier2_list(min_importance=0.8)
    assert result["success"] is True
    assert result["total"] == 1
    score = float(result["documents"][0]["metadata"]["importance_score"])
    assert score >= 0.8


def test_importance_asc_sort(e2e_config):
    """ORDER BY (metadata->>'importance_score')::float ASC."""
    from tools.tier2 import tier2_write, tier2_list

    for imp in [0.9, 0.1, 0.5]:
        tier2_write(
            content=f"Item with importance {imp}",
            content_type="observation",
            importance_score=imp,
            skip_secret_scan=True,
        )

    result = tier2_list(sort_by="importance_asc")
    assert result["success"] is True
    scores = [
        float(d["metadata"]["importance_score"])
        for d in result["documents"]
    ]
    assert scores == sorted(scores)


def test_created_at_desc_sort(e2e_config):
    """ORDER BY created_at DESC."""
    import time
    from datetime import datetime, timezone, timedelta
    from tools.tier2 import tier2_write, tier2_list

    # Insert with explicit, well-separated created_at timestamps
    # to avoid relying on Python's datetime resolution or PG's now()
    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ids = []
    for i in range(3):
        ts = (base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = tier2_write(
            content=f"Chronological item {i}",
            content_type="observation",
            extra_metadata={"created_at": ts, "updated_at": ts},
            skip_secret_scan=True,
        )
        ids.append(r["id"])

    result = tier2_list(sort_by="created_at_desc")
    assert result["success"] is True
    result_ids = [d["id"] for d in result["documents"]]
    # Most recent first
    assert result_ids == list(reversed(ids))


def test_store_remove_verify_gone(e2e_config):
    """DELETE + subsequent SELECT returns empty."""
    from tools.tier2 import tier2_write, tier2_read, tier2_delete

    result = tier2_write(
        content="Content to be deleted",
        content_type="observation",
        skip_secret_scan=True,
    )
    doc_id = result["id"]

    # Verify it exists
    assert tier2_read(doc_id)["found"] is True

    # Delete
    del_result = tier2_delete(doc_id)
    assert del_result["success"] is True
    assert del_result["deleted"] is True

    # Verify gone
    gone = tier2_read(doc_id)
    assert gone["found"] is False
