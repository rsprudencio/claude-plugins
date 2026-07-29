"""E2E: LLM-generated contextual summaries against real PostgreSQL.

The unit suite proves the formatting and cache logic; only real SQL can prove
the DDL is idempotent, that the ``(parent_file, content_hash)`` cache genuinely
survives a second indexing pass, that a STALE row is really DELETEd, that the
achieved augmentation state lands in ``local.meta`` JSONB, that the shadow
drift guard's query works, that the augmentation era can be read back out of
``config_snapshot``, and — the acceptance shape — that a relational fact living
ONLY in a generated summary makes its document retrievable by a query phrased
with that relation.

Generation is OUT OF BAND (bin/generate_summaries.py). These tests therefore
follow the real operator sequence — generate, THEN reindex — and assert that the
indexing path itself never generates. The summary generator is mocked (a fixed
sentence): this exercises the storage/augmentation/retrieval pipeline, not Haiku.
A deterministic bag-of-words embedding service maps token overlap onto real
pgvector cosine, the same technique as test_contextual_embeddings_e2e.py.
"""

import math
import os
import re

import psycopg
import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("E2E_POSTGRES_URL"),
        reason="E2E_POSTGRES_URL not set",
    ),
]


class _VocabEmbeddingService:
    """Deterministic bag-of-words embeddings: cosine ≈ token overlap."""

    def __init__(self, dimensions: int = 384):
        self._dimensions = dimensions
        self._vocab: dict[str, int] = {}

    def _dim(self, tok: str) -> int:
        if tok not in self._vocab:
            self._vocab[tok] = min(len(self._vocab), self._dimensions - 1)
        return self._vocab[tok]

    def _vec(self, text: str):
        vec = [0.0] * self._dimensions
        for tok in re.findall(r"[a-z0-9]+", (text or "").lower()):
            vec[self._dim(tok)] += 1.0
        norm = math.sqrt(sum(x * x for x in vec))
        return [x / norm for x in vec] if norm > 0 else vec

    def encode(self, text: str):
        return self._vec(text)

    def encode_batch(self, texts, batch_size: int = 64):
        return [self._vec(t) for t in texts]

    @property
    def dimensions(self) -> int:
        return self._dimensions

    @property
    def model_name(self) -> str:
        return "vocab-model"

    @property
    def backend(self) -> str:
        return "mock"

    @property
    def is_loaded(self) -> bool:
        return True


# The distinctive RELATIONAL fact ("igor", "manager", "assigned") appears
# nowhere in the path, the title, or the body — only in the generated summary.
# That is precisely the information the mechanical prefix cannot express.
_TARGET_PATH = "notes/q3-workstream-notes.md"
_TARGET_DOC = (
    "---\ntype: note\n---\n"
    "# Q3 workstream notes\n\n"
    "## Original Request\n\n"
    "Improve scanning coverage across every cloud account and container host, "
    "tighten remediation timelines with clearly documented ownership for each "
    "engineering team, and establish a weekly triage cadence for newly "
    "discovered exposures across the whole fleet.\n\n"
    "## Scope of Work\n\n"
    "Stand up executive dashboards for asset owners, define service level tiers "
    "by severity band, and run structured quarterly reviews together with the "
    "platform reliability and security operations groups across all regions."
)

_DECOY_DOC = (
    "# Coffee brewing notes\n\n"
    "## Method\n\n"
    + ("Grind size and water temperature shape espresso extraction quality. " * 5)
    + "\n\n## Beans\n\n"
    + ("Single origin beans from various regions vary in tasting notes. " * 5)
)

_SUMMARY = (
    "A vulnerability management mandate that Igor, the author's manager, "
    "assigned to them, covering scanning coverage and remediation ownership."
)

_DECOY_SUMMARY = (
    "A personal reference note on espresso brewing method and single origin "
    "bean tasting."
)

_RELATIONAL_QUERY = "mandate igor manager assigned"


def _install_bow(monkeypatch):
    import tools.embedding as embedding_module

    bow = _VocabEmbeddingService(384)
    monkeypatch.setattr(embedding_module, "get_embedding_service", lambda: bow)
    monkeypatch.setattr(embedding_module, "_service", bow)
    return bow


