"""Integration tests for contextual chunk augmentation across every site.

Covers the three places a fragment is embedded or reranked:
  1. vault indexing embed path (memory._upsert_batch)
  2. query rerank paths (query_vault + semantic_context)
  3. the shadow scorer's document resolver (retrieval_telemetry._fetch_candidate_documents)

Plus the config-off rollback switch and the invariants: the STORED document
column stays byte-identical to the raw chunk, and whole-document rows never get
a prefix.
"""

from unittest.mock import patch

import pytest

from tests.conftest import MockEmbeddingService


# A multi-heading document that chunks into several fragments (chunk_total > 1).
CHUNKED_DOC = (
    "# Quarterly Vulnerability Report\n\n"
    "## Coverage\n\n"
    + ("Scanning coverage across cloud accounts and hosts is tracked weekly. " * 6)
    + "\n\n"
    "## Remediation\n\n"
    + ("Remediation timelines and SLA tracking for asset owners each sprint. " * 6)
)

# A short document that stays a single whole-document row (chunk_total == 1).
SINGLE_DOC = "# Single Note\n\nA short standalone note that stays as one chunk."


class _CapturingEmbeddingService(MockEmbeddingService):
    """MockEmbeddingService that records every text handed to the encoder."""

    def __init__(self, dimensions=384):
        super().__init__(dimensions)
        self.encoded: list[str] = []

    def encode(self, text: str):
        self.encoded.append(text)
        return super().encode(text)

    def encode_batch(self, texts, batch_size: int = 64):
        self.encoded.extend(texts)
        return super().encode_batch(texts, batch_size)


def _write_files(mock_config):
    notes = mock_config.vault_path / "notes"
    notes.mkdir(exist_ok=True)
    (notes / "quarterly.md").write_text(CHUNKED_DOC)
    (notes / "single.md").write_text(SINGLE_DOC)


class TestIndexingEmbedAugmentation:
    def _install_capture(self, monkeypatch):
        import tools.embedding as embedding_module

        cap = _CapturingEmbeddingService(384)
        monkeypatch.setattr(embedding_module, "get_embedding_service", lambda: cap)
        monkeypatch.setattr(embedding_module, "_service", cap)
        return cap

    def test_chunks_embedded_with_prefix_stored_raw(self, mock_config, monkeypatch):
        """Chunk fragments are embedded WITH the document-context prefix, while
        the stored `document` column stays byte-identical to the raw chunk."""
        cap = self._install_capture(monkeypatch)
        _write_files(mock_config)

        from tools.memory import index_vault

        result = index_vault()
        assert result["success"] is True

        # At least one embedded input for the chunked file carries the prefix.
        prefixed = [
            t for t in cap.encoded
            if t.startswith("Document: notes/quarterly.md — Quarterly Vulnerability Report")
        ]
        assert prefixed, "expected augmented embed input for chunked file"
        # The prefix names the section heading (heading trail).
        assert any("› Coverage" in t or "› Remediation" in t for t in prefixed)

        # STORED documents must be raw — no prefix leaked into the DB.
        for row in mock_config.db.vault_rows.values():
            assert not row["document"].startswith("Document: notes/")
            if row["parent_file"] == "notes/quarterly.md":
                assert int(row["chunk_total"]) > 1

    def test_whole_document_row_not_prefixed(self, mock_config, monkeypatch):
        """Unchunked whole-document rows (which already begin with their own
        title) are embedded WITHOUT a prefix."""
        cap = self._install_capture(monkeypatch)
        _write_files(mock_config)

        from tools.memory import index_vault

        index_vault()

        # The single-doc body was embedded verbatim (no Document: prefix).
        single_inputs = [t for t in cap.encoded if "standalone note" in t]
        assert single_inputs
        assert all(not t.startswith("Document:") for t in single_inputs)

    def test_config_off_passthrough(self, mock_config, monkeypatch):
        """With contextual_embeddings disabled (mode 'none'), chunks are raw."""
        cap = self._install_capture(monkeypatch)
        _write_files(mock_config)
        monkeypatch.setattr(
            "tools.memory.get_contextual_augmentation_mode", lambda: "none"
        )

        from tools.memory import index_vault

        index_vault()

        assert not any(t.startswith("Document: notes/") for t in cap.encoded)
        # Coverage body still embedded — just without the prefix.
        assert any("Scanning coverage" in t for t in cap.encoded)


