"""Real PostgreSQL contracts for the lexical recall channel (Phase 1).

Covers: DDL idempotency + tsv/GIN/channel existence, lexical_candidates
finding a rare-term doc with a true cosine, and THE ACCEPTANCE SHAPE — a doc
whose distinctive term lives only in the title, body semantically unrelated to
the query, ranked outside the ANN window, is rescued via the augmented-BGE
logit gate and injected with channel='lexical' (persisted on the candidate
row) while funnel.logit_rescued >= 1.
"""

import json
import math
import os

import psycopg
import pytest

pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        not os.environ.get("E2E_POSTGRES_URL"),
        reason="E2E_POSTGRES_URL not set",
    ),
]


def _vec_literal(vec):
    return "[" + ",".join(f"{x:.8f}" for x in vec) + "]"


def _orthonormal_at_cosine(v, cosine):
    """Return a unit vector w with cos(v, w) == cosine (Gram-Schmidt on e0)."""
    e0 = [1.0] + [0.0] * (len(v) - 1)
    dot = sum(a * b for a, b in zip(e0, v))
    u = [a - dot * b for a, b in zip(e0, v)]
    norm = math.sqrt(sum(x * x for x in u))
    u = [x / norm for x in u]
    return [cosine * a + math.sqrt(1 - cosine * cosine) * b for a, b in zip(v, u)]


def _enable_host_reranker(e2e_config, candidate_count=20):
    import jarvis_common.config as ccm
    import tools.config as config_module

    cfgfile = e2e_config["config_dir"] / "config.json"
    data = json.loads(cfgfile.read_text())
    data.setdefault("memory", {})["reranking"] = {
        "enabled": True, "backend": "host", "model": "test-bge",
        "alpha": 0.7, "max_latency_ms": 5000, "candidate_count": candidate_count,
    }
    # Persist every candidate row (default caps rejected rows at 100) so the
    # decoy's logit_rejected row is recorded even behind 100+ ANN fillers.
    data.setdefault("memory", {})["retrieval_telemetry"] = {
        "candidate_detail_limit": 300,
    }
    cfgfile.write_text(json.dumps(data))
    config_module._config_cache = None
    ccm._config_cache = None


# ── DDL ───────────────────────────────────────────────────────────────


def test_lexical_ddl_is_idempotent_and_objects_exist(e2e_config):
    from tools.schema import ensure_schema

    ensure_schema()
    ensure_schema()  # twice — must not error

    with psycopg.connect(e2e_config["db_url"]) as conn:
        obsidian_tsv = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema='obsidian'"
            " AND table_name='documents' AND column_name='tsv'"
        ).fetchone()
        local_tsv = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema='local'"
            " AND table_name='memories' AND column_name='tsv'"
        ).fetchone()
        channel_col = conn.execute(
            "SELECT 1 FROM information_schema.columns WHERE table_schema='local'"
            " AND table_name='retrieval_candidates' AND column_name='channel'"
        ).fetchone()
        gin = conn.execute(
            "SELECT indexname FROM pg_indexes "
            "WHERE indexname IN ('idx_obsidian_tsv', 'idx_local_tsv')"
        ).fetchall()

    assert obsidian_tsv and local_tsv and channel_col
    assert {row[0] for row in gin} == {"idx_obsidian_tsv", "idx_local_tsv"}


# ── lexical_candidates ────────────────────────────────────────────────


def test_lexical_candidates_finds_rare_term_and_returns_real_cosine(e2e_config):
    from tools.embedding import get_embedding_service
    from tools.lexical import (
        extract_query_lexemes, informative_terms, lexical_candidates,
    )
    from tools.schema import _get_pool

    query = "notes about zzqxrare"
    v = get_embedding_service().encode(query)

    pool = _get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO obsidian.documents "
                "(id, document, embedding, parent_file, directory, vault_type, title) "
                "VALUES (%s, %s, %s::halfvec, %s, 'notes', 'note', %s)",
                ("vault::notes/rare.md", "body text unrelated", _vec_literal(v),
                 "notes/rare.md", "Zzqxrare topic"),
            )
            cur.execute(
                "INSERT INTO obsidian.documents "
                "(id, document, embedding, parent_file, directory, vault_type, title) "
                "VALUES (%s, %s, %s::halfvec, %s, 'notes', 'note', %s)",
                ("vault::notes/common.md", "common body", _vec_literal(v),
                 "notes/common.md", "Common"),
            )
        conn.commit()

        lexemes = extract_query_lexemes(conn, query)
        terms = informative_terms(conn, lexemes, max_df_ratio=0.6, max_terms=8)
        assert any(t.startswith("zzqxrar") for t in terms), terms

        rows = lexical_candidates(
            conn, terms, schema="obsidian", limit=30, query_embedding=v
        )

    ids = [row["id"] for row in rows]
    assert "vault::notes/rare.md" in ids
    rare = next(row for row in rows if row["id"] == "vault::notes/rare.md")
    # embedding == query vector → true raw cosine ~1.0 (halfvec rounding aside).
    assert 0.9 <= rare["similarity"] <= 1.01
    assert abs(rare["distance"] - (1.0 - rare["similarity"])) < 1e-9