def _mock_generator(monkeypatch, summary=None):
    """Replace only the LLM call; cache + augmentation stay production code.

    Per-path by default so the decoy gets its OWN summary — handing every file
    the target's relational sentence would make the decoy match the relational
    query too and prove nothing about ranking.
    """
    import tools.context_summary as context_summary

    calls = []

    def fake_generate(
        path, title=None, headings=None, body="", config=None, timeout=None
    ):
        calls.append(path)
        if summary is not None:
            return summary
        return _SUMMARY if path == _TARGET_PATH else _DECOY_SUMMARY

    monkeypatch.setattr(context_summary, "generate_document_summary", fake_generate)
    monkeypatch.setattr("tools.conflict.haiku_available", lambda: True)
    context_summary.reset_unavailable_warning()
    return calls


def _write_vault(e2e_config):
    notes = e2e_config["vault_dir"] / "notes"
    notes.mkdir(exist_ok=True)
    (notes / "q3-workstream-notes.md").write_text(_TARGET_DOC)
    (notes / "coffee.md").write_text(_DECOY_DOC)


def _context_rows(db_url):
    with psycopg.connect(db_url) as conn:
        return conn.execute(
            "SELECT parent_file, summary, content_hash, model, generated_at "
            "FROM obsidian.document_context ORDER BY parent_file"
        ).fetchall()


def _seed_embedding_identity():
    """Record the first-run embedding identity.

    The e2e harness sets JARVIS_SKIP_MODEL_CHECK=1, so check_model_consistency()
    returns before recording; these tests are about what _record_contextual_meta
    does to an EXISTING record.
    """
    from tools.config import get_embedding_config
    from tools.embedding import get_embedding_model_identity
    from tools.schema import set_meta

    emb = get_embedding_config()
    set_meta("embedding_config", {
        "model": get_embedding_model_identity(emb),
        "dimensions": emb["dimensions"],
        "vector_type": "halfvec",
        "contextual_chunks": "mechanical",
    })


