"""Tests for search SQL generation across sources with differing schemas.

`local.memories` has a `status` column (soft-delete); `obsidian.documents`
does not. Search must only emit the `status != 'deleted'` predicate against
tables that actually have the column, otherwise Postgres raises
`column "status" does not exist`.

Uses FastAPI TestClient with a mocked _local_pool, capturing the SQL passed
to conn.execute() and rendering it back to a string for assertions.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure app is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient
from psycopg import sql


# ── Fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Isolate config to temp directory."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({
        "memory": {"postgres": {"url": "postgresql://test:test@localhost/test"}},
    }))
    from jarvis_common.config import clear_config_cache
    clear_config_cache()
    yield
    clear_config_cache()


_LOCAL_SOURCE = {
    "id": "local",
    "label": "Local Memories",
    "type": "local",
    "schema": "local",
    "table": "memories",
    "has_retrieval_count": True,
    "has_status": True,
    "capabilities": ["text", "metadata", "semantic"],
    "metadata_filters": ["category", "scope", "status"],
}

# obsidian.documents has neither a status nor a retrieval_count column
_OBSIDIAN_SOURCE = {
    "id": "obsidian",
    "label": "Obsidian Vault",
    "type": "local",
    "schema": "obsidian",
    "table": "documents",
    "has_retrieval_count": False,
    "has_status": False,
    "capabilities": ["text", "metadata", "semantic"],
    "metadata_filters": ["vault_type", "directory"],
}


@pytest.fixture
def client():
    """TestClient with a mocked pool that records every SQL statement."""
    statements: list[str] = []

    pool = MagicMock()
    conn = MagicMock()
    cur = MagicMock()
    pool.connection.return_value.__enter__ = MagicMock(return_value=conn)
    pool.connection.return_value.__exit__ = MagicMock(return_value=False)

    def _execute(query, params=None):
        if isinstance(query, (sql.SQL, sql.Composed)):
            statements.append(query.as_string(None))
        else:
            statements.append(str(query))
        return cur

    conn.execute = MagicMock(side_effect=_execute)
    cur.fetchone.return_value = (0,)   # COUNT(*) result
    cur.fetchall.return_value = []     # no rows

    sources = {"local": _LOCAL_SOURCE, "obsidian": _OBSIDIAN_SOURCE}
    with (
        patch("app._local_pool", pool),
        patch("app._sources", sources),
        # avoid loading the real embedding model
        patch("app._vec_str", return_value="[0.1,0.2]"),
    ):
        import app as app_module
        yield TestClient(app_module.app), statements


def _search(tc, **kwargs):
    body = {"source": "obsidian", "mode": "text", "query": "", **kwargs}
    return tc.post("/api/search", json=body)


def _sql_text(statements: list[str]) -> str:
    """All captured SQL, minus the session-level SET statements."""
    return "\n".join(s for s in statements if not s.startswith("SET"))


# ── Obsidian: must never reference the non-existent status column ─────────

class TestObsidianHasNoStatusColumn:
    @pytest.mark.parametrize("mode,query", [
        ("text", ""),
        ("text", "hello"),
        ("semantic", "hello"),
        ("metadata", ""),
    ])
    def test_no_status_predicate(self, client, mode, query):
        tc, statements = client
        r = _search(tc, mode=mode, query=query)
        assert r.status_code == 200, r.text
        assert "status" not in _sql_text(statements).lower()

    def test_no_retrieval_count_column(self, client):
        """obsidian.documents also lacks retrieval_count."""
        tc, statements = client
        assert _search(tc, mode="text", query="x").status_code == 200
        assert "retrieval_count" not in _sql_text(statements)

    def test_retrieval_sort_falls_back(self, client):
        """Sorting by retrieval_count must not leak into obsidian SQL."""
        tc, statements = client
        r = _search(tc, mode="text", query="x", sort_by="retrieval_desc")
        assert r.status_code == 200
        assert "retrieval_count" not in _sql_text(statements)

    def test_status_filter_is_ignored(self, client):
        """A status filter is not in obsidian's allowed filters — must be dropped."""
        tc, statements = client
        r = _search(tc, mode="metadata", filters={"status": "active"})
        assert r.status_code == 200
        assert "status" not in _sql_text(statements).lower()

    def test_queries_target_obsidian_documents(self, client):
        """Sanity check: SQL really is being built for obsidian.documents."""
        tc, statements = client
        assert _search(tc, mode="text", query="x").status_code == 200
        assert '"obsidian"."documents"' in _sql_text(statements)


# ── Local: soft-deleted rows stay excluded ───────────────────────────────

class TestLocalStatusFiltering:
    @pytest.mark.parametrize("mode,query", [
        ("text", ""),
        ("text", "hello"),
        ("semantic", "hello"),
        ("metadata", ""),
    ])
    def test_excludes_deleted_by_default(self, client, mode, query):
        tc, statements = client
        r = _search(tc, source="local", mode=mode, query=query)
        assert r.status_code == 200, r.text
        assert "status != 'deleted'" in _sql_text(statements)

    def test_explicit_status_filter_overrides_default(self, client):
        """Filtering by status lets the user see deleted rows."""
        tc, statements = client
        r = _search(tc, source="local", mode="metadata",
                    filters={"status": "deleted"})
        assert r.status_code == 200
        text = _sql_text(statements)
        assert "status != 'deleted'" not in text
        assert '"status" = ' in text
