"""E2E test fixtures — real PostgreSQL + pgvector.

Three layered fixtures:
  e2e_database_url  (session) — CREATE/DROP disposable jarvis_e2e_test database
  e2e_schema        (session) — run dual-schema DDL (local + obsidian)
  e2e_config        (function, autouse) — env vars, mock embedding, pool reset,
                                          TRUNCATE on teardown
"""

import os
from urllib.parse import urlparse, urlunparse

import psycopg
import pytest

from tests.conftest import MockEmbeddingService


E2E_POSTGRES_URL = os.environ.get("E2E_POSTGRES_URL", "")

pytestmark = pytest.mark.skipif(
    not E2E_POSTGRES_URL,
    reason="E2E_POSTGRES_URL not set — skipping e2e tests",
)


# ── Session fixtures ──────────────────────────────────────────────────


@pytest.fixture(scope="session")
def e2e_database_url():
    """Create a disposable jarvis_e2e_test database for the test session."""
    if not E2E_POSTGRES_URL:
        pytest.skip("E2E_POSTGRES_URL not set")

    # Connect to the admin database to CREATE/DROP the test database.
    # autocommit is required because CREATE DATABASE can't run in a transaction.
    admin_conn = psycopg.connect(E2E_POSTGRES_URL, autocommit=True)
    try:
        admin_conn.execute("DROP DATABASE IF EXISTS jarvis_e2e_test")
        admin_conn.execute("CREATE DATABASE jarvis_e2e_test")
    finally:
        admin_conn.close()

    # Build the test database URL by replacing the database name.
    parsed = urlparse(E2E_POSTGRES_URL)
    test_url = urlunparse(parsed._replace(path="/jarvis_e2e_test"))

    yield test_url

    # Teardown: drop the test database
    admin_conn = psycopg.connect(E2E_POSTGRES_URL, autocommit=True)
    try:
        admin_conn.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = 'jarvis_e2e_test' AND pid <> pg_backend_pid()"
        )
        admin_conn.execute("DROP DATABASE IF EXISTS jarvis_e2e_test")
    finally:
        admin_conn.close()


@pytest.fixture(scope="session")
def e2e_schema(e2e_database_url):
    """Initialize dual-schema DDL (local + obsidian) in the test database."""
    from tools.schema import (
        LOCAL_SCHEMA_SQL, OBSIDIAN_SCHEMA_SQL, LOCAL_META_SQL,
        SYNC_SCHEMA_SQL, CONSOLIDATION_SCHEMA_SQL,
        RETRIEVAL_TELEMETRY_SCHEMA_SQL, LEXICAL_SCHEMA_SQL,
        DOCUMENT_CONTEXT_SCHEMA_SQL,
    )

    conn = psycopg.connect(e2e_database_url, autocommit=True)
    try:
        conn.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.execute(LOCAL_SCHEMA_SQL.format(dimensions=384))
        conn.execute(OBSIDIAN_SCHEMA_SQL.format(dimensions=384))
        # Per-file LLM summary cache (contextual summaries).
        conn.execute(DOCUMENT_CONTEXT_SCHEMA_SQL)
        conn.execute(LOCAL_META_SQL)
        conn.execute(SYNC_SCHEMA_SQL)
        conn.execute(CONSOLIDATION_SCHEMA_SQL)
        conn.execute(RETRIEVAL_TELEMETRY_SCHEMA_SQL)
        # Phase 1 hybrid retrieval — lexical tsvector columns + channel column.
        conn.execute(LEXICAL_SCHEMA_SQL)
    finally:
        conn.close()

    return e2e_database_url


