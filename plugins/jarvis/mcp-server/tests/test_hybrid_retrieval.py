"""Integration contracts for hybrid retrieval (Phase 1) in semantic_context.

Covers the channel union + dedup tagging, the reserved rerank slots that keep
low-cosine lexical rows in the rerank batch, the recall-additive OR-gate
(cosine-passed kept / logit-rescued kept / both-failed rejected /
unjudged-dropped on reranker fallback), and funnel accounting.

The lexical channel and the reranker are fully controlled: the lexical module
functions and _cross_schema_search are patched to inject deterministic rows,
and the host reranker client is patched to return chosen logits.
"""

from __future__ import annotations

import json

import pytest


# ── Config + fake helpers ─────────────────────────────────────────────


def _set_memory(mock_config, **sections):
    """Merge memory subsections into the live config (preserves postgres_url)."""
    import tools.config as config_module

    data = json.loads(mock_config.path.read_text())
    data.setdefault("memory", {}).update(sections)
    mock_config.path.write_text(json.dumps(data))
    config_module.clear_config_cache()


def _seed_filler(mock_config):
    """One real vault doc so the non-empty-collection guard passes."""
    from tools.embedding import get_embedding_service

    emb = get_embedding_service()
    mock_config.db.upsert_vault(
        "vault::notes/filler.md", "filler", emb.encode("filler"),
        parent_file="notes/filler.md", directory="notes", vault_type="note",
    )


def _ann_vault_row(doc_id, parent_file, distance, document, importance=0.5):
    return {
        "id": doc_id, "document": document, "metadata": {},
        "parent_file": parent_file, "directory": "notes", "vault_type": "note",
        "title": "T", "chunk_index": 0, "chunk_total": 1, "chunk_heading": "",
        "importance_score": importance, "distance": distance, "_schema": "obsidian",
    }


def _lex_vault_row(doc_id, parent_file, similarity, document, importance=0.5):
    return {
        "id": doc_id, "document": document, "metadata": {},
        "parent_file": parent_file, "directory": "notes", "vault_type": "note",
        "title": "T", "chunk_index": 0, "chunk_total": 1, "chunk_heading": "",
        "importance_score": importance, "similarity": similarity,
        "distance": 1.0 - similarity, "_schema": "obsidian",
    }


class _FakeHostClient:
    def __init__(self, logit_fn):
        self._logit_fn = logit_fn

    def rerank(self, query, documents):
        return [self._logit_fn(doc) for doc in documents]


def _patch_lexical(monkeypatch, *, terms, per_schema):
    """Patch the lexical module: fixed terms + canned rows per schema."""
    import tools.lexical as lexical

    monkeypatch.setattr(lexical, "extract_query_lexemes", lambda conn, q: list(terms))
    monkeypatch.setattr(
        lexical, "informative_terms", lambda conn, lexemes, **kw: list(terms)
    )
    monkeypatch.setattr(
        lexical, "lexical_candidates",
        lambda conn, t, *, schema, limit, query_embedding, user=None: list(
            per_schema.get(schema, [])
        ),
    )


def _capture_record_event(monkeypatch):
    import tools.retrieval_telemetry as telemetry

    captured = {}

    def fake_record_event(**kwargs):
        captured["candidates"] = kwargs.get("candidates")
        captured["funnel"] = kwargs.get("funnel")
        captured["config_snapshot"] = kwargs.get("config_snapshot")
        return "trace-id"

    monkeypatch.setattr(telemetry, "record_event", fake_record_event)
    return captured


def _enable_host_reranker(mock_config, monkeypatch, logit_fn, *, candidate_count=20):
    import tools.reranking as reranking

    _set_memory(mock_config, reranking={
        "enabled": True, "backend": "host", "model": "test-bge",
        "alpha": 0.7, "max_latency_ms": 5000, "candidate_count": candidate_count,
    })
    monkeypatch.setattr(
        reranking, "_get_host_client", lambda config: _FakeHostClient(logit_fn)
    )