def _generate_out_of_band(e2e_config, **kwargs):
    """The operator's step 1: bin/generate_summaries.py, minus argparse.

    Uses the script's own request assembly so the content_hash it stores is the
    hash the index path will later look up.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from bin.generate_summaries import build_requests, list_chunked_files
    from tools.config import get_contextual_summaries_config
    from tools.context_summary import generate_missing_summaries

    config = get_contextual_summaries_config()
    with psycopg.connect(e2e_config["db_url"]) as conn:
        parent_files = list_chunked_files(conn)
        requests, unreadable = build_requests(
            str(e2e_config["vault_dir"]), parent_files, config
        )
        report = generate_missing_summaries(conn, requests, config=config, **kwargs)
        conn.commit()
    return report, parent_files, unreadable


# ── DDL ───────────────────────────────────────────────────────────────


def test_document_context_ddl_is_idempotent(e2e_config):
    from tools.schema import ensure_schema

    ensure_schema()
    ensure_schema()

    with psycopg.connect(e2e_config["db_url"]) as conn:
        columns = dict(conn.execute(
            """SELECT column_name, data_type FROM information_schema.columns
               WHERE table_schema = 'obsidian'
                 AND table_name = 'document_context'"""
        ).fetchall())
        assert columns == {
            "parent_file": "text",
            "summary": "text",
            "content_hash": "text",
            "model": "text",
            "generated_at": "timestamp with time zone",
        }
        primary_key = conn.execute(
            """SELECT a.attname
               FROM pg_index i
               JOIN pg_attribute a
                 ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
               WHERE i.indrelid = 'obsidian.document_context'::regclass
                 AND i.indisprimary"""
        ).fetchall()
        assert [row[0] for row in primary_key] == ["parent_file"]


# ── The indexing path never generates ─────────────────────────────────


def test_indexing_never_generates_even_with_an_llm_available(
    e2e_config, monkeypatch
):
    """The architectural rule, against real SQL: a fully reachable LLM plus a
    force reindex must still leave obsidian.document_context empty. Inline
    generation here is what blocked the MCP event loop on every vault write and
    made the per-run spend cap inoperative."""
    _install_bow(monkeypatch)
    calls = _mock_generator(monkeypatch)
    _write_vault(e2e_config)

    from tools.memory import index_file, index_vault

    assert index_vault(force=True)["success"] is True
    assert index_file(_TARGET_PATH)["success"] is True
    assert calls == [], "a runtime path generated a summary"
    assert _context_rows(e2e_config["db_url"]) == []


def test_indexing_succeeds_when_llm_unavailable(e2e_config, monkeypatch):
    """No API key, no CLI: indexing completes on mechanical augmentation, writes
    no cache rows, and records a COHERENT mechanical space — never a
    half-summary one."""
    _install_bow(monkeypatch)
    _write_vault(e2e_config)

    import tools.context_summary as context_summary

    monkeypatch.setattr("tools.conflict.haiku_available", lambda: False)
    context_summary.reset_unavailable_warning()

    from tools.memory import index_vault
    from tools.schema import get_meta

    _seed_embedding_identity()
    result = index_vault(force=True)
    assert result["success"] is True
    assert result["errors"] == []
    assert result["files_indexed"] == 2
    assert result["contextual_augmentation"] == "mechanical"
    assert _context_rows(e2e_config["db_url"]) == []
    assert get_meta("embedding_config")["contextual_chunks"] == "mechanical"


# ── Out-of-band generation, then the cache lifecycle ──────────────────


def test_out_of_band_generation_then_index_embeds_the_summary(
    e2e_config, monkeypatch
):
    """The full documented operator sequence: generate, then reindex."""
    _install_bow(monkeypatch)
    calls = _mock_generator(monkeypatch)
    _write_vault(e2e_config)

    from tools.memory import index_vault

    # Pass 1 — index so the chunked-file list exists (mechanical).
    first = index_vault(force=True)
    assert first["success"] is True
    assert first["contextual_augmentation"] == "mechanical"
    assert first["summaries_missing"] == 2
    assert "bin/generate_summaries.py" in first["summary_hint"]

    # Step 1 — generate out of band.
    report, parent_files, unreadable = _generate_out_of_band(e2e_config)
    assert sorted(parent_files) == ["notes/coffee.md", _TARGET_PATH]
    assert unreadable == []
    assert report.generated == 2
    assert sorted(calls) == ["notes/coffee.md", _TARGET_PATH]

    rows = _context_rows(e2e_config["db_url"])
    assert [row[0] for row in rows] == ["notes/coffee.md", _TARGET_PATH]
    target_row = rows[1]
    assert target_row[1] == _SUMMARY
    assert len(target_row[2]) == 64  # sha256 hex
    assert target_row[3]  # model recorded

    # Step 2 — re-embed. Now the space is genuinely 'summary'.
    import tools.embedding as embedding_module

    encoded: list[str] = []
    inner = embedding_module.get_embedding_service()

    class _Recording:
        def __getattr__(self, name):
            return getattr(inner, name)

        def encode(self, text):
            encoded.append(text)
            return inner.encode(text)

        def encode_batch(self, texts, batch_size: int = 64):
            encoded.extend(texts)
            return inner.encode_batch(texts, batch_size)

    recorder = _Recording()
    monkeypatch.setattr(embedding_module, "get_embedding_service", lambda: recorder)
    monkeypatch.setattr(embedding_module, "_service", recorder)

    second = index_vault(force=True)
    assert second["success"] is True
    assert second["contextual_augmentation"] == "summary"
    assert second["summaries_used"] == second["summary_candidates"] == 2
    assert "summary_hint" not in second

    prefixed = [t for t in encoded if t.startswith(f"Document: {_TARGET_PATH}")]
    assert prefixed, "expected augmented embed inputs for the chunked target"
    for text in prefixed:
        lines = text.split("\n")
        assert lines[1] == _SUMMARY, "summary line must follow the mechanical line"
        assert lines[2] == "", "prefix must end in a blank line"

    # The STORED column must be byte-identical to the raw chunk: no prefix, no
    # summary. UI, injection budgets, and telemetry all read this text.
    with psycopg.connect(e2e_config["db_url"]) as conn:
        stored = conn.execute(
            "SELECT document FROM obsidian.documents WHERE parent_file = %s",
            (_TARGET_PATH,),
        ).fetchall()
    assert stored
    for (document,) in stored:
        assert not document.startswith("Document: ")
        assert _SUMMARY not in document
        assert document in _TARGET_DOC or document.strip() in _TARGET_DOC


def test_second_generation_pass_is_free(e2e_config, monkeypatch):
    """Idempotency against real SQL — the entire point of content_hash."""
    _install_bow(monkeypatch)
    calls = _mock_generator(monkeypatch)
    _write_vault(e2e_config)

    from tools.memory import index_vault

    index_vault(force=True)
    _generate_out_of_band(e2e_config)
    assert len(calls) == 2

    calls.clear()
    report, _files, _unreadable = _generate_out_of_band(e2e_config)
    assert calls == [], "unchanged files must reuse the cached summary"
    assert report.already_valid == 2
    assert report.generated == 0
    assert _context_rows(e2e_config["db_url"])[1][1] == _SUMMARY


def test_edited_file_regenerates_and_overwrites_its_row(e2e_config, monkeypatch):
    _install_bow(monkeypatch)
    _mock_generator(monkeypatch)
    _write_vault(e2e_config)

    from tools.memory import index_vault

    index_vault(force=True)
    _generate_out_of_band(e2e_config)
    original_hash = [
        row for row in _context_rows(e2e_config["db_url"]) if row[0] == _TARGET_PATH
    ][0][2]

    target = e2e_config["vault_dir"] / "notes" / "q3-workstream-notes.md"
    target.write_text(_TARGET_DOC + "\n\n## Addendum\n\n" + ("New material. " * 30))
    _mock_generator(
        monkeypatch,
        summary="An updated mandate summary naming Igor as the author's manager.",
    )

    _generate_out_of_band(e2e_config)
    rows = _context_rows(e2e_config["db_url"])
    assert len(rows) == 2, "still one row per file — no duplicates"
    updated = [row for row in rows if row[0] == _TARGET_PATH][0]
    assert updated[2] != original_hash
    assert updated[1] == (
        "An updated mandate summary naming Igor as the author's manager."
    )


def test_run_cap_is_enforced_once_against_real_sql(e2e_config, monkeypatch):
    _install_bow(monkeypatch)
    calls = _mock_generator(monkeypatch, summary="A situating sentence for a note.")
    notes = e2e_config["vault_dir"] / "notes"
    notes.mkdir(exist_ok=True)
    for index in range(8):
        (notes / f"f{index:02d}.md").write_text(
            f"# F{index}\n\n## A\n\n" + ("body text goes here. " * 30)
            + "\n\n## B\n\n" + ("more body text here. " * 30)
        )

    from tools.memory import index_vault

    index_vault(force=True)
    report, parent_files, _unreadable = _generate_out_of_band(
        e2e_config, limit=3, concurrency=2
    )
    assert len(parent_files) == 8
    assert len(calls) == 3
    assert report.skipped_over_limit == 5
    assert len(_context_rows(e2e_config["db_url"])) == 3

    # Repeated runs converge instead of re-rolling the same subset.
    _generate_out_of_band(e2e_config, limit=3)
    assert len(_context_rows(e2e_config["db_url"])) == 6


# ── Cache coherence: the stale row is really DELETEd ──────────────────


def test_reindexing_an_edited_file_deletes_its_stale_summary_row(
    e2e_config, monkeypatch
):
    """The coherence fix, at the SQL boundary. Readers are hash-blind, so a
    surviving row would make every later rerank score
    `mechanical + STALE summary + new chunk text` — text no stored vector
    corresponds to — and the shadow scorer would persist that logit as a valid
    calibration label."""
    _install_bow(monkeypatch)
    _mock_generator(monkeypatch)
    _write_vault(e2e_config)

    from tools.context_summary import fetch_document_summaries
    from tools.memory import index_file, index_vault

    index_vault(force=True)
    _generate_out_of_band(e2e_config)
    assert fetch_document_summaries([_TARGET_PATH]) == {_TARGET_PATH: _SUMMARY}

    # Rewrite the note into something completely different and reindex it.
    target = e2e_config["vault_dir"] / "notes" / "q3-workstream-notes.md"
    target.write_text(_DECOY_DOC)
    assert index_file(_TARGET_PATH)["success"] is True

    rows = _context_rows(e2e_config["db_url"])
    assert [row[0] for row in rows] == ["notes/coffee.md"], (
        "the stale row for the rewritten file must be DELETEd"
    )
    assert fetch_document_summaries([_TARGET_PATH]) == {}

    # And the reranker now sees exactly what was embedded: mechanical only.
    from tools.config import get_contextual_augmentation_mode
    from tools.query import _rerank_doc_text, _rerank_summaries
    from tools.schema import execute_query

    row = execute_query(
        """SELECT id, document, title, chunk_heading, chunk_total, parent_file
           FROM obsidian.documents WHERE parent_file = %s AND chunk_total > 1
           ORDER BY chunk_index LIMIT 1""",
        (_TARGET_PATH,), fetch="one",
    )
    assert row is not None
    entry = {
        "document": row["document"],
        "parent_file": row["parent_file"],
        "_schema": "obsidian",
        "metadata": {
            "title": row["title"],
            "chunk_heading": row["chunk_heading"],
            "chunk_total": row["chunk_total"],
        },
    }
    text = _rerank_doc_text(
        entry, get_contextual_augmentation_mode(), _rerank_summaries([entry])
    )
    assert _SUMMARY not in text


def test_full_force_reindex_drops_every_stale_row(e2e_config, monkeypatch):
    _install_bow(monkeypatch)
    _mock_generator(monkeypatch)
    _write_vault(e2e_config)

    from tools.memory import index_vault

    index_vault(force=True)
    _generate_out_of_band(e2e_config)
    assert len(_context_rows(e2e_config["db_url"])) == 2

    for name, body in (
        ("q3-workstream-notes.md", _DECOY_DOC),
        ("coffee.md", _TARGET_DOC),
    ):
        (e2e_config["vault_dir"] / "notes" / name).write_text(body)

    result = index_vault(force=True)
    assert result["contextual_augmentation"] == "mechanical"
    assert _context_rows(e2e_config["db_url"]) == []


# ── Achieved augmentation state in local.meta (JSONB) ─────────────────


def test_partial_coverage_records_partial_summary_state(e2e_config, monkeypatch):
    """Only some chunked files have a summary → the vault spans two embedding
    spaces, and local.meta must say so rather than restating the config."""
    _install_bow(monkeypatch)
    _write_vault(e2e_config)

    from tools.memory import index_vault
    from tools.schema import get_meta

    _seed_embedding_identity()
    index_vault(force=True)

    # Generate for ONE file only (a run cap, or a per-file API failure).
    _mock_generator(monkeypatch)
    _generate_out_of_band(e2e_config, limit=1)
    assert len(_context_rows(e2e_config["db_url"])) == 1

    result = index_vault(force=True)
    assert result["summary_candidates"] == 2
    assert result["summaries_used"] == 1
    assert result["contextual_augmentation"] == "partial-summary"

    stored = get_meta("embedding_config")
    assert stored["contextual_chunks"] == "partial-summary"
    assert stored["contextual_coverage"] == {
        "chunked_files": 2, "files_with_summary": 1,
    }

    # The startup check must warn about the PARTIAL state specifically, and name
    # the two-step remedy.
    import logging

    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    from tools import schema as schema_module

    handler = _Capture()
    schema_module.logger.addHandler(handler)
    monkeypatch.delenv("JARVIS_SKIP_MODEL_CHECK", raising=False)
    try:
        schema_module.check_model_consistency()
    finally:
        schema_module.logger.removeHandler(handler)
    joined = "\n".join(records)
    assert "PARTIAL" in joined
    assert "1 of 2" in joined
    assert "bin/generate_summaries.py" in joined


def test_reindexer_records_the_achieved_state_not_the_config(
    e2e_config, monkeypatch
):
    """bin/reindex_embeddings.py only READS the cache, so with an empty cache it
    must record 'mechanical' — recording 'summary' is what disarmed the startup
    consistency check for good."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from bin.generate_summaries import get_connection
    from bin.reindex_embeddings import STORES, stage_store
    from tools.config import get_contextual_augmentation_mode
    from tools.memory import index_vault, resolve_achieved_augmentation

    service = _install_bow(monkeypatch)
    _write_vault(e2e_config)
    assert index_vault(force=True)["success"] is True
    assert get_contextual_augmentation_mode() == "summary"

    conn = get_connection(e2e_config["db_url"])
    try:
        coverage: dict = {}
        stage_store(
            conn, STORES["obsidian"], service, dimensions=384, batch_size=16,
            coverage=coverage,
        )
        conn.commit()
    finally:
        conn.close()

    chunked = len(coverage.get("chunked_files", set()))
    with_summary = len(coverage.get("files_with_summary", set()))
    assert chunked == 2
    assert with_summary == 0
    assert resolve_achieved_augmentation(chunked, with_summary) == "mechanical"