# ── Per-test fixture ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def e2e_config(e2e_schema, tmp_path, monkeypatch):
    """Configure the production code to use the e2e test database.

    - Sets POSTGRES_URL env var → get_postgres_config() picks it up
    - Patches embedding service to MockEmbeddingService (384d)
    - Resets the connection pool singleton
    - Creates a temp vault + config directory
    - TRUNCATEs tables on teardown
    """
    import json

    import tools.config as config_module
    import tools.schema as schema_module
    import tools.embedding as embedding_module
    import jarvis_common.config as common_config_module

    db_url = e2e_schema

    # ── Environment ───────────────────────────────────────────────
    monkeypatch.setenv("POSTGRES_URL", db_url)
    monkeypatch.setenv("JARVIS_SKIP_MODEL_CHECK", "1")
    monkeypatch.setenv("EMBEDDING_DIMENSIONS", "384")

    # ── Temp vault + config ───────────────────────────────────────
    vault_dir = tmp_path / "vault"
    vault_dir.mkdir()
    (vault_dir / "journal" / "2026" / "01").mkdir(parents=True)
    (vault_dir / "notes").mkdir()

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    memories_dir = config_dir / "memories"
    memories_dir.mkdir()
    strategic_dir = config_dir / "strategic"
    strategic_dir.mkdir()

    config_file = config_dir / "config.json"
    config_data = {
        "vault_path": str(vault_dir),
        "vault_confirmed": True,
        "configured_at": "2026-01-01T00:00:00Z",
        "memory": {
            "postgres_url": db_url,
            "project_memories_path": str(memories_dir),
            "secret_detection": False,
        },
    }
    config_file.write_text(json.dumps(config_data))

    # ── Clear all caches ──────────────────────────────────────────
    config_module._config_cache = None
    common_config_module._config_cache = None
    schema_module._pool = None
    schema_module._pool_cache_key = None
    embedding_module._service = None
    embedding_module._service_cache_key = None

    # ── Patch config to use our temp dirs ─────────────────────────
    def mock_get_config():
        if common_config_module._config_cache is None:
            common_config_module._config_cache = json.loads(
                config_file.read_text()
            )
        return common_config_module._config_cache

    monkeypatch.setattr(common_config_module, "get_config", mock_get_config)
    monkeypatch.setattr(config_module, "get_config", mock_get_config)

    # Redirect JARVIS_HOME so path resolution finds temp dirs
    monkeypatch.setenv("JARVIS_HOME", str(config_dir))

    # Patch get_vault_path in common config module
    monkeypatch.setattr(
        common_config_module, "get_vault_path", lambda: str(vault_dir)
    )
    if hasattr(config_module, "get_vault_path"):
        monkeypatch.setattr(
            config_module, "get_vault_path", lambda: str(vault_dir)
        )

    # ── Patch embedding to use deterministic mock ─────────────────
    mock_emb = MockEmbeddingService(dimensions=384)
    monkeypatch.setattr(
        embedding_module, "get_embedding_service", lambda: mock_emb
    )
    monkeypatch.setattr(embedding_module, "_service", mock_emb)
    monkeypatch.setattr(
        embedding_module,
        "_service_cache_key",
        ("mock", 384, "cpu", "mock"),
    )

    # ── Hermeticity: no real LLM calls ────────────────────────────
    # Contextual document summaries run inside the indexing path and `claude`
    # is on PATH on developer machines; a None response is exactly what an
    # unreachable backend produces. Tests that want generation override this.
    import tools.conflict as conflict_module
    import tools.context_summary as context_summary_module

    monkeypatch.setattr(
        conflict_module, "_call_haiku_raw", lambda *a, **k: None
    )
    context_summary_module.reset_unavailable_warning()

    # ── Reset pool so it connects to the e2e database ─────────────
    schema_module.reset_pool()

    yield {
        "db_url": db_url,
        "vault_dir": vault_dir,
        "config_dir": config_dir,
        "embedding_service": mock_emb,
    }

    # ── Teardown: TRUNCATE tables, reset pool ─────────────────────
    try:
        conn = psycopg.connect(db_url, autocommit=True)
        conn.execute(
            "TRUNCATE local.memory_chunks, local.memories, obsidian.documents, "
            "obsidian.document_context, local.meta, local.sync_queue, "
            "local.retrieval_events CASCADE"
        )
        conn.close()
    except Exception:
        pass

    schema_module.reset_pool()
    config_module._config_cache = None
    common_config_module._config_cache = None
    embedding_module._service = None
    embedding_module._service_cache_key = None
