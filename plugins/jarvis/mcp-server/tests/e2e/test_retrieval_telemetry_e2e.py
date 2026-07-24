"""Real PostgreSQL contracts for retrieval telemetry."""

import psycopg
import time


def test_retrieval_event_roundtrip_and_no_candidate_body(e2e_config):
    from tools.retrieval_telemetry import CandidateTrace, get_event, get_summary, record_event
    from tools.schema import _get_pool

    _get_pool()  # retrieval paths have already established this in production
    trace_id = record_event(
        purpose="context_injection",
        query="full user prompt for calibration",
        candidates=[CandidateTrace(
            schema_name="local", doc_id="obs::telemetry", vector_rank=1,
            similarity=0.91, terminal_reason="selected", returned=True,
        )],
        funnel={"ann_unique": 1, "budget_selected": 1},
        latency={"total_ms": 3.2}, outcome="results",
        shadow_eligible=False,
    )
    assert trace_id
    event = get_event(trace_id)
    assert event["query_text"] == "full user prompt for calibration"
    assert event["candidates"][0]["doc_id"] == "obs::telemetry"
    assert "document" not in event["candidates"][0]
    assert "content" not in event["candidates"][0]
    summary = get_summary(7)
    assert summary["requests"] == 1
    assert summary["funnel"]["candidates"] == 1
    assert summary["schemas"][0]["schema"] == "local"
    assert "cosine" in summary["histograms"]


def test_feedback_cascades_with_event(e2e_config):
    from tools.retrieval_telemetry import (
        CandidateTrace, put_candidate_feedback, put_event_feedback, record_event,
    )
    from tools.schema import _get_pool

    _get_pool()
    candidate = CandidateTrace(schema_name="local", doc_id="obs::cascade", vector_rank=1)
    trace_id = record_event(
        purpose="explicit_recall", query="cascade", candidates=[candidate],
        funnel={}, latency={}, outcome="results", shadow_eligible=False,
    )
    put_event_feedback(trace_id, {"verdict": "useful", "expected_missing_ids": []})
    put_candidate_feedback(trace_id, candidate.candidate_key, {"verdict": "relevant"})

    with psycopg.connect(e2e_config["db_url"]) as conn:
        conn.execute("DELETE FROM local.retrieval_events WHERE id = %s::uuid", (trace_id,))
        remaining = conn.execute(
            """SELECT
                 (SELECT count(*) FROM local.retrieval_candidates WHERE event_id = %s::uuid),
                 (SELECT count(*) FROM local.retrieval_feedback WHERE event_id = %s::uuid),
                 (SELECT count(*) FROM local.retrieval_candidate_feedback WHERE event_id = %s::uuid)""",
            (trace_id, trace_id, trace_id),
        ).fetchone()
    assert remaining == (0, 0, 0)


def test_schema_initialization_is_idempotent_with_telemetry(e2e_config):
    from tools.schema import ensure_schema

    ensure_schema()
    ensure_schema()


def test_telemetry_write_p95_under_ten_ms(e2e_config, monkeypatch):
    import tools.retrieval_telemetry as telemetry
    from tools.schema import _get_pool

    _get_pool()
    monkeypatch.setattr(telemetry, "_config", lambda: {
        "enabled": True, "retention_days": 30, "store_user_prompts": True,
        "candidate_detail_limit": 100, "shadow": {"enabled": False},
    })
    timings = []
    for index in range(30):
        started = time.perf_counter()
        assert telemetry.record_event(
            purpose="performance_probe", query=f"probe {index}",
            candidates=[telemetry.CandidateTrace(
                schema_name="local", doc_id=f"obs::probe-{index}",
                vector_rank=1, similarity=0.9,
            )],
            funnel={"ann_unique": 1}, latency={"total_ms": 1},
            outcome="results", shadow_eligible=False,
        )
        timings.append((time.perf_counter() - started) * 1000)
    p95 = sorted(timings)[int(len(timings) * 0.95) - 1]
    assert p95 < 10.0, f"telemetry p95 was {p95:.2f}ms"


