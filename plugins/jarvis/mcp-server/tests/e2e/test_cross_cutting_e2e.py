"""Cross-cutting e2e tests — metadata fidelity, idempotency, triggers, schema.

Verifies JSONB round-trip, ingest_event_id dedup, metadata merge,
updated_at trigger, halfvec storage, schema idempotence, and jarvis_meta.
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
    """JSONB fidelity for strings, numbers, lists, nested values."""
    from tools.tier2 import tier2_write, tier2_read

    result = tier2_write(
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

    read = tier2_read(result["id"])
    meta = read["metadata"]

    assert meta["type"] == "observation"
    assert meta["tier"] == "chromadb"
    assert meta["source"] == "integration-test"
    assert meta["session_id"] == "meta-session-42"
    assert meta["custom_key"] == "custom_value"
    assert meta["numeric_field"] == "42"
    assert "alpha" in meta["tags"]
    assert "gamma" in meta["tags"]
    assert abs(float(meta["importance_score"]) - 0.73) < 0.01


def test_idempotent_write_with_event_id(e2e_config):
    """SELECT...WHERE metadata->>'ingest_event_id' dedup prevents duplicates."""
    from tools.tier2 import tier2_write

    event_id = "dedup-test-event-001"

    first = tier2_write(
        content="First write with event ID",
        content_type="observation",
        extra_metadata={"ingest_event_id": event_id},
        skip_secret_scan=True,
    )
    assert first["success"] is True
    original_id = first["id"]

    # Second write with same event ID should be deduplicated
    second = tier2_write(
        content="Second write — should be deduped",
        content_type="observation",
        extra_metadata={"ingest_event_id": event_id},
        skip_secret_scan=True,
    )
    assert second["success"] is True
    assert second["deduplicated"] is True
    assert second["id"] == original_id


def test_mark_superseded_jsonb_merge(e2e_config):
    """metadata || jsonb_build_object(...) JSONB merge for superseded status."""
    from tools.tier2 import tier2_write, tier2_read
    from tools.conflict import mark_superseded

    old = tier2_write(
        content="Old observation to be superseded",
        content_type="observation",
        skip_secret_scan=True,
    )
    new = tier2_write(
        content="New observation that supersedes the old one",
        content_type="observation",
        skip_secret_scan=True,
    )

    updated = mark_superseded(old["id"], new["id"])
    assert updated is True

    read = tier2_read(old["id"])
    meta = read["metadata"]
    assert meta["status"] == "superseded"
    assert meta["superseded_by"] == new["id"]
    assert "superseded_at" in meta


def test_trigger_updates_timestamp(e2e_config):
    """trg_jarvis_updated_at PG trigger auto-updates updated_at column."""
    import psycopg
    from tools.tier2 import tier2_write

    result = tier2_write(
        content="Trigger test content",
        content_type="observation",
        skip_secret_scan=True,
    )
    doc_id = result["id"]

    db_url = e2e_config["db_url"]

    # Read the initial updated_at
    conn = psycopg.connect(db_url)
    with conn.cursor() as cur:
        cur.execute("SELECT updated_at FROM jarvis WHERE id = %s", (doc_id,))
        initial_ts = cur.fetchone()[0]
    conn.close()

    time.sleep(0.05)  # Ensure time passes

    # Update the row — trigger should auto-set updated_at
    conn = psycopg.connect(db_url)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE jarvis SET document = 'Modified content' WHERE id = %s",
            (doc_id,),
        )
        conn.commit()
        cur.execute("SELECT updated_at FROM jarvis WHERE id = %s", (doc_id,))
        new_ts = cur.fetchone()[0]
    conn.close()

    assert new_ts > initial_ts


def test_halfvec_roundtrip(e2e_config):
    """halfvec 16-bit storage + cosine distance computation."""
    import psycopg
    from tools.tier2 import tier2_write

    result = tier2_write(
        content="Halfvec roundtrip test",
        content_type="observation",
        skip_secret_scan=True,
    )
    doc_id = result["id"]

    db_url = e2e_config["db_url"]
    conn = psycopg.connect(db_url)
    with conn.cursor() as cur:
        # Verify the embedding was stored and can be used in distance calc
        cur.execute(
            "SELECT embedding <=> embedding AS self_distance FROM jarvis WHERE id = %s",
            (doc_id,),
        )
        row = cur.fetchone()
        self_distance = float(row[0])
        # Cosine distance of a vector with itself should be ~0
        assert self_distance < 0.01
    conn.close()


def test_ensure_schema_idempotent(e2e_config):
    """All CREATE IF NOT EXISTS DDL can be run twice without error."""
    from tools.schema import SCHEMA_SQL, META_SCHEMA_SQL

    import psycopg

    db_url = e2e_config["db_url"]
    conn = psycopg.connect(db_url, autocommit=True)
    try:
        # Run schema DDL a second time — should not raise
        conn.execute(SCHEMA_SQL.format(dimensions=384))
        conn.execute(META_SCHEMA_SQL)
    finally:
        conn.close()


def test_meta_upsert_and_read(e2e_config):
    """jarvis_meta JSONB upsert + read roundtrip."""
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
    """SELECT from jarvis_meta returns None for missing key."""
    from tools.schema import get_meta

    result = get_meta("nonexistent_key_xyz")
    assert result is None


def test_meta_list_all(e2e_config):
    """SELECT all from jarvis_meta returns {key: value} dict."""
    from tools.schema import set_meta, get_all_meta

    set_meta("list_test_a", {"data": "alpha"})
    set_meta("list_test_b", {"data": "beta"})

    all_meta = get_all_meta()
    assert "list_test_a" in all_meta
    assert "list_test_b" in all_meta
    assert all_meta["list_test_a"]["data"] == "alpha"
    assert all_meta["list_test_b"]["data"] == "beta"