def test_force_model_record_measures_coverage_instead_of_trusting_config(
    e2e_config, monkeypatch
):
    """`init_db.py --force-model-record` re-embeds nothing, so stamping the
    CONFIGURED mode would relabel a mechanical space as 'summary' and silence
    the mismatch warning forever — the exact failure operators reach for this
    flag to escape."""
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from bin.init_db import measure_contextual_state
    from tools.config import get_contextual_augmentation_mode
    from tools.memory import index_vault

    _install_bow(monkeypatch)
    _write_vault(e2e_config)
    assert index_vault(force=True)["success"] is True
    assert get_contextual_augmentation_mode() == "summary"

    # Empty cache → mechanical, despite config saying 'summary'.
    state, coverage = measure_contextual_state()
    assert state == "mechanical"
    assert coverage == {"chunked_files": 2, "files_with_summary": 0}

    # One of two covered → partial.
    _mock_generator(monkeypatch)
    _generate_out_of_band(e2e_config, limit=1)
    state, coverage = measure_contextual_state()
    assert state == "partial-summary"
    assert coverage == {"chunked_files": 2, "files_with_summary": 1}

    # Fully covered → summary.
    _generate_out_of_band(e2e_config)
    state, coverage = measure_contextual_state()
    assert state == "summary"
    assert coverage == {"chunked_files": 2, "files_with_summary": 2}