def test_shadow_job_scores_pending_candidate(e2e_config, monkeypatch):
    import tools.config as config
    import tools.reranking as reranking
    import tools.retrieval_telemetry as telemetry
    from tools.schema import _get_pool

    vector = "[" + ",".join(["0.01"] * 384) + "]"
    with psycopg.connect(e2e_config["db_url"]) as conn:
        conn.execute(
            """INSERT INTO local.memories
               (id, document, embedding, category, scope, source)
               VALUES ('obs::shadow', 'host model shadow document', %s::halfvec,
                       'observation', 'global', 'test')""",
            (vector,),
        )
    shadow_cfg = {
        "enabled": True, "retention_days": 30, "store_user_prompts": True,
        "candidate_detail_limit": 100,
        "shadow": {"enabled": True, "candidate_count": 20, "delay_seconds": 0,
                   "poll_seconds": 0.1, "max_attempts": 3, "max_jobs_per_second": 10},
    }
    rerank_cfg = {"backend": "host", "model": "bge-test", "alpha": 0.7}
    monkeypatch.setattr(telemetry, "_config", lambda: shadow_cfg)
    monkeypatch.setattr(config, "get_reranking_config", lambda: rerank_cfg)
    monkeypatch.setattr(
        reranking, "rerank_raw",
        lambda query, docs, cfg: {"logits": [2.0], "probabilities": [0.880797], "latency_ms": 1.0},
    )
    _get_pool()
    trace_id = telemetry.record_event(
        purpose="context_injection", query="shadow query",
        candidates=[telemetry.CandidateTrace(
            schema_name="local", doc_id="obs::shadow", chunk_index=0,
            vector_rank=1, similarity=0.7, terminal_reason="cosine_rejected",
        )],
        funnel={"ann_unique": 1}, latency={"total_ms": 2}, outcome="empty",
        model_snapshot={"reranker_model": "bge-test"},
    )
    started = time.perf_counter()
    assert telemetry.process_one_shadow_job() is True
    elapsed = time.perf_counter() - started
    event = telemetry.get_event(trace_id)
    assert event["shadow_status"] == "complete"
    assert event["candidates"][0]["raw_bge_logit"] == 2.0
    assert elapsed < 60


def test_event_documents_resolve_bodies_on_demand(e2e_config):
    """Telemetry stores only locators; the UI resolves bodies from source
    tables at inspection time — memories, chunk windows, and remote mirrors
    without a local body."""
    from tools.retrieval_telemetry import CandidateTrace, get_event_documents, record_event
    from tools.schema import _get_pool

    pool = _get_pool()
    with pool.connection() as conn:
        conn.execute(
            """INSERT INTO local.memories (id, document, embedding)
               VALUES ('obs::doc-body', 'the full memory body', %s::halfvec)
               ON CONFLICT (id) DO UPDATE SET document = EXCLUDED.document""",
            ("[" + ",".join(["0"] * 384) + "]",),
        )
        conn.commit()

    local_hit = CandidateTrace(schema_name="local", doc_id="obs::doc-body", vector_rank=1, returned=True)
    remote_miss = CandidateTrace(schema_name="remote_team", doc_id="obs::elsewhere", vector_rank=2)
    trace_id = record_event(
        purpose="context_injection", query="doc bodies", candidates=[local_hit, remote_miss],
        funnel={}, latency={}, outcome="results", shadow_eligible=False,
    )

    docs = get_event_documents(trace_id, preview_chars=10)
    assert docs[local_hit.candidate_key]["found"] is True
    assert docs[local_hit.candidate_key]["text"] == "the full m"
    assert docs[local_hit.candidate_key]["truncated"] is True
    assert docs[local_hit.candidate_key]["size"] == len("the full memory body")
    assert docs[remote_miss.candidate_key]["found"] is False

    full = get_event_documents(trace_id, candidate_key=local_hit.candidate_key)
    assert full[local_hit.candidate_key]["text"] == "the full memory body"
    assert full[local_hit.candidate_key]["truncated"] is False