# ── Union dedup + channel tagging ─────────────────────────────────────


def test_union_tags_channels_semantic_lexical_and_both(mock_config, monkeypatch):
    from tools.query import semantic_context

    _seed_filler(mock_config)
    captured = _capture_record_event(monkeypatch)

    ann_rows = [
        _ann_vault_row("vault::notes/x.md", "notes/x.md", 0.05, "x body"),
        _ann_vault_row("vault::notes/z.md", "notes/z.md", 0.10, "z body"),
    ]
    monkeypatch.setattr("tools.query._cross_schema_search", lambda *a, **k: list(ann_rows))
    _patch_lexical(monkeypatch, terms=["igor"], per_schema={"obsidian": [
        _lex_vault_row("vault::notes/x.md", "notes/x.md", 0.9, "x body"),   # dedup → both
        _lex_vault_row("vault::notes/y.md", "notes/y.md", 0.6, "y body"),   # lexical-only
    ]})

    semantic_context("igor goals", threshold=-1.0)

    by_id = {c.doc_id: c for c in captured["candidates"]}
    assert by_id["vault::notes/x.md"].channel == "both"
    assert by_id["vault::notes/z.md"].channel == "semantic"
    assert by_id["vault::notes/y.md"].channel == "lexical"
    assert captured["funnel"]["lexical_candidates"] == 2
    assert captured["funnel"]["lexical_added"] == 1


# ── Reserved rerank slots ─────────────────────────────────────────────


def test_reserved_slots_admit_low_pre_score_lexical_row(mock_config, monkeypatch):
    """The reserved lexical slot keeps a low-cosine lexical row in the rerank
    batch (where its logit rescues it) WITHOUT evicting any cosine-passing row —
    even under a tiny candidate_count. Recall-additivity: passers are exempt
    from cap competition against cosine-failing rows (finding 0/1)."""
    from tools.query import semantic_context

    _seed_filler(mock_config)
    captured = _capture_record_event(monkeypatch)
    _enable_host_reranker(mock_config, monkeypatch, lambda doc: 5.0, candidate_count=1)

    ann_rows = [
        _ann_vault_row("vault::notes/a.md", "notes/a.md", 0.05, "a body", 0.9),
        _ann_vault_row("vault::notes/b.md", "notes/b.md", 0.08, "b body", 0.9),
    ]
    monkeypatch.setattr("tools.query._cross_schema_search", lambda *a, **k: list(ann_rows))
    _patch_lexical(monkeypatch, terms=["igor"], per_schema={"obsidian": [
        _lex_vault_row("vault::notes/lex.md", "notes/lex.md", 0.3, "lex body"),
    ]})

    result = semantic_context("igor goals", threshold=0.85)

    by_id = {c.doc_id: c for c in captured["candidates"]}
    lex = by_id["vault::notes/lex.md"]
    assert lex.channel == "lexical"
    # Proof it entered the rerank batch (reserved) despite lowest pre-score.
    assert lex.raw_bge_logit is not None
    assert lex.returned is True
    # Recall-additivity: no cosine-PASSING row is ever candidate_cap'd by the
    # low-cosine lexical failer — both ANN passers survive to output.
    capped_passers = [
        c for c in captured["candidates"]
        if c.terminal_reason == "candidate_cap" and c.channel == "semantic"
    ]
    assert capped_passers == []
    match_ids = [m["id"] for m in result["matches"]]
    assert "notes/a.md" in match_ids and "notes/b.md" in match_ids
    assert captured["funnel"]["logit_rescued"] >= 1
    assert "notes/lex.md" in match_ids


# ── Recall-additive OR-gate semantics ─────────────────────────────────