# ── Shadow scorer: text identity and cache-drift guard ────────────────


def test_shadow_resolved_text_matches_live_rerank_text(e2e_config, monkeypatch):
    """Divergence here silently corrupts the calibration corpus — the bug class
    already found and fixed twice. Compare the two code paths on the SAME row."""
    _install_bow(monkeypatch)
    _mock_generator(monkeypatch)
    _write_vault(e2e_config)

    from tools.memory import index_vault

    assert index_vault(force=True)["success"] is True
    _generate_out_of_band(e2e_config)
    assert index_vault(force=True)["contextual_augmentation"] == "summary"

    from tools.config import get_contextual_augmentation_mode
    from tools.query import _rerank_doc_text, _rerank_summaries
    from tools.retrieval_telemetry import (
        CandidateTrace, _fetch_candidate_documents, record_event,
    )
    from tools.schema import _get_pool, execute_query

    row = execute_query(
        """SELECT id, document, title, chunk_heading, chunk_total, parent_file,
                  chunk_index
           FROM obsidian.documents
           WHERE parent_file = %s AND chunk_total > 1
           ORDER BY chunk_index LIMIT 1""",
        (_TARGET_PATH,), fetch="one",
    )
    assert row is not None

    # ── Live path (query rerank) ──────────────────────────────────────
    entry = {
        "document": row["document"],
        "parent_file": row["parent_file"],
        "_schema": "obsidian",
        "metadata": {
            "title": row["title"],
            "chunk_heading": row["chunk_heading"],
            "chunk_total": row["chunk_total"],
        },
    }
    live_text = _rerank_doc_text(
        entry, get_contextual_augmentation_mode(), _rerank_summaries([entry])
    )
    assert _SUMMARY in live_text

    # ── Shadow path (telemetry document resolver) ─────────────────────
    trace_id = record_event(
        purpose="context_injection", query=_RELATIONAL_QUERY,
        candidates=[CandidateTrace(
            schema_name="obsidian", doc_id=row["id"],
            parent_file=row["parent_file"], chunk_index=row["chunk_index"],
            vector_rank=1, similarity=0.5, terminal_reason="selected",
        )],
        funnel={"ann_unique": 1}, latency={"total_ms": 1}, outcome="results",
    )
    assert trace_id
    pool = _get_pool()
    with pool.connection() as conn:
        docs, missing = _fetch_candidate_documents(conn, trace_id, 20)
    assert missing == 1 and len(docs) == 1
    assert docs[0]["document"] == live_text