class TestQueryRerankAugmentation:
    """The reranker must see exactly what the embedder saw."""

    def _capture_rerank(self, monkeypatch):
        import tools.reranking as rerank_module

        captured = {"docs": None}

        def fake_rerank(query, documents, vector_scores, config=None):
            captured["docs"] = list(documents)
            return vector_scores  # identity → treated as fallback, no rescore

        def fake_rerank_multi(queries, documents, vector_scores, config=None):
            captured["docs"] = list(documents)
            return vector_scores

        monkeypatch.setattr(rerank_module, "rerank", fake_rerank)
        monkeypatch.setattr(rerank_module, "rerank_multi", fake_rerank_multi)
        return captured

    def test_query_vault_reranker_receives_augmented_chunk(
        self, mock_config, monkeypatch
    ):
        _write_files(mock_config)
        from tools.memory import index_vault

        index_vault()
        mock_config.set(memory={"reranking": {"enabled": True, "backend": "host"}})
        captured = self._capture_rerank(monkeypatch)

        from tools.query import query_vault

        result = query_vault("vulnerability coverage remediation", n_results=5)
        assert result["success"] is True
        assert captured["docs"] is not None
        # The chunked file's fragment reaches the reranker with its prefix; the
        # single whole-doc row does not.
        assert any(d.startswith("Document: notes/quarterly.md") for d in captured["docs"])
        assert any(
            "standalone note" in d and not d.startswith("Document:")
            for d in captured["docs"]
        )

    def test_semantic_context_reranker_receives_augmented_chunk(
        self, mock_config, monkeypatch
    ):
        _write_files(mock_config)
        from tools.memory import index_vault

        index_vault()
        mock_config.set(memory={"reranking": {"enabled": True, "backend": "host"}})
        captured = self._capture_rerank(monkeypatch)

        from tools.query import semantic_context

        # threshold=-1 lets the hash-embedding candidates through the gate.
        semantic_context("vulnerability coverage remediation", threshold=-1.0)
        assert captured["docs"] is not None
        assert any(d.startswith("Document: notes/quarterly.md") for d in captured["docs"])

    def test_query_vault_rerank_config_off(self, mock_config, monkeypatch):
        _write_files(mock_config)
        from tools.memory import index_vault

        index_vault()
        mock_config.set(memory={"reranking": {"enabled": True, "backend": "host"}})
        captured = self._capture_rerank(monkeypatch)
        monkeypatch.setattr(
            "tools.query.get_contextual_augmentation_mode", lambda: "none"
        )

        from tools.query import query_vault

        query_vault("vulnerability coverage remediation", n_results=5)
        assert captured["docs"] is not None
        assert not any(d.startswith("Document:") for d in captured["docs"])


class _FakeCursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConn:
    """Scripts the two SQL shapes _fetch_candidate_documents issues."""

    def __init__(self, refs, obsidian_rows, local_rows=None):
        self._refs = refs
        self._obsidian = obsidian_rows  # doc_id -> (document, title, heading, total, parent)
        self._local = local_rows or {}  # doc_id -> (document,)

    def execute(self, sql, params=None):
        s = " ".join(sql.split()).lower()
        if "from local.retrieval_candidates" in s:
            return _FakeCursor(self._refs)
        if "from obsidian.documents" in s:
            row = self._obsidian.get(params[0])
            return _FakeCursor([row] if row else [])
        if "from local.memory_chunks" in s or "from local.memories" in s:
            row = self._local.get(params[0])
            return _FakeCursor([row] if row else [])
        return _FakeCursor([])


class TestShadowScorerAugmentation:
    """retrieval_telemetry._fetch_candidate_documents must augment identically
    to the live path, or shadow logits diverge and corrupt calibration data."""

    def test_obsidian_chunk_augmented(self):
        from tools.retrieval_telemetry import _fetch_candidate_documents

        refs = [
            # (candidate_key, schema, doc_id, chunk_index, query_window_index, raw_logit)
            ("k1", "obsidian", "vault::notes/quarterly.md#chunk-1", 1, 0, None),
        ]
        obsidian = {
            "vault::notes/quarterly.md#chunk-1": (
                "Remediation timelines and SLA tracking.",  # document (raw body)
                "Quarterly Vulnerability Report",           # title
                "Remediation",                              # chunk_heading
                3,                                          # chunk_total
                "notes/quarterly.md",                       # parent_file
            )
        }
        conn = _FakeConn(refs, obsidian)
        out, missing = _fetch_candidate_documents(conn, "evt", 20)
        assert missing == 1
        assert len(out) == 1
        assert out[0]["document"].startswith(
            "Document: notes/quarterly.md — Quarterly Vulnerability Report › Remediation\n\n"
        )
        assert out[0]["document"].endswith("Remediation timelines and SLA tracking.")

    def test_obsidian_whole_doc_not_augmented(self):
        from tools.retrieval_telemetry import _fetch_candidate_documents

        refs = [("k1", "obsidian", "vault::notes/single.md", 0, 0, None)]
        obsidian = {
            "vault::notes/single.md": (
                "A short standalone note.", "Single Note", "", 1, "notes/single.md"
            )
        }
        out, _ = _fetch_candidate_documents(_FakeConn(refs, obsidian), "evt", 20)
        assert out[0]["document"] == "A short standalone note."

    def test_local_memory_not_augmented(self):
        from tools.retrieval_telemetry import _fetch_candidate_documents

        refs = [("k1", "local", "obs::123", 0, 0, None)]
        local = {"obs::123": ("a bare observation body",)}
        out, _ = _fetch_candidate_documents(_FakeConn(refs, {}, local), "evt", 20)
        assert out[0]["document"] == "a bare observation body"

    def test_already_scored_skipped(self):
        from tools.retrieval_telemetry import _fetch_candidate_documents

        refs = [("k1", "obsidian", "vault::notes/quarterly.md#chunk-1", 1, 0, -2.3)]
        obsidian = {"vault::notes/quarterly.md#chunk-1": ("body", "T", "H", 3, "notes/quarterly.md")}
        out, missing = _fetch_candidate_documents(_FakeConn(refs, obsidian), "evt", 20)
        assert out == []
        assert missing == 0

    def test_config_off_no_prefix(self, monkeypatch):
        from tools.retrieval_telemetry import _fetch_candidate_documents

        monkeypatch.setattr(
            "tools.config.get_contextual_embeddings_enabled", lambda: False
        )
        refs = [("k1", "obsidian", "vault::notes/quarterly.md#chunk-1", 1, 0, None)]
        obsidian = {
            "vault::notes/quarterly.md#chunk-1": (
                "Remediation body.", "Quarterly Report", "Remediation", 3, "notes/quarterly.md"
            )
        }
        out, _ = _fetch_candidate_documents(_FakeConn(refs, obsidian), "evt", 20)
        assert out[0]["document"] == "Remediation body."