def test_or_gate_keeps_cosine_and_rescues_and_rejects(mock_config, monkeypatch):
    """Phase-1 scope: only LEXICAL cosine-failers reach the logit gate. A
    cosine-passer is kept via the cosine clause; a lexical failer with a high
    logit is rescued; a lexical failer with a low logit is rejected."""
    from tools.query import semantic_context

    _seed_filler(mock_config)
    captured = _capture_record_event(monkeypatch)

    def logit_fn(doc):
        if "RESCUE" in doc:
            return 5.0     # >= -4 → rescued
        if "REJECT" in doc:
            return -10.0   # <  -4 → rejected
        return 5.0         # cosine-passer, logit irrelevant

    _enable_host_reranker(mock_config, monkeypatch, logit_fn)

    ann_rows = [
        _ann_vault_row("vault::notes/pass.md", "notes/pass.md", 0.05, "PASS body"),
    ]
    monkeypatch.setattr("tools.query._cross_schema_search", lambda *a, **k: list(ann_rows))
    # rescue/reject arrive via the LEXICAL channel (cosine 0.5 < 0.85 threshold).
    _patch_lexical(monkeypatch, terms=["igor"], per_schema={"obsidian": [
        _lex_vault_row("vault::notes/rescue.md", "notes/rescue.md", 0.5, "RESCUE body"),
        _lex_vault_row("vault::notes/reject.md", "notes/reject.md", 0.5, "REJECT body"),
    ]})

    result = semantic_context("igor goals", threshold=0.85)
    ids = [m["id"] for m in result["matches"]]
    by_id = {c.doc_id: c for c in captured["candidates"]}

    assert "notes/pass.md" in ids                 # cosine clause
    assert "notes/rescue.md" in ids               # logit clause (lexical)
    assert "notes/reject.md" not in ids           # failed both
    assert by_id["vault::notes/reject.md"].terminal_reason == "logit_rejected"
    assert captured["funnel"]["logit_rescued"] >= 1
    assert captured["funnel"]["logit_rejected"] >= 1
    assert captured["config_snapshot"]["bge_logit_threshold"] == -4.0


def test_cosine_failing_semantic_row_dropped_not_gated(mock_config, monkeypatch):
    """Phase-1 scope: a cosine-failing SEMANTIC (ANN) row is dropped at the
    threshold and never reaches the logit gate, even with a high logit — only
    lexical failers get the rescue path."""
    from tools.query import semantic_context

    _seed_filler(mock_config)
    captured = _capture_record_event(monkeypatch)
    _enable_host_reranker(mock_config, monkeypatch, lambda doc: 50.0)

    ann_rows = [
        _ann_vault_row("vault::notes/pass.md", "notes/pass.md", 0.05, "pass body"),
        _ann_vault_row("vault::notes/semfail.md", "notes/semfail.md", 0.5, "sem body"),
    ]
    monkeypatch.setattr("tools.query._cross_schema_search", lambda *a, **k: list(ann_rows))
    _patch_lexical(monkeypatch, terms=["igor"], per_schema={})

    result = semantic_context("igor goals", threshold=0.85)
    ids = [m["id"] for m in result["matches"]]
    by_id = {c.doc_id: c for c in captured["candidates"]}

    assert "notes/pass.md" in ids
    assert "notes/semfail.md" not in ids
    # Dropped at the threshold (cosine_rejected), never logit_rejected/rescued.
    assert by_id["vault::notes/semfail.md"].terminal_reason == "cosine_rejected"
    assert by_id["vault::notes/semfail.md"].raw_bge_logit is None
    assert captured["funnel"]["logit_rescued"] == 0