def test_shadow_drift_guard_detects_a_summary_newer_than_the_event(
    e2e_config, monkeypatch
):
    """The mode guard compares config, but the rerank text also depends on the
    cache, which is not stamped. A summary generated between retrieval and
    shadow scoring would otherwise record +0.03 where the live path produced
    −8.16."""
    _install_bow(monkeypatch)
    _write_vault(e2e_config)

    from tools.memory import index_vault
    from tools.retrieval_telemetry import (
        CandidateTrace, _summary_cache_drifted, record_event,
    )
    from tools.schema import _get_pool, execute_query

    assert index_vault(force=True)["success"] is True
    row = execute_query(
        """SELECT id, parent_file, chunk_index FROM obsidian.documents
           WHERE parent_file = %s AND chunk_total > 1
           ORDER BY chunk_index LIMIT 1""",
        (_TARGET_PATH,), fetch="one",
    )
    trace_id = record_event(
        purpose="context_injection", query=_RELATIONAL_QUERY,
        candidates=[CandidateTrace(
            schema_name="obsidian", doc_id=row["id"],
            parent_file=row["parent_file"], chunk_index=row["chunk_index"],
            vector_rank=1, similarity=0.5, terminal_reason="selected",
        )],
        funnel={"ann_unique": 1}, latency={"total_ms": 1}, outcome="results",
    )
    created_at = execute_query(
        "SELECT created_at FROM local.retrieval_events WHERE id = %s::uuid",
        (trace_id,), fetch="one",
    )["created_at"]
    pool = _get_pool()

    # No cache at all → no drift.
    assert _summary_cache_drifted(pool, trace_id, created_at) is False

    # A summary for an UNRELATED file must not trip the guard.
    _mock_generator(monkeypatch)
    _generate_out_of_band(e2e_config, limit=0)
    with psycopg.connect(e2e_config["db_url"], autocommit=True) as conn:
        conn.execute(
            "DELETE FROM obsidian.document_context WHERE parent_file = %s",
            (_TARGET_PATH,),
        )
    assert _summary_cache_drifted(pool, trace_id, created_at) is False

    # A summary for THIS candidate's file, generated after the event → drift.
    _generate_out_of_band(e2e_config)
    assert _summary_cache_drifted(pool, trace_id, created_at) is True

    # An event recorded AFTER the summary is unaffected.
    later = record_event(
        purpose="context_injection", query=_RELATIONAL_QUERY,
        candidates=[CandidateTrace(
            schema_name="obsidian", doc_id=row["id"],
            parent_file=row["parent_file"], chunk_index=row["chunk_index"],
            vector_rank=1, similarity=0.5, terminal_reason="selected",
        )],
        funnel={"ann_unique": 1}, latency={"total_ms": 1}, outcome="results",
    )
    later_created = execute_query(
        "SELECT created_at FROM local.retrieval_events WHERE id = %s::uuid",
        (later,), fetch="one",
    )["created_at"]
    assert _summary_cache_drifted(pool, later, later_created) is False


