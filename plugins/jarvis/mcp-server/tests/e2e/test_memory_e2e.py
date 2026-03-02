"""Memory lifecycle e2e tests — real PostgreSQL.

Verifies file write + INSERT to core.memories with category='memory',
ON CONFLICT DO UPDATE (overwrite), and graceful fallback.
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


def test_write_and_read_memory(e2e_config):
    """File write + INSERT to core.memories + SELECT roundtrip."""
    from tools.memory_crud import memory_write, memory_read

    result = memory_write(
        name="test-principle",
        content="Always write tests before shipping.",
        scope="global",
        tags=["testing", "workflow"],
        importance=0.9,
        overwrite=False,
        skip_secret_scan=True,
    )
    assert result["success"] is True
    assert result["indexed"] is True

    read = memory_read("test-principle", scope="global")
    assert read["success"] is True
    assert read["found"] is True
    assert "Always write tests" in read["content"]
    assert read["source"] == "database"


def test_overwrite_memory(e2e_config):
    """ON CONFLICT DO UPDATE replaces content on overwrite."""
    from tools.memory_crud import memory_write, memory_read

    # Initial write
    memory_write(
        name="evolving-memory",
        content="Version 1 of this memory.",
        scope="global",
        importance=0.5,
        overwrite=False,
        skip_secret_scan=True,
    )

    # Overwrite
    result = memory_write(
        name="evolving-memory",
        content="Version 2 — updated content.",
        scope="global",
        importance=0.8,
        overwrite=True,
        skip_secret_scan=True,
    )
    assert result["success"] is True

    read = memory_read("evolving-memory", scope="global")
    assert read["found"] is True
    assert "Version 2" in read["content"]


def test_read_nonexistent_memory(e2e_config):
    """SELECT returns NULL → graceful 'not found' response."""
    from tools.memory_crud import memory_read

    read = memory_read("does-not-exist", scope="global")
    assert read["success"] is True
    assert read["found"] is False


def test_memory_has_category_column(e2e_config):
    """Verify memory_write stores category='memory' as a column."""
    import psycopg
    from tools.memory_crud import memory_write
    from jarvis_common.namespaces import global_memory_id

    memory_write(
        name="cat-col-test",
        content="Category column verification.",
        scope="global",
        skip_secret_scan=True,
    )

    doc_id = global_memory_id("cat-col-test")
    db_url = e2e_config["db_url"]
    conn = psycopg.connect(db_url)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT category, scope FROM core.memories WHERE id = %s",
            (doc_id,),
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "memory"
        assert row[1] == "global"
    conn.close()


def test_memory_delete(e2e_config):
    """Delete memory removes both file and database record."""
    from tools.memory_crud import memory_write, memory_read, memory_delete

    memory_write(
        name="delete-me",
        content="Temporary memory.",
        scope="global",
        skip_secret_scan=True,
    )

    # Verify exists
    assert memory_read("delete-me")["found"] is True

    # Delete
    result = memory_delete(name="delete-me", confirm=True)
    assert result["success"] is True

    # Verify gone
    assert memory_read("delete-me")["found"] is False
