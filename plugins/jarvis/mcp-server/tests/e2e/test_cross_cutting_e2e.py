"""Cross-cutting e2e tests — metadata fidelity, idempotency, triggers, schema.

Verifies JSONB round-trip, ingest_event_id dedup, supersession via columns,
updated_at trigger, halfvec storage, schema idempotence, and local.meta.
"""

import os
import time

import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("E2E_POSTGRES_URL"),
        reason="E2E_POSTGRES_URL not set",
    ),
]


def test_all_metadata_fields_preserved(e2e_config):
    """JSONB fidelity for remaining flexible metadata fields."""
    from tools.content import content_write, content_read

    result = content_write(
        content="Metadata fidelity test",
        content_type="observation",
        importance_score=0.73,
        source="integration-test",
        tags=["alpha", "beta", "gamma"],
        session_id="meta-session-42",
        extra_metadata={
            "custom_key": "custom_value",
            "numeric_field": "42",
        },
        skip_secret_scan=True,
    )
    assert result["success"] is True

    read = content_read(result["id"])

    # Column-level fields
    assert read["category"] == "observation"
    assert read["source"] == "integration-test"
    assert abs(read["importance_score"] - 0.73) < 0.01

    # Remaining JSONB metadata
    meta = read["metadata"]
    assert meta["session_id"] == "meta-session-42"
    assert meta["custom_key"] == "custom_value"
    assert meta["numeric_field"] == "42"
    assert "alpha" in meta["tags"]
    assert "gamma" in meta["tags"]

    # Promoted fields should NOT be in metadata JSONB
    assert "type" not in meta
    assert "tier" not in meta
    assert "category" not in meta
    assert "source" not in meta
    assert "importance_score" not in meta


def test_idempotent_write_with_event_id(e2e_config):
    """metadata->>'ingest_event_id' dedup prevents duplicates."""
    from tools.content import content_write

    event_id = "dedup-test-event-001"

    first = content_write(
        content="First write with event ID",
        content_type="observation",
        extra_metadata={"ingest_event_id": event_id},
        skip_secret_scan=True,
    )
    assert first["success"] is True
    original_id = first["id"]

    # Second write with same event ID should be deduplicated
    second = content_write(
        content="Second write — should be deduped",
        content_type="observation",
        extra_metadata={"ingest_event_id": event_id},
        skip_secret_scan=True,
    )
    assert second["success"] is True
    assert second["deduplicated"] is True
    assert second["id"] == original_id


def test_mark_superseded_column_update(e2e_config):
    """Column-level supersession: status='superseded', superseded_by=new_id."""
    import psycopg
    from tools.content import content_write, content_read
    from tools.conflict import mark_superseded

    old = content_write(
        content="Old observation to be superseded",
        content_type="observation",
        skip_secret_scan=True,
    )
    new = content_write(
        content="New observation that supersedes the old one",
        content_type="observation",
        skip_secret_scan=True,
    )

    updated = mark_superseded(old["id"], new["id"])
    assert updated is True

    # The superseded record should not be found via content_read (filters active)
    read = content_read(old["id"])
    assert read["found"] is False

    # But it should still exist in the table with status='superseded'
    db_url = e2e_config["db_url"]
    conn = psycopg.connect(db_url)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT status, superseded_by FROM local.memories WHERE id = %s",
            (old["id"],),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "superseded"
        assert row[1] == new["id"]
    conn.close()


def test_trigger_updates_timestamp(e2e_config):
    """trg_local_memories_updated_at PG trigger auto-updates updated_at column."""
    import psycopg
    from tools.content import content_write

    result = content_write(
        content="Trigger test content",
        content_type="observation",
        skip_secret_scan=True,
    )
    doc_id = result["id"]

    db_url = e2e_config["db_url"]

    # Read the initial updated_at
    conn = psycopg.connect(db_url)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT updated_at FROM local.memories WHERE id = %s", (doc_id,)
        )
        initial_ts = cur.fetchone()[0]
    conn.close()

    time.sleep(0.05)

    # Update the row — trigger should auto-set updated_at
    conn = psycopg.connect(db_url)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE local.memories SET document = 'Modified content' WHERE id = %s",
            (doc_id,),
        )
        conn.commit()
        cur.execute(
            "SELECT updated_at FROM local.memories WHERE id = %s", (doc_id,)
        )
        new_ts = cur.fetchone()[0]
    conn.close()

    assert new_ts > initial_ts