# ── Augmentation era is readable back out of config_snapshot ──────────


def test_calibration_data_can_be_separated_by_augmentation_era(
    e2e_config, monkeypatch
):
    """Phase-2 threshold calibration must not pool mechanical-era and
    summary-era logits: the same chunk scores −8.16 and +0.03. The SQL has to
    read the era back out of the JSONB config_snapshot, including the legacy
    boolean form."""
    _install_bow(monkeypatch)
    _write_vault(e2e_config)

    from tools.memory import index_vault
    from tools.retrieval_telemetry import (
        CandidateTrace, export_labeled_events, put_event_feedback, record_event,
        simulate_policy,
    )
    from tools.schema import execute_query

    assert index_vault(force=True)["success"] is True
    rows = execute_query(
        """SELECT id, parent_file, chunk_index FROM obsidian.documents
           WHERE chunk_total > 1 ORDER BY id LIMIT 2"""
    )
    assert len(rows) == 2

    eras = [
        ({"contextual_augmentation": "mechanical"}, -8.16, "useful"),
        ({"contextual_augmentation": "summary"}, 0.03, "useful"),
        ({"contextual_embeddings": True}, -7.0, "noisy"),   # legacy boolean
        ({}, -5.0, "noisy"),                                # unstamped
    ]
    trace_ids = []
    for index, (snapshot, logit, verdict) in enumerate(eras):
        trace_id = record_event(
            purpose="context_injection", query=f"query {index}",
            candidates=[CandidateTrace(
                schema_name="obsidian", doc_id=rows[index % 2]["id"],
                parent_file=rows[index % 2]["parent_file"],
                chunk_index=rows[index % 2]["chunk_index"],
                vector_rank=1, similarity=0.9, terminal_reason="selected",
            )],
            funnel={"ann_unique": 1}, latency={"total_ms": 1}, outcome="results",
            config_snapshot=snapshot,
        )
        assert trace_id
        trace_ids.append(trace_id)
        with psycopg.connect(e2e_config["db_url"], autocommit=True) as conn:
            conn.execute(
                """UPDATE local.retrieval_candidates SET raw_bge_logit = %s
                   WHERE event_id = %s::uuid""",
                (logit, trace_id),
            )
        put_event_feedback(trace_id, {"verdict": verdict})

    census = simulate_policy({"policy": "bge-only", "bge_logit_threshold": -4.0})
    assert census["augmentation_eras"] == {
        "mechanical": 2,  # explicit + legacy boolean True
        "summary": 1,
        "unstamped": 1,
    }
    assert census["augmentation_eras_mixed"] is True

    summary_era = simulate_policy({
        "policy": "bge-only", "bge_logit_threshold": -4.0,
        "contextual_augmentation": "summary",
    })
    assert summary_era["candidate_count"] == 1
    assert summary_era["selected_count"] == 1

    mechanical_era = simulate_policy({
        "policy": "bge-only", "bge_logit_threshold": -4.0,
        "contextual_augmentation": "mechanical",
    })
    assert mechanical_era["candidate_count"] == 2
    assert mechanical_era["selected_count"] == 0

    exported = export_labeled_events()
    assert exported
    assert {row["contextual_augmentation"] for row in exported} == {
        "mechanical", "summary", "unstamped",
    }