def test_or_gate_drops_unjudged_when_reranker_falls_back(mock_config, monkeypatch):
    """Reranker fallback (no logits) drops a lexical failer that only qualified
    via the logit clause — behavior degrades to exactly today's cosine-only
    path (counted as logit_unjudged_dropped)."""
    from tools.query import semantic_context

    _seed_filler(mock_config)
    captured = _capture_record_event(monkeypatch)

    def boom(doc):
        raise RuntimeError("host reranker down")

    _enable_host_reranker(mock_config, monkeypatch, boom)

    ann_rows = [
        _ann_vault_row("vault::notes/pass.md", "notes/pass.md", 0.05, "pass body"),
    ]
    monkeypatch.setattr("tools.query._cross_schema_search", lambda *a, **k: list(ann_rows))
    _patch_lexical(monkeypatch, terms=["igor"], per_schema={"obsidian": [
        _lex_vault_row("vault::notes/fail.md", "notes/fail.md", 0.5, "fail body"),
    ]})

    result = semantic_context("igor goals", threshold=0.85)
    ids = [m["id"] for m in result["matches"]]
    by_id = {c.doc_id: c for c in captured["candidates"]}

    assert "notes/pass.md" in ids
    assert "notes/fail.md" not in ids
    assert by_id["vault::notes/fail.md"].terminal_reason == "cosine_rejected"
    assert captured["funnel"]["logit_unjudged_dropped"] >= 1
    assert captured["funnel"]["logit_rescued"] == 0


def test_reranking_disabled_degrades_to_cosine_only(mock_config, monkeypatch):
    """With reranking off, cosine-failing rows are dropped in the raw loop
    exactly as today — the logit clause is inert and nothing is rescued."""
    from tools.query import semantic_context

    _seed_filler(mock_config)
    captured = _capture_record_event(monkeypatch)

    ann_rows = [
        _ann_vault_row("vault::notes/pass.md", "notes/pass.md", 0.05, "pass body"),
        _ann_vault_row("vault::notes/fail.md", "notes/fail.md", 0.5, "fail body"),
    ]
    monkeypatch.setattr("tools.query._cross_schema_search", lambda *a, **k: list(ann_rows))
    _patch_lexical(monkeypatch, terms=["igor"], per_schema={})

    result = semantic_context("igor goals", threshold=0.85)
    ids = [m["id"] for m in result["matches"]]
    by_id = {c.doc_id: c for c in captured["candidates"]}

    assert "notes/pass.md" in ids
    assert "notes/fail.md" not in ids
    assert by_id["vault::notes/fail.md"].terminal_reason == "cosine_rejected"
    assert captured["funnel"]["logit_rescued"] == 0
    assert captured["funnel"]["logit_rejected"] == 0
    assert captured["funnel"]["logit_unjudged_dropped"] == 0


# ── Funnel accounting ─────────────────────────────────────────────────


def test_funnel_lexical_counts_sum(mock_config, monkeypatch):
    from tools.query import semantic_context

    _seed_filler(mock_config)
    captured = _capture_record_event(monkeypatch)

    ann_rows = [_ann_vault_row("vault::notes/x.md", "notes/x.md", 0.05, "x body")]
    monkeypatch.setattr("tools.query._cross_schema_search", lambda *a, **k: list(ann_rows))
    _patch_lexical(monkeypatch, terms=["igor"], per_schema={"obsidian": [
        _lex_vault_row("vault::notes/x.md", "notes/x.md", 0.9, "x body"),   # dedup → both
        _lex_vault_row("vault::notes/y.md", "notes/y.md", 0.6, "y body"),   # added
        _lex_vault_row("vault::notes/w.md", "notes/w.md", 0.55, "w body"),  # added
    ]})

    semantic_context("igor goals", threshold=-1.0)
    funnel = captured["funnel"]
    # 3 fetched; 1 deduped into ANN ('both'); 2 appended.
    assert funnel["lexical_candidates"] == 3
    assert funnel["lexical_added"] == 2