def test_halfvec_roundtrip(e2e_config):
    """halfvec 16-bit storage + cosine distance computation."""
    import psycopg
    from tools.content import content_write

    result = content_write(
        content="Halfvec roundtrip test",
        content_type="observation",
        skip_secret_scan=True,
    )
    doc_id = result["id"]

    db_url = e2e_config["db_url"]
    conn = psycopg.connect(db_url)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT embedding <=> embedding AS self_distance "
            "FROM local.memories WHERE id = %s",
            (doc_id,),
        )
        row = cur.fetchone()
        self_distance = float(row[0])
        # Cosine distance of a vector with itself should be ~0
        assert self_distance < 0.01
    conn.close()


def test_vault_halfvec_roundtrip(e2e_config):
    """halfvec in obsidian.documents — same cosine distance validation."""
    import psycopg
    from tools.memory import index_file

    vault_dir = e2e_config["vault_dir"]
    test_file = vault_dir / "notes" / "halfvec-test.md"
    test_file.write_text("# Halfvec Test\n\nVector storage validation.")
    index_file(str(test_file))

    db_url = e2e_config["db_url"]
    conn = psycopg.connect(db_url)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT embedding <=> embedding AS self_distance "
            "FROM obsidian.documents LIMIT 1"
        )
        row = cur.fetchone()
        if row:
            self_distance = float(row[0])
            assert self_distance < 0.01
    conn.close()


def test_ensure_schema_idempotent(e2e_config):
    """All CREATE IF NOT EXISTS DDL can be run twice without error."""
    from tools.schema import CORE_SCHEMA_SQL, VAULT_SCHEMA_SQL, CORE_META_SQL

    import psycopg

    db_url = e2e_config["db_url"]
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        conn.execute(CORE_SCHEMA_SQL.format(dimensions=384))
        conn.execute(VAULT_SCHEMA_SQL.format(dimensions=384))
        conn.execute(CORE_META_SQL)
    finally:
        conn.close()


def test_meta_upsert_and_read(e2e_config):
    """local.meta JSONB upsert + read roundtrip."""
    from tools.schema import set_meta, get_meta

    set_meta("test_config", {"version": 1, "enabled": True})

    result = get_meta("test_config")
    assert result is not None
    assert result["version"] == 1
    assert result["enabled"] is True

    # Upsert (update)
    set_meta("test_config", {"version": 2, "enabled": False, "new_key": "x"})
    updated = get_meta("test_config")
    assert updated["version"] == 2
    assert updated["enabled"] is False
    assert updated["new_key"] == "x"


def test_meta_read_nonexistent(e2e_config):
    """SELECT from local.meta returns None for missing key."""
    from tools.schema import get_meta

    result = get_meta("nonexistent_key_xyz")
    assert result is None


def test_meta_list_all(e2e_config):
    """SELECT all from local.meta returns {key: value} dict."""
    from tools.schema import set_meta, get_all_meta

    set_meta("list_test_a", {"data": "alpha"})
    set_meta("list_test_b", {"data": "beta"})

    all_meta = get_all_meta()
    assert "list_test_a" in all_meta
    assert "list_test_b" in all_meta
    assert all_meta["list_test_a"]["data"] == "alpha"
    assert all_meta["list_test_b"]["data"] == "beta"


def test_active_memories_view_excludes_deleted(e2e_config):
    """local.active_memories view filters out deleted and superseded records."""
    import psycopg
    from tools.content import content_write, content_delete

    active = content_write(
        content="I should be visible",
        content_type="observation",
        skip_secret_scan=True,
    )
    deleted = content_write(
        content="I should be hidden",
        content_type="observation",
        skip_secret_scan=True,
    )

    # Soft delete
    content_delete(deleted["id"])

    db_url = e2e_config["db_url"]
    conn = psycopg.connect(db_url)
    with conn.cursor() as cur:
        # Active view should only show the active record
        cur.execute(
            "SELECT id FROM local.active_memories WHERE id IN (%s, %s)",
            (active["id"], deleted["id"]),
        )
        found_ids = [row[0] for row in cur.fetchall()]
        assert active["id"] in found_ids
        assert deleted["id"] not in found_ids
    conn.close()
