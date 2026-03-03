"""Content lifecycle e2e tests — real PostgreSQL.

Verifies INSERT, retrieval_count column increment, column-based filters,
ORDER BY importance_score, and soft-delete against real SQL execution.
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
    """INSERT + SELECT roundtrip with column-based fields."""
    from tools.content import content_write, content_read

    result = content_write(
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
    read = content_read(doc_id)
    assert read["success"] is True
    assert read["found"] is True
    assert read["content"] == "User prefers dark mode for all interfaces"
    assert read["category"] == "observation"
    assert read["source"] == "test"
    assert read["scope"] == "global"
    assert read["importance_score"] == 0.7


def test_triple_read_increments_count(e2e_config):
    """retrieval_count column incremented via SQL + 1."""
    from tools.content import content_write, content_read

    result = content_write(
        content="Retrieval count test content",
        content_type="observation",
        importance_score=0.5,
        skip_secret_scan=True,
    )
    doc_id = result["id"]

    # Three reads should increment count from 0 → 3
    for _ in range(3):
        content_read(doc_id)

    final = content_read(doc_id)
    # 3 reads above + 1 final read = 4
    assert final["retrieval_count"] == 4.0


def test_fractional_increment_then_read(e2e_config):
    """Fractional increment on retrieval_count column works correctly."""
    from tools.content import content_write, content_read
    from tools.query import _increment_retrieval_counts

    result = content_write(
        content="Fractional increment test",
        content_type="observation",
        importance_score=0.5,
        skip_secret_scan=True,
    )
    doc_id = result["id"]

    # Fractional increment via batch update
    _increment_retrieval_counts([doc_id], increment=0.01)

    # Now read — this also increments by 1
    read = content_read(doc_id)
    assert read["success"] is True
    # 0.01 (batch) + 1 (read) = 1.01
    assert abs(read["retrieval_count"] - 1.01) < 0.001


def test_list_by_session_id(e2e_config):
    """metadata->>'session_id' = %s WHERE filter."""
    from tools.content import content_write, content_list

    session = "session-filter-test"

    content_write(
        content="Session A observation",
        content_type="observation",
        session_id=session,
        skip_secret_scan=True,
    )
    content_write(
        content="Session B observation",
        content_type="observation",
        session_id="other-session",
        skip_secret_scan=True,
    )

    result = content_list(session_id=session)
    assert result["success"] is True
    assert result["total"] == 1
    assert result["documents"][0]["metadata"]["session_id"] == session


def test_min_importance_filter(e2e_config):
    """importance_score >= %s column filter."""
    from tools.content import content_write, content_list

    content_write(
        content="Low importance item",
        content_type="observation",
        importance_score=0.2,
        skip_secret_scan=True,
    )
    content_write(
        content="High importance item",
        content_type="observation",
        importance_score=0.9,
        skip_secret_scan=True,
    )

    result = content_list(min_importance=0.8)
    assert result["success"] is True
    assert result["total"] == 1
    assert result["documents"][0]["importance_score"] >= 0.8


def test_importance_asc_sort(e2e_config):
    """ORDER BY importance_score ASC."""
    from tools.content import content_write, content_list

    for imp in [0.9, 0.1, 0.5]:
        content_write(
            content=f"Item with importance {imp}",
            content_type="observation",
            importance_score=imp,
            skip_secret_scan=True,
        )

    result = content_list(sort_by="importance_asc")
    assert result["success"] is True
    scores = [d["importance_score"] for d in result["documents"]]
    assert scores == sorted(scores)


def test_created_at_desc_sort(e2e_config):
    """ORDER BY created_at DESC."""
    from datetime import datetime, timezone, timedelta
    from tools.content import content_write, content_list

    base = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ids = []
    for i in range(3):
        ts = (base + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = content_write(
            content=f"Chronological item {i}",
            content_type="observation",
            extra_metadata={"created_at": ts, "updated_at": ts},
            skip_secret_scan=True,
        )
        ids.append(r["id"])

    result = content_list(sort_by="created_at_desc")
    assert result["success"] is True
    result_ids = [d["id"] for d in result["documents"]]
    # Most recent first
    assert result_ids == list(reversed(ids))


def test_soft_delete_and_verify_gone(e2e_config):
    """Soft DELETE (status='deleted') + subsequent read returns not found."""
    from tools.content import content_write, content_read, content_delete

    result = content_write(
        content="Content to be deleted",
        content_type="observation",
        skip_secret_scan=True,
    )
    doc_id = result["id"]

    # Verify it exists
    assert content_read(doc_id)["found"] is True

    # Soft delete
    del_result = content_delete(doc_id)
    assert del_result["success"] is True
    assert del_result["deleted"] is True

    # Verify gone (soft-deleted records not returned)
    gone = content_read(doc_id)
    assert gone["found"] is False


def test_hard_delete(e2e_config):
    """Hard DELETE removes the row entirely."""
    import psycopg
    from tools.content import content_write, content_delete

    result = content_write(
        content="Hard delete test",
        content_type="observation",
        skip_secret_scan=True,
    )
    doc_id = result["id"]

    # Hard delete
    del_result = content_delete(doc_id, hard=True)
    assert del_result["success"] is True

    # Verify row is truly gone (not just soft-deleted)
    db_url = e2e_config["db_url"]
    conn = psycopg.connect(db_url)
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM local.memories WHERE id = %s", (doc_id,))
        assert cur.fetchone() is None
    conn.close()


def test_category_column_stored(e2e_config):
    """Verify category is stored as a proper column, not in JSONB."""
    import psycopg
    from tools.content import content_write

    result = content_write(
        content="Category column test",
        content_type="learning",
        skip_secret_scan=True,
    )
    doc_id = result["id"]

    db_url = e2e_config["db_url"]
    conn = psycopg.connect(db_url)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT category, metadata FROM local.memories WHERE id = %s",
            (doc_id,),
        )
        row = cur.fetchone()
        assert row[0] == "learning"
        # category should NOT be duplicated in metadata JSONB
        import json
        meta = json.loads(row[1]) if isinstance(row[1], str) else row[1]
        assert "category" not in meta
        assert "type" not in meta
        assert "tier" not in meta
    conn.close()