def test_ann_unique_excludes_lexical_added(mock_config, monkeypatch):
    """funnel.ann_unique counts ONLY ANN rows — never the lexical-only rows the
    union appends (finding 2/11)."""
    from tools.query import semantic_context

    _seed_filler(mock_config)
    captured = _capture_record_event(monkeypatch)

    ann_rows = [
        _ann_vault_row("vault::notes/a.md", "notes/a.md", 0.05, "a body"),
        _ann_vault_row("vault::notes/b.md", "notes/b.md", 0.08, "b body"),
    ]
    monkeypatch.setattr("tools.query._cross_schema_search", lambda *a, **k: list(ann_rows))
    _patch_lexical(monkeypatch, terms=["igor"], per_schema={"obsidian": [
        _lex_vault_row("vault::notes/y.md", "notes/y.md", 0.6, "y body"),
        _lex_vault_row("vault::notes/z.md", "notes/z.md", 0.55, "z body"),
    ]})

    semantic_context("igor goals", threshold=-1.0)
    funnel = captured["funnel"]
    assert funnel["ann_unique"] == 2           # NOT 4 (2 ANN + 2 lexical)
    assert funnel["lexical_added"] == 2


# ── Recall-additivity invariant (findings 0 / 1) ──────────────────────


def test_recall_additivity_enabled_superset_of_disabled(mock_config, monkeypatch):
    """STRICT RECALL-ADDITIVITY: the set injected with reranking ENABLED is a
    superset of the set injected with it DISABLED — enabling the reranker only
    ADDS the logit-rescued lexical failer, never removes a cosine-passer."""
    from tools.query import semantic_context

    _seed_filler(mock_config)

    ann_rows = [
        _ann_vault_row("vault::notes/p1.md", "notes/p1.md", 0.05, "p1 body", 0.9),
        _ann_vault_row("vault::notes/p2.md", "notes/p2.md", 0.10, "p2 body", 0.9),
    ]
    monkeypatch.setattr("tools.query._cross_schema_search", lambda *a, **k: list(ann_rows))
    _patch_lexical(monkeypatch, terms=["igor"], per_schema={"obsidian": [
        _lex_vault_row("vault::notes/lex.md", "notes/lex.md", 0.3, "RESCUE lex body"),
    ]})

    # Reranking DISABLED — cosine only. The lexical failer is dropped.
    _set_memory(mock_config, reranking={"enabled": False})
    disabled = semantic_context("igor goals", threshold=0.85, max_results=3, budget=8000)
    disabled_ids = {m["id"] for m in disabled["matches"]}

    # Reranking ENABLED with a rescuing reranker — same fixture.
    _enable_host_reranker(mock_config, monkeypatch, lambda doc: 5.0)
    enabled = semantic_context("igor goals", threshold=0.85, max_results=3, budget=8000)
    enabled_ids = {m["id"] for m in enabled["matches"]}

    assert disabled_ids <= enabled_ids                 # superset invariant
    assert {"notes/p1.md", "notes/p2.md"} <= disabled_ids
    assert "notes/lex.md" in enabled_ids               # rescue ADDED
    assert "notes/lex.md" not in disabled_ids


def test_passers_keep_seats_under_tight_budget(mock_config, monkeypatch):
    """Under a tight budget the cosine-passers keep their seats; the rescued
    lexical row only takes leftover budget — it never displaces a passer."""
    from tools.query import semantic_context

    _seed_filler(mock_config)
    captured = _capture_record_event(monkeypatch)

    ann_rows = [
        _ann_vault_row("vault::notes/p1.md", "notes/p1.md", 0.05, "p1 body", 0.9),
        _ann_vault_row("vault::notes/p2.md", "notes/p2.md", 0.10, "p2 body", 0.9),
    ]
    monkeypatch.setattr("tools.query._cross_schema_search", lambda *a, **k: list(ann_rows))
    _patch_lexical(monkeypatch, terms=["igor"], per_schema={"obsidian": [
        _lex_vault_row("vault::notes/lex.md", "notes/lex.md", 0.3, "RESCUE lex body"),
    ]})
    _enable_host_reranker(mock_config, monkeypatch, lambda doc: 5.0)

    # Budget fits only two vault references (120 chars each); passers first.
    result = semantic_context("igor goals", threshold=0.85, max_results=20, budget=250)
    ids = [m["id"] for m in result["matches"]]

    assert "notes/p1.md" in ids and "notes/p2.md" in ids   # passers keep seats
    assert "notes/lex.md" not in ids                        # rescue gets leftover
    by_id = {c.doc_id: c for c in captured["candidates"]}
    assert by_id["vault::notes/lex.md"].terminal_reason == "budget_rejected"
    # Rescue still counted at the gate even though budget dropped it (finding 3).
    assert captured["funnel"]["logit_rescued"] >= 1
    assert captured["funnel"]["logit_rescued_injected"] == 0