def test_flag_honors_real_config_key(mock_config):
    """The rollback switch must work through the actual config chain — every
    other test stubs the getter, so this is the only guard against the key
    being renamed/moved while users' config silently stops working."""
    from tools.config import get_contextual_embeddings_enabled

    assert get_contextual_embeddings_enabled() is True  # shipped default

    mock_config.set(memory={"chunking": {"contextual_embeddings": False}})
    assert get_contextual_embeddings_enabled() is False

    # A user chunking section WITHOUT the key keeps the default (merge, not replace).
    mock_config.set(memory={"chunking": {"min_chunk_chars": 200}})
    assert get_contextual_embeddings_enabled() is True


def test_query_vault_stamps_augmentation_flag_into_telemetry(mock_config, monkeypatch):
    """Shadow rescoring and offline calibration must be able to tell which
    augmentation mode produced an event."""
    import tools.query as query_module
    import tools.retrieval_telemetry as telemetry

    captured = {}

    def capture(**kwargs):
        captured.update(kwargs)
        return None

    monkeypatch.setattr(telemetry, "record_event", capture)
    # Bypass the empty-collection early exit so the MAIN record_event fires.
    monkeypatch.setattr(query_module, "execute_query", lambda *a, **k: {"cnt": 5})
    monkeypatch.setattr(
        query_module, "_search_query_windows",
        lambda *a, **k: ([], {"terms_added": [], "intent": None}),
    )

    result = query_module.query_vault("stamping test", n_results=3)
    assert result["success"] is True
    assert captured["config_snapshot"]["contextual_embeddings"] is True


def test_mechanical_mode_still_drops_stale_summary_rows(mock_config):
    """H3-1: the documented rollback (contextual_summaries.enabled=false) used to
    skip cache cleanup entirely, so an edit made while mechanical left a stale
    row that readers — hash-blind by design — served after the next flip back on.
    Building the request must not be gated on summary mode."""
    import tools.memory as memory
    from tools.config import get_contextual_augmentation_mode

    mock_config.set(memory={"chunking": {
        "enabled": True, "contextual_embeddings": True,
        "contextual_summaries": {"enabled": False},
    }})
    assert get_contextual_augmentation_mode() == "mechanical"

    request = memory._build_summary_request("notes/mandate.md", "fresh body", "Mandate")
    assert request is not None, "mechanical mode must still resolve (to clean) the cache"
    assert request.parent_file == "notes/mandate.md"
    assert request.content_hash

    # Fully disabled augmentation needs no cache work at all.
    mock_config.set(memory={"chunking": {
        "enabled": True, "contextual_embeddings": False,
        "contextual_summaries": {"enabled": False},
    }})
    assert memory._build_summary_request("notes/mandate.md", "fresh body", "Mandate") is None


def test_coverage_denominator_counts_the_whole_store(mock_config, monkeypatch):
    """H2-1: index_vault skips sensitive/secret files BEFORE the force-delete, so
    their previous-era rows survive. Scoring the verdict on the run's own
    counters stamped a mixed space as clean 'summary' with no warning."""
    import tools.memory as memory

    captured = {}

    def fake_resolve(chunked, with_summary, mode=None):
        captured["args"] = (chunked, with_summary)
        return "partial-summary"

    monkeypatch.setattr(memory, "_count_chunked_files", lambda: 10)
    monkeypatch.setattr(memory, "resolve_achieved_augmentation", fake_resolve)
    monkeypatch.setattr(memory, "execute_query", lambda *a, **k: None)

    state = memory._record_contextual_meta(10, 6)
    assert captured["args"] == (10, 6)
    assert state == "partial-summary"