# ── ACCEPTANCE SHAPE ──────────────────────────────────────────────────


def test_lexical_channel_rescues_out_of_window_doc(e2e_config, monkeypatch):
    """Distinctive term only in the title; body unrelated; ranked outside the
    ANN window. The lexical channel surfaces it, the reserved rerank slot keeps
    it, and a high BGE logit rescues it past the recall-additive gate."""
    import tools.reranking as reranking
    from tools.embedding import get_embedding_service
    from tools.query import semantic_context
    from tools.schema import _get_pool

    query = "what about zzqxrare specifically"
    v = get_embedding_service().encode(query)
    target_vec = _orthonormal_at_cosine(v, 0.30)  # below the 0.85 threshold
    filler_vec = _orthonormal_at_cosine(v, 0.60)   # above target, below threshold

    pool = _get_pool()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            # 105 fillers at cosine 0.60 — above the target's 0.30 (so the target
            # is pushed past the ANN fetch window, top 100) but BELOW the 0.85
            # threshold, so they are cosine-rejected rather than consuming the
            # cosine-passers-first injection slots. (Perfect-match fillers would
            # rightly claim every max_results seat under recall-additivity,
            # leaving no room for a cosine-failing lexical rescue.)
            for i in range(105):
                cur.execute(
                    "INSERT INTO obsidian.documents "
                    "(id, document, embedding, parent_file, directory, vault_type, title) "
                    "VALUES (%s, %s, %s::halfvec, %s, 'notes', 'note', %s)",
                    (f"vault::notes/filler{i}.md", f"common filler prose number {i}",
                     _vec_literal(filler_vec), f"notes/filler{i}.md", f"Filler {i}"),
                )
            # Target: distinctive term ONLY in the title; body unrelated to the
            # query. MARKERTARGET (body) is just the reranker's hook.
            cur.execute(
                "INSERT INTO obsidian.documents "
                "(id, document, embedding, parent_file, directory, vault_type, title) "
                "VALUES (%s, %s, %s::halfvec, %s, 'notes', 'note', %s)",
                ("vault::notes/target.md",
                 "MARKERTARGET completely unrelated prose about gardening tomatoes",
                 _vec_literal(target_vec), "notes/target.md", "Zzqxrare Mandate"),
            )
            # DECOY: also carries the rare title term (so it is surfaced by the
            # lexical channel too) and also fails cosine — but its BGE logit is
            # BELOW bge_logit_threshold, so the gate MUST reject it. This makes a
            # broken/loosened threshold comparison (rescuing every reranked
            # cosine-failer) fail the acceptance suite.
            cur.execute(
                "INSERT INTO obsidian.documents "
                "(id, document, embedding, parent_file, directory, vault_type, title) "
                "VALUES (%s, %s, %s::halfvec, %s, 'notes', 'note', %s)",
                ("vault::notes/decoy.md",
                 "DECOYMARK utterly irrelevant filler about bicycle maintenance",
                 _vec_literal(target_vec), "notes/decoy.md", "Zzqxrare Sidebar"),
            )
        conn.commit()

    _enable_host_reranker(e2e_config)

    class _FakeHostClient:
        def rerank(self, query_text, documents):
            scores = []
            for doc in documents:
                if "MARKERTARGET" in doc:
                    scores.append(50.0)      # >= -4 → rescued
                elif "DECOYMARK" in doc:
                    scores.append(-10.0)     # <  -4 → MUST be rejected
                else:
                    scores.append(1.0)
            return scores

    monkeypatch.setattr(reranking, "_get_host_client", lambda config: _FakeHostClient())

    result = semantic_context(query, threshold=0.85, max_results=20)

    match_ids = [match["id"] for match in result["matches"]]
    assert "notes/target.md" in match_ids, match_ids
    # The low-logit decoy is rejected by the gate, never injected.
    assert "notes/decoy.md" not in match_ids, match_ids

    # Verify persisted telemetry: funnel counts + channel + terminal_reason.
    with psycopg.connect(e2e_config["db_url"]) as conn:
        event = conn.execute(
            "SELECT id, funnel FROM local.retrieval_events "
            "WHERE purpose = 'context_injection' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        event_id, funnel = event
        assert funnel.get("logit_rescued", 0) >= 1, funnel
        assert funnel.get("logit_rejected", 0) >= 1, funnel
        assert funnel.get("lexical_added", 0) >= 1, funnel

        channel = conn.execute(
            "SELECT channel FROM local.retrieval_candidates "
            "WHERE event_id = %s AND doc_id = %s",
            (event_id, "vault::notes/target.md"),
        ).fetchone()
        assert channel is not None and channel[0] == "lexical", channel

        decoy = conn.execute(
            "SELECT terminal_reason FROM local.retrieval_candidates "
            "WHERE event_id = %s AND doc_id = %s",
            (event_id, "vault::notes/decoy.md"),
        ).fetchone()
        assert decoy is not None and decoy[0] == "logit_rejected", decoy