# ── User isolation + metadata filter (findings 6 / 14) ────────────────


def test_query_vault_skips_lexical_when_filter_present(mock_config, monkeypatch):
    """A metadata filter skips the lexical union entirely (Phase-1 limitation)
    rather than returning rows that violate the filter."""
    from tools.query import query_vault

    _seed_filler(mock_config)
    captured = _capture_record_event(monkeypatch)

    ann_rows = [_ann_vault_row("vault::notes/x.md", "notes/x.md", 0.05, "x body")]
    monkeypatch.setattr("tools.query._cross_schema_search", lambda *a, **k: list(ann_rows))
    _patch_lexical(monkeypatch, terms=["igor"], per_schema={"obsidian": [
        _lex_vault_row("vault::notes/y.md", "notes/y.md", 0.6, "y body"),
    ]})

    # With a filter → lexical skipped.
    filtered = query_vault("igor", n_results=10, filter={"type": "note"})
    assert captured["funnel"]["lexical_added"] == 0
    assert "vault::notes/y.md" not in [r["id"] for r in filtered["results"]]

    # Without a filter → lexical runs (control).
    unfiltered = query_vault("igor", n_results=10)
    assert captured["funnel"]["lexical_added"] == 1
    assert "vault::notes/y.md" in [r["id"] for r in unfiltered["results"]]


def test_query_vault_threads_user_into_lexical(mock_config, monkeypatch):
    """query_vault passes its user scope through to the lexical channel so the
    lexical SQL applies the same per-user isolation as the ANN path."""
    from tools.query import query_vault
    import tools.lexical as lexical

    _seed_filler(mock_config)
    _capture_record_event(monkeypatch)

    ann_rows = [_ann_vault_row("vault::notes/x.md", "notes/x.md", 0.05, "x body")]
    monkeypatch.setattr("tools.query._cross_schema_search", lambda *a, **k: list(ann_rows))

    seen_users = []
    monkeypatch.setattr(lexical, "extract_query_lexemes", lambda conn, q: ["igor"])
    monkeypatch.setattr(
        lexical, "informative_terms", lambda conn, lexemes, **kw: ["igor"]
    )

    def _capture_user(conn, t, *, schema, limit, query_embedding, user=None):
        seen_users.append(user)
        return []

    monkeypatch.setattr(lexical, "lexical_candidates", _capture_user)

    query_vault("igor", n_results=10, user="alice")
    assert seen_users and all(u == "alice" for u in seen_users)