# ── Acceptance shape ─────────────────────────────────────────────────


def test_summary_only_relational_fact_makes_document_retrievable(
    e2e_config, monkeypatch
):
    """THE acceptance case. "igor", "manager", "mandate" and "assigned" appear
    nowhere in the target's path, title, or body — only in its generated
    summary. With summaries ON the relational query must retrieve the document;
    with summaries OFF (mechanical only) it must not."""
    _install_bow(monkeypatch)
    _mock_generator(monkeypatch)
    _write_vault(e2e_config)

    # Guard the premise: the fact really is absent from every mechanical input.
    for term in ("igor", "manager", "mandate", "assigned"):
        assert term not in _TARGET_DOC.lower()
        assert term not in _TARGET_PATH.lower()

    from tools.memory import index_vault
    from tools.query import query_vault

    assert index_vault(force=True)["success"] is True
    _generate_out_of_band(e2e_config)
    assert index_vault(force=True)["contextual_augmentation"] == "summary"

    result_on = query_vault(_RELATIONAL_QUERY, n_results=5)
    assert result_on["success"] is True
    hits_on = {entry["path"]: entry for entry in result_on["results"]}
    assert _TARGET_PATH in hits_on, "summary must make the target retrievable"
    assert result_on["results"][0]["path"] == _TARGET_PATH, "target should rank #1"
    similarity_on = float(hits_on[_TARGET_PATH]["similarity"])
    assert similarity_on > 0.2, similarity_on

    # ── Mechanical only: the same query loses the document ────────────
    monkeypatch.setattr(
        "tools.memory.get_contextual_augmentation_mode", lambda: "mechanical"
    )
    monkeypatch.setattr(
        "tools.query.get_contextual_augmentation_mode", lambda: "mechanical"
    )
    assert index_vault(force=True)["success"] is True

    result_off = query_vault(_RELATIONAL_QUERY, n_results=5)
    assert result_off["success"] is True
    hits_off = {
        entry["path"]: float(entry["similarity"]) for entry in result_off["results"]
    }
    similarity_off = hits_off.get(_TARGET_PATH, 0.0)
    assert similarity_off < 0.05, (
        f"without the summary the relational terms share nothing with the "
        f"embedded text, got {similarity_off}"
    )
    assert similarity_on > similarity_off + 0.2