def test_hybrid_never_removes_what_the_reranked_path_injects(mock_config, monkeypatch):
    """THE deliverable invariant (V1-V6 triage): with reranking ON in BOTH runs,
    enabling the lexical channel never removes an injection the non-hybrid
    reranked path makes — even when max_results binds, the reranker reorders
    passers, and a rescued lexical failer shares a parent_file with a passer
    (parent-dedup passer-priority) or nearly duplicates one (semantic-dedup
    passer-priority)."""
    from tools.query import semantic_context

    _seed_filler(mock_config)

    ann_rows = [
        _ann_vault_row("vault::notes/p1.md#chunk-0", "notes/p1.md", 0.149, "p1 body", 0.2),
        _ann_vault_row("vault::notes/p2.md#chunk-0", "notes/p2.md", 0.10, "p2 body", 0.5),
        _ann_vault_row("vault::notes/p3.md#chunk-0", "notes/p3.md", 0.05, "p3 body", 0.9),
    ]
    monkeypatch.setattr("tools.query._cross_schema_search", lambda *a, **k: list(ann_rows))

    # Reordering reranker: prefers p1's text, hates nothing outright — passers
    # get shuffled by blend, exactly the pre-existing reranker behavior.
    def logits(doc):
        return {"p1 body": 6.0, "p2 body": -1.0, "p3 body": 2.0}.get(doc, 5.0)

    lexical_rows = [
        # Shares parent_file with passer p1, higher importance → higher
        # pre-score than p1's chunk. Without passer-priority parent dedup this
        # failer steals the (notes/p1.md, obsidian) key.
        _lex_vault_row("vault::notes/p1.md#chunk-7", "notes/p1.md", 0.84, "p1 deep chunk", 1.0),
        # Independent rescue candidate.
        _lex_vault_row("vault::notes/lex.md", "notes/lex.md", 0.30, "RESCUE lex body", 0.5),
    ]

    def run(lexical_enabled):
        _set_memory(mock_config, lexical={"enabled": lexical_enabled,
                                          "candidate_limit": 30, "lexical_rerank_slots": 10})
        _enable_host_reranker(mock_config, monkeypatch, logits)
        if lexical_enabled:
            _patch_lexical(monkeypatch, terms=["igor"], per_schema={"obsidian": lexical_rows})
        else:
            _patch_lexical(monkeypatch, terms=[], per_schema={})
        # Binding cut: 3 passers, only 2 seats.
        result = semantic_context("igor goals", threshold=0.85, max_results=2, budget=8000)
        return {m["id"] for m in result["matches"]}

    baseline = run(lexical_enabled=False)
    hybrid = run(lexical_enabled=True)

    assert len(baseline) == 2  # the cut binds
    assert baseline <= hybrid, f"hybrid removed {baseline - hybrid}"


def test_reserved_slots_honor_lexical_native_rank_not_cosine(mock_config, monkeypatch):
    """Regression (live Igor case, stage 2): reserved rerank seats must go by
    the lexical channel's rarity order, not cosine pre-score — otherwise
    common-term matches with higher cosine take every seat and the df=1 term's
    rows never reach BGE."""
    from tools.query import semantic_context

    _seed_filler(mock_config)
    captured = _capture_record_event(monkeypatch)

    ann_rows = [_ann_vault_row("vault::notes/p1.md", "notes/p1.md", 0.05, "p1 body", 0.5)]
    monkeypatch.setattr("tools.query._cross_schema_search", lambda *a, **k: list(ann_rows))
    # lexical_candidates returns rarest-term rows FIRST: the low-cosine rare
    # target ahead of a higher-cosine common-term row. Only ONE reserved seat.
    _patch_lexical(monkeypatch, terms=["igor", "goal"], per_schema={"obsidian": [
        _lex_vault_row("vault::notes/rare.md", "notes/rare.md", 0.60, "rare target body"),
        _lex_vault_row("vault::notes/common.md", "notes/common.md", 0.83, "common goalish body"),
    ]})
    _set_memory(mock_config, lexical={"enabled": True, "candidate_limit": 30,
                                      "lexical_rerank_slots": 1})
    _enable_host_reranker(mock_config, monkeypatch, lambda doc: 3.0)

    result = semantic_context("igor goals", threshold=0.85, max_results=5, budget=8000)
    ids = {m["id"] for m in result["matches"]}

    assert "notes/rare.md" in ids, "rarest-term row must get the reserved seat"
    by_id = {c.doc_id: c for c in captured["candidates"]}
    assert by_id["vault::notes/common.md"].terminal_reason == "candidate_cap"
