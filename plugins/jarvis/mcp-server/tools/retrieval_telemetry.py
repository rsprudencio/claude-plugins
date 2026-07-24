"""Durable, content-safe observability for semantic retrieval.

The live retrieval path calls :func:`record_event` best-effort. Every public
function is fail-open so telemetry can never affect what Jarvis retrieves.
Candidate bodies are deliberately absent from both the schema and API shapes.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from psycopg.types.json import Jsonb

logger = logging.getLogger("jarvis-core")


@dataclass
class CandidateTrace:
    schema_name: str
    doc_id: str
    parent_id: Optional[str] = None
    parent_file: Optional[str] = None
    chunk_index: Optional[int] = None
    query_window_index: int = 0
    vector_rank: Optional[int] = None
    final_rank: Optional[int] = None
    similarity: Optional[float] = None
    pre_score: Optional[float] = None
    raw_bge_logit: Optional[float] = None
    bge_probability: Optional[float] = None
    blended_score: Optional[float] = None
    display_cost: Optional[int] = None
    terminal_reason: Optional[str] = None
    returned: bool = False
    delivered: bool = False
    channel: str = "semantic"
    candidate_key: str = ""

    def __post_init__(self) -> None:
        if not self.candidate_key:
            raw = f"{self.schema_name}\0{self.doc_id}\0{self.query_window_index}"
            self.candidate_key = hashlib.sha256(raw.encode()).hexdigest()[:24]


def _config() -> dict:
    from .config import get_retrieval_telemetry_config

    return get_retrieval_telemetry_config()


def telemetry_enabled() -> bool:
    try:
        return bool(_config().get("enabled", True))
    except Exception:
        return False


def _json(value: Any) -> Jsonb:
    return Jsonb(value or {})


def _query_identity(query: str) -> tuple[str, int]:
    encoded = query.encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest(), len(query)


def _select_candidate_rows(candidates: list, limit: int) -> list[CandidateTrace]:
    """Bound per-event candidate rows without dropping returned candidates.

    ANN overfetch can trace more rows than candidate_detail_limit. A returned
    candidate must always keep its row — delivery marking, shadow scoring, and
    label export all join on it — so returned rows win the cut and the
    remaining budget goes to the best-ranked rejected rows.
    """
    normalized = [
        item if isinstance(item, CandidateTrace) else CandidateTrace(**item)
        for item in candidates
    ]
    if len(normalized) <= limit:
        return normalized
    returned = [c for c in normalized if c.returned]
    # Rows the reranker actually judged (lexical rescues/rejections carry no
    # vector_rank and would otherwise be truncated out of the trace, making
    # gate decisions invisible in the UI).
    scored = [c for c in normalized if not c.returned and c.raw_bge_logit is not None]
    others = [c for c in normalized if not c.returned and c.raw_bge_logit is None]
    return (returned + scored + others)[:limit]


def record_event(
    *,
    purpose: str,
    query: str,
    candidates: list[CandidateTrace | dict],
    funnel: dict,
    latency: dict,
    outcome: str,
    pipeline: str = "semantic",
    user_name: Optional[str] = None,
    user_facing: bool = True,
    query_ref: Optional[str] = None,
    query_window_count: int = 1,
    model_snapshot: Optional[dict] = None,
    config_snapshot: Optional[dict] = None,
    status: str = "complete",
    shadow_eligible: bool = True,
) -> Optional[str]:
    """Persist one retrieval event and its candidate score trail.

    Returns the trace UUID, or ``None`` when disabled/unavailable. No document
    body is accepted by this API, making accidental body persistence harder.
    """
    if not telemetry_enabled():
        return None
    try:
        from . import schema as schema_module

        # Retrieval already owns a live pool. Do not create/wait for a new one
        # merely to write telemetry during startup, teardown, or degradation.
        if schema_module._pool is None:
            return None

        cfg = _config()
        retention = max(1, int(cfg.get("retention_days", 30)))
        limit = max(0, min(1000, int(cfg.get("candidate_detail_limit", 100))))
        shadow_cfg = cfg.get("shadow") or {}
        shadow_requested = shadow_eligible and shadow_cfg.get("enabled", True) and candidates
        if shadow_requested:
            try:
                from .config import get_reranking_config

                shadow_status = "pending" if get_reranking_config().get("backend") == "host" else "skipped"
            except Exception:
                shadow_status = "skipped"
        else:
            shadow_status = "disabled"
        event_id = str(uuid.uuid4())
        query_sha, query_length = _query_identity(query)
        store_prompt = user_facing and bool(cfg.get("store_user_prompts", True))
        expires_at = datetime.now(timezone.utc) + timedelta(days=retention)
        normalized = _select_candidate_rows(candidates, limit)

        pool = schema_module._pool
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """INSERT INTO local.retrieval_events
                       (id, expires_at, user_name, purpose, pipeline, status, outcome,
                        query_text, query_sha256, query_ref, query_length,
                        query_window_count, model_snapshot, config_snapshot,
                        funnel, latency, shadow_status)
                       VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                               %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        event_id, expires_at, user_name, purpose, pipeline, status,
                        outcome, query if store_prompt else None, query_sha, query_ref,
                        query_length, query_window_count, _json(model_snapshot),
                        _json(config_snapshot), _json(funnel), _json(latency),
                        shadow_status,
                    ),
                )
                if normalized:
                    cur.executemany(
                        """INSERT INTO local.retrieval_candidates
                           (event_id, candidate_key, schema_name, doc_id, parent_id,
                            parent_file, chunk_index, query_window_index, vector_rank,
                            final_rank, similarity, pre_score, raw_bge_logit,
                            bge_probability, blended_score, display_cost,
                            terminal_reason, returned, delivered, channel)
                           VALUES (%s::uuid, %s, %s, %s, %s, %s, %s, %s, %s,
                                   %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                        [
                            (
                                event_id, c.candidate_key, c.schema_name, c.doc_id,
                                c.parent_id, c.parent_file, c.chunk_index,
                                c.query_window_index, c.vector_rank, c.final_rank,
                                c.similarity, c.pre_score, c.raw_bge_logit,
                                c.bge_probability, c.blended_score, c.display_cost,
                                c.terminal_reason, c.returned, c.delivered, c.channel,
                            )
                            for c in normalized
                        ],
                    )
            conn.commit()
        return event_id
    except Exception as exc:
        logger.debug("Retrieval telemetry write skipped: %s", exc)
        return None


def acknowledge_delivery(trace_id: str, payload: dict) -> bool:
    """Record what the hook actually emitted after session-level dedup."""
    try:
        from .schema import _get_pool

        delivered = [str(key) for key in payload.get("delivered_candidate_keys", [])]
        safe_payload = {
            "status": str(payload.get("status", "complete")),
            "returned_count": int(payload.get("returned_count", 0)),
            "delivered_count": int(payload.get("delivered_count", len(delivered))),
            "suppressed_count": int(payload.get("suppressed_count", 0)),
            "output_chars": int(payload.get("output_chars", 0)),
            "acknowledged_at": datetime.now(timezone.utc).isoformat(),
        }
        pool = _get_pool()
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE local.retrieval_events SET delivery = %s WHERE id = %s::uuid",
                    (_json(safe_payload), trace_id),
                )
                event_updated = cur.rowcount
                if delivered:
                    cur.execute(
                        """UPDATE local.retrieval_candidates SET delivered = true
                           WHERE event_id = %s::uuid AND candidate_key = ANY(%s)""",
                        (trace_id, delivered),
                    )
            conn.commit()
        return event_updated > 0
    except Exception as exc:
        logger.debug("Retrieval delivery acknowledgement skipped: %s", exc)
        return False


def get_summary(days: int = 7) -> dict:
    """Return compact aggregate telemetry for health/UI consumers."""
    from .schema import execute_query

    days = max(1, min(int(days), 90))
    row = execute_query(
        """SELECT count(*) AS requests,
                  count(*) FILTER (WHERE outcome = 'empty') AS zero_results,
                  count(*) FILTER (WHERE shadow_status = 'pending') AS shadow_pending,
                  count(*) FILTER (WHERE shadow_status = 'failed') AS shadow_failed,
                  percentile_cont(0.5) WITHIN GROUP
                    (ORDER BY NULLIF(latency->>'total_ms','')::double precision) AS p50_ms,
                  percentile_cont(0.95) WITHIN GROUP
                    (ORDER BY NULLIF(latency->>'total_ms','')::double precision) AS p95_ms
           FROM local.retrieval_events
           WHERE created_at >= now() - (%s * interval '1 day')""",
        (days,), fetch="one",
    ) or {}
    purposes = execute_query(
        """SELECT purpose, count(*) AS requests,
                  count(*) FILTER (WHERE outcome = 'empty') AS zero_results
           FROM local.retrieval_events
           WHERE created_at >= now() - (%s * interval '1 day')
           GROUP BY purpose ORDER BY requests DESC""",
        (days,),
    )
    candidate_totals = execute_query(
        """SELECT count(*) AS candidates,
                  count(*) FILTER (WHERE terminal_reason = 'cosine_rejected') AS cosine_rejected,
                  count(*) FILTER (WHERE terminal_reason = 'logit_rejected') AS logit_rejected,
                  count(*) FILTER (WHERE terminal_reason = 'sensitive') AS sensitive_rejected,
                  count(*) FILTER (WHERE terminal_reason = 'parent_dedup') AS parent_dedup,
                  count(*) FILTER (WHERE terminal_reason = 'semantic_duplicate') AS semantic_duplicates,
                  count(*) FILTER (WHERE terminal_reason = 'candidate_cap') AS candidate_cap,
                  count(*) FILTER (WHERE terminal_reason = 'result_cap') AS result_cap,
                  count(*) FILTER (WHERE terminal_reason = 'budget_rejected') AS budget_rejected,
                  count(*) FILTER (WHERE channel = 'lexical') AS lexical_channel,
                  count(*) FILTER (WHERE channel = 'both') AS both_channel,
                  count(*) FILTER (WHERE returned) AS returned,
                  count(*) FILTER (WHERE delivered) AS delivered,
                  count(*) FILTER (WHERE raw_bge_logit IS NOT NULL) AS bge_scored
           FROM local.retrieval_candidates c
           JOIN local.retrieval_events e ON e.id = c.event_id
           WHERE e.created_at >= now() - (%s * interval '1 day')""",
        (days,), fetch="one",
    ) or {}
    delivery = execute_query(
        """SELECT count(*) FILTER (WHERE delivery ? 'acknowledged_at') AS acknowledged,
                  COALESCE(sum(COALESCE(NULLIF(delivery->>'returned_count','')::integer, 0)), 0) AS returned,
                  COALESCE(sum(COALESCE(NULLIF(delivery->>'delivered_count','')::integer, 0)), 0) AS delivered,
                  COALESCE(sum(COALESCE(NULLIF(delivery->>'suppressed_count','')::integer, 0)), 0) AS suppressed
           FROM local.retrieval_events
           WHERE created_at >= now() - (%s * interval '1 day')""",
        (days,), fetch="one",
    ) or {}
    shadow_rows = execute_query(
        """SELECT shadow_status AS status, count(*) AS count
           FROM local.retrieval_events
           WHERE created_at >= now() - (%s * interval '1 day')
           GROUP BY shadow_status ORDER BY shadow_status""",
        (days,),
    )
    schemas = execute_query(
        """SELECT c.schema_name AS schema, count(*) AS candidates,
                  count(*) FILTER (WHERE c.returned) AS returned
           FROM local.retrieval_candidates c
           JOIN local.retrieval_events e ON e.id = c.event_id
           WHERE e.created_at >= now() - (%s * interval '1 day')
           GROUP BY c.schema_name ORDER BY candidates DESC""",
        (days,),
    )
    recent_contracts = execute_query(
        """SELECT model_snapshot, config_snapshot, funnel
           FROM local.retrieval_events
           WHERE created_at >= now() - (%s * interval '1 day')
           ORDER BY created_at DESC LIMIT 100""",
        (days,),
    )
    models = []
    thresholds = []
    for contract in recent_contracts:
        model = contract.get("model_snapshot") or {}
        threshold = (contract.get("config_snapshot") or {}).get("cosine_threshold")
        if model and model not in models:
            models.append(model)
        if threshold is not None and threshold not in thresholds:
            thresholds.append(threshold)
    cosine_histogram = execute_query(
        """SELECT round((floor(c.similarity * 10) / 10)::numeric, 1)::float AS bucket,
                  count(*) AS count
           FROM local.retrieval_candidates c
           JOIN local.retrieval_events e ON e.id = c.event_id
           WHERE e.created_at >= now() - (%s * interval '1 day')
             AND c.similarity IS NOT NULL
           GROUP BY bucket ORDER BY bucket""",
        (days,),
    )
    logit_histogram = execute_query(
        """SELECT floor(c.raw_bge_logit)::integer AS bucket, count(*) AS count
           FROM local.retrieval_candidates c
           JOIN local.retrieval_events e ON e.id = c.event_id
           WHERE e.created_at >= now() - (%s * interval '1 day')
             AND c.raw_bge_logit IS NOT NULL
           GROUP BY bucket ORDER BY bucket""",
        (days,),
    )
    return {
        **row, "days": days, "purposes": purposes,
        "funnel": candidate_totals,
        "delivery": delivery,
        "reranking": {
            "bge_scored": candidate_totals.get("bge_scored", 0),
            "live_applied": sum(
                1 for event in recent_contracts
                if (event.get("funnel") or {}).get("live_reranker_applied")
            ),
        },
        "shadow": {item["status"]: item["count"] for item in shadow_rows},
        "schemas": schemas,
        "models": models,
        "thresholds": thresholds,
        "histograms": {"cosine": cosine_histogram, "raw_bge_logit": logit_histogram},
    }


def list_events(*, limit: int = 50, offset: int = 0, purpose: str = "", outcome: str = "") -> list[dict]:
    from .schema import execute_query

    conditions = ["true"]
    params: list[Any] = []
    if purpose:
        conditions.append("purpose = %s")
        params.append(purpose)
    if outcome:
        conditions.append("outcome = %s")
        params.append(outcome)
    params.extend([max(1, min(limit, 200)), max(0, offset)])
    return execute_query(
        f"""SELECT id::text, created_at, purpose, pipeline, status, outcome,
                    query_text, query_sha256, query_length, query_window_count,
                    funnel, latency, delivery, shadow_status, shadow_attempts,
                    shadow_error
             FROM local.retrieval_events WHERE {' AND '.join(conditions)}
             ORDER BY created_at DESC LIMIT %s OFFSET %s""",
        tuple(params),
    )


def get_event(event_id: str) -> Optional[dict]:
    from .schema import execute_query

    event = execute_query(
        """SELECT id::text, created_at, expires_at, user_name, purpose, pipeline,
                  status, outcome, query_text, query_sha256, query_ref, query_length,
                  query_window_count, model_snapshot, config_snapshot, funnel,
                  latency, delivery, shadow_status, shadow_attempts, shadow_error
           FROM local.retrieval_events WHERE id = %s::uuid""",
        (event_id,), fetch="one",
    )
    if not event:
        return None
    event["candidates"] = execute_query(
        """SELECT candidate_key, schema_name, doc_id, parent_id, parent_file,
                  chunk_index, query_window_index, vector_rank, final_rank,
                  similarity, pre_score, raw_bge_logit, bge_probability,
                  blended_score, display_cost, terminal_reason, returned,
                  delivered, channel
           FROM local.retrieval_candidates WHERE event_id = %s::uuid
           ORDER BY vector_rank NULLS LAST, candidate_key""",
        (event_id,),
    )
    event["feedback"] = execute_query(
        "SELECT verdict, expected_missing_ids, note, user_name, updated_at FROM local.retrieval_feedback WHERE event_id = %s::uuid",
        (event_id,), fetch="one",
    )
    feedback = execute_query(
        "SELECT candidate_key, verdict, note, user_name, updated_at FROM local.retrieval_candidate_feedback WHERE event_id = %s::uuid",
        (event_id,),
    )
    by_key = {row["candidate_key"]: row for row in feedback}
    for candidate in event["candidates"]:
        candidate["feedback"] = by_key.get(candidate["candidate_key"])
    return event


def get_event_documents(
    event_id: str,
    preview_chars: int = 240,
    candidate_key: Optional[str] = None,
) -> dict:
    """Resolve candidate locators to source text for UI inspection.

    Telemetry never stores document bodies (content-safe by design), so this
    reads them from the live source tables on demand — the same resolution the
    shadow scorer uses. Keyed by candidate_key; remote mirrors without a local
    body return found=False. preview_chars=0 (or a single candidate_key)
    returns the full text.
    """
    from .schema import _get_pool

    pool = _get_pool()
    out: dict[str, dict] = {}
    full = bool(candidate_key) or preview_chars <= 0
    with pool.connection() as conn:
        params: list[Any] = [event_id]
        key_filter = ""
        if candidate_key:
            key_filter = " AND candidate_key = %s"
            params.append(candidate_key)
        refs = conn.execute(
            f"""SELECT candidate_key, schema_name, doc_id, chunk_index
               FROM local.retrieval_candidates
               WHERE event_id = %s::uuid{key_filter}
               ORDER BY vector_rank NULLS LAST""",
            params,
        ).fetchall()
        for key, schema_name, doc_id, chunk_index in refs:
            row = None
            if schema_name == "local":
                row = conn.execute(
                    """SELECT COALESCE(
                           (SELECT document FROM local.memory_chunks
                            WHERE parent_id = %s AND chunk_index = %s),
                           (SELECT document FROM local.memories WHERE id = %s))""",
                    (doc_id, chunk_index, doc_id),
                ).fetchone()
            elif schema_name == "obsidian":
                row = conn.execute(
                    "SELECT document FROM obsidian.documents WHERE id = %s", (doc_id,)
                ).fetchone()
            document = row[0] if row and row[0] is not None else None
            if document is None:
                out[key] = {"found": False, "size": 0, "text": None, "truncated": False}
            else:
                text = document if full else document[:preview_chars]
                out[key] = {
                    "found": True,
                    "size": len(document),
                    "text": text,
                    "truncated": len(text) < len(document),
                }
    return out


def put_event_feedback(event_id: str, payload: dict, user_name: Optional[str] = None) -> bool:
    verdict = str(payload.get("verdict", ""))
    if verdict not in {"useful", "mixed", "noisy", "missed", "unsure"}:
        raise ValueError("invalid retrieval feedback verdict")
    from .schema import execute_write

    execute_write(
        """INSERT INTO local.retrieval_feedback
           (event_id, verdict, expected_missing_ids, note, user_name)
           VALUES (%s::uuid, %s, %s, %s, %s)
           ON CONFLICT (event_id) DO UPDATE SET verdict = EXCLUDED.verdict,
             expected_missing_ids = EXCLUDED.expected_missing_ids,
             note = EXCLUDED.note, user_name = EXCLUDED.user_name,
             updated_at = now()""",
        (event_id, verdict, _json(payload.get("expected_missing_ids", [])),
         payload.get("note"), user_name),
    )
    return True


def put_candidate_feedback(event_id: str, candidate_key: str, payload: dict, user_name: Optional[str] = None) -> bool:
    verdict = str(payload.get("verdict", ""))
    if verdict not in {"relevant", "irrelevant", "unsure"}:
        raise ValueError("invalid candidate feedback verdict")
    from .schema import execute_write

    execute_write(
        """INSERT INTO local.retrieval_candidate_feedback
           (event_id, candidate_key, verdict, note, user_name)
           VALUES (%s::uuid, %s, %s, %s, %s)
           ON CONFLICT (event_id, candidate_key) DO UPDATE SET
             verdict = EXCLUDED.verdict, note = EXCLUDED.note,
             user_name = EXCLUDED.user_name, updated_at = now()""",
        (event_id, candidate_key, verdict, payload.get("note"), user_name),
    )
    return True


def export_labeled_events() -> list[dict]:
    from .schema import execute_query

    return execute_query(
        """SELECT e.id::text AS trace_id, e.created_at, e.purpose, e.query_text,
                  e.query_sha256, f.verdict, f.expected_missing_ids, f.note,
                  COALESCE(jsonb_agg(jsonb_build_object(
                    'candidate_key', c.candidate_key, 'schema', c.schema_name,
                    'doc_id', c.doc_id, 'similarity', c.similarity,
                    'raw_bge_logit', c.raw_bge_logit,
                    'bge_probability', c.bge_probability,
                    'blended_score', c.blended_score,
                    'terminal_reason', c.terminal_reason,
                    'label', cf.verdict) ORDER BY c.vector_rank)
                    FILTER (WHERE c.candidate_key IS NOT NULL), '[]'::jsonb) AS candidates
           FROM local.retrieval_events e
           JOIN local.retrieval_feedback f ON f.event_id = e.id
           LEFT JOIN local.retrieval_candidates c ON c.event_id = e.id
           LEFT JOIN local.retrieval_candidate_feedback cf
             ON cf.event_id = c.event_id AND cf.candidate_key = c.candidate_key
           GROUP BY e.id, f.event_id ORDER BY e.created_at DESC"""
    )


def simulate_policy(payload: dict) -> dict:
    """Evaluate thresholds against stored scores; never mutates live config."""
    policy = str(payload.get("policy", "cosine-only"))
    if policy not in {"cosine-only", "bge-only", "coarse+bge", "cosine-or-bge"}:
        raise ValueError("invalid policy")
    cosine = float(payload.get("cosine_threshold", 0.85))
    bge = float(payload.get("bge_logit_threshold", -2.5))
    from .schema import execute_query

    rows = execute_query(
        """SELECT c.event_id::text, c.candidate_key, c.similarity,
                  c.raw_bge_logit, f.verdict AS request_label,
                  cf.verdict AS candidate_label
           FROM local.retrieval_candidates c
           LEFT JOIN local.retrieval_feedback f ON f.event_id = c.event_id
           LEFT JOIN local.retrieval_candidate_feedback cf
             ON cf.event_id = c.event_id AND cf.candidate_key = c.candidate_key"""
    )
    # BGE logits only exist for candidates the shadow scorer reached (top-N,
    # local/obsidian bodies, host backend active, job succeeded). Treating a
    # missing score as "rejected by the threshold" would count unscored
    # relevant candidates as false negatives and make bge policies look
    # arbitrarily bad — evaluate each policy only on candidates it can
    # actually score, and report the censored remainder explicitly.
    def _evaluable(row: dict) -> bool:
        # cosine-or-bge mirrors production's recall-additive gate: it requires a
        # similarity (the cosine clause always applies) and treats BGE as an
        # optional rescue — the logit is only consulted when present, so a
        # missing logit does NOT make the row unevaluable (unlike bge-only).
        needs_cosine = policy in ("cosine-only", "coarse+bge", "cosine-or-bge")
        needs_bge = policy in ("bge-only", "coarse+bge")
        if needs_cosine and row["similarity"] is None:
            return False
        if needs_bge and row["raw_bge_logit"] is None:
            return False
        return True

    def _key(row: dict) -> tuple:
        return (row["event_id"], row["candidate_key"])

    scored = [r for r in rows if _evaluable(r)]
    unscored = [r for r in rows if not _evaluable(r)]
    selected = []
    selected_keys: set[tuple] = set()
    for row in scored:
        cos_ok = row["similarity"] is not None and float(row["similarity"]) >= cosine
        bge_ok = row["raw_bge_logit"] is not None and float(row["raw_bge_logit"]) >= bge
        if policy == "cosine-only":
            keep = cos_ok
        elif policy == "bge-only":
            keep = bge_ok
        elif policy == "coarse+bge":
            keep = cos_ok and bge_ok
        else:  # cosine-or-bge — production's recall-additive gate
            keep = cos_ok or bge_ok
        if keep:
            selected.append(row)
            selected_keys.add(_key(row))
    labeled = [r for r in scored if r.get("candidate_label") in ("relevant", "irrelevant")]
    labeled_unscored = sum(
        1 for r in unscored if r.get("candidate_label") in ("relevant", "irrelevant")
    )
    tp = sum(1 for r in labeled if _key(r) in selected_keys and r["candidate_label"] == "relevant")
    fp = sum(1 for r in labeled if _key(r) in selected_keys and r["candidate_label"] == "irrelevant")
    fn = sum(1 for r in labeled if _key(r) not in selected_keys and r["candidate_label"] == "relevant")
    # Request-level metrics carry the same censoring: an event whose
    # candidates were never scored can never be "selected" under a bge
    # policy, so only events with at least one evaluable candidate count.
    evaluable_events = {r["event_id"] for r in scored}
    pos_all = {r["event_id"] for r in rows if r.get("request_label") in ("useful", "mixed", "missed")}
    neg_all = {r["event_id"] for r in rows if r.get("request_label") == "noisy"}
    positive_ids = pos_all & evaluable_events
    negative_ids = neg_all & evaluable_events
    selected_ids = {r["event_id"] for r in selected}
    pos_requests = len(positive_ids)
    neg_requests = len(negative_ids)
    enough = pos_requests >= 20 and neg_requests >= 20
    return {
        "policy": policy, "candidate_count": len(rows), "selected_count": len(selected),
        "scored_count": len(scored), "unscored_count": len(unscored),
        "labeled_count": len(labeled), "labeled_unscored_count": labeled_unscored,
        "true_positive": tp, "false_positive": fp,
        "false_negative": fn, "precision": tp / (tp + fp) if tp + fp else None,
        "recall": tp / (tp + fn) if tp + fn else None,
        "positive_requests": pos_requests, "negative_requests": neg_requests,
        "requests_excluded_unscored": len((pos_all | neg_all) - evaluable_events),
        "positive_request_recall": len(positive_ids & selected_ids) / pos_requests if pos_requests else None,
        "negative_rejection_rate": len(negative_ids - selected_ids) / neg_requests if neg_requests else None,
        "recommendation_ready": enough,
        "config_snippet": {"cosine_threshold": cosine, "bge_logit_threshold": bge},
    }


def _fetch_candidate_documents(conn, event_id: str, limit: int) -> tuple[list[dict], int]:
    """Resolve telemetry locators back to source text only for scoring.

    The shadow reranker MUST score exactly the text the live reranker scored, so
    obsidian fragments are augmented with the same document-context prefix used
    at index/query time (see tools/chunk_context.py). Omitting it here would make
    shadow logits diverge from live logits and corrupt the calibration dataset.
    Local memories are never augmented (their embed/rerank path isn't either).
    """
    from .chunk_context import augment_chunk_for_model
    from .config import get_contextual_embeddings_enabled

    contextual_enabled = get_contextual_embeddings_enabled()
    refs = conn.execute(
        """SELECT candidate_key, schema_name, doc_id, chunk_index,
                  query_window_index, raw_bge_logit
           FROM local.retrieval_candidates
           WHERE event_id = %s::uuid
           ORDER BY vector_rank NULLS LAST LIMIT %s""",
        (event_id, limit),
    ).fetchall()
    out = []
    missing = 0
    for key, schema_name, doc_id, chunk_index, window_index, raw_logit in refs:
        if raw_logit is not None:
            continue
        missing += 1
        document = None
        if schema_name == "local":
            row = conn.execute(
                """SELECT COALESCE(
                       (SELECT document FROM local.memory_chunks
                        WHERE parent_id = %s AND chunk_index = %s),
                       (SELECT document FROM local.memories WHERE id = %s))""",
                (doc_id, chunk_index, doc_id),
            ).fetchone()
            if row and row[0] is not None:
                document = row[0]
        elif schema_name == "obsidian":
            row = conn.execute(
                """SELECT document, title, chunk_heading, chunk_total, parent_file
                   FROM obsidian.documents WHERE id = %s""",
                (doc_id,),
            ).fetchone()
            if row and row[0] is not None:
                try:
                    chunk_total = int(row[3] or 1)
                except (ValueError, TypeError):
                    chunk_total = 1
                document = augment_chunk_for_model(
                    row[0],
                    path=row[4] or "",
                    title=row[1] or "",
                    heading_trail=row[2] or "",
                    is_chunk=chunk_total > 1,
                    enabled=contextual_enabled,
                )
        else:
            # Remote mirrors may not have a stable local body. Mark partial.
            document = None
        if document is not None:
            out.append({"candidate_key": key, "document": document, "query_window_index": window_index})
    return out, missing


def _mark_shadow_skipped(pool, event_id: str, reason: str) -> None:
    with pool.connection() as conn:
        conn.execute(
            """UPDATE local.retrieval_events SET shadow_status = 'skipped',
                      shadow_error = %s, shadow_finished_at = now()
               WHERE id = %s::uuid""",
            (reason, event_id),
        )
        conn.commit()


def _rebuild_live_query_windows(query_text: str, max_index: int) -> tuple[list[str], str]:
    """Rebuild the base query windows exactly as live retrieval built them.

    Live retrieval splits with the embedding service's tokenizer
    (query._prepare_query_windows); candidates store an index into that base
    window list. Rebuilding with different boundaries would score candidates
    against the wrong slice of the prompt and silently corrupt calibration
    data, so return ([], reason) whenever the reconstruction can't be trusted.
    """
    from .embedding import get_embedding_service
    from .query import _QUERY_WINDOW_OVERLAP, _QUERY_WINDOW_TOKENS
    from .text_windows import split_text_windows

    tokenizer = getattr(get_embedding_service(), "tokenize", None)
    if tokenizer is None and len(query_text.encode("utf-8")) > _QUERY_WINDOW_TOKENS:
        return [], "tokenizer unavailable to rebuild multi-window query"

    # split_text_windows silently byte-falls back when the tokenizer RAISES
    # (host outage, non-host backend); byte boundaries differ from the live
    # tokenizer split, so a tokenizer failure must surface as a refusal here,
    # never as differently-sliced windows scored as if they were the originals.
    tokenizer_failure: list[Exception] = []
    strict_tokenize = None
    if tokenizer is not None:
        def strict_tokenize(text, **kwargs):
            try:
                return tokenizer(text, **kwargs)
            except Exception as exc:
                tokenizer_failure.append(exc)
                raise

    windows = split_text_windows(
        query_text,
        max_tokens=_QUERY_WINDOW_TOKENS,
        overlap_tokens=_QUERY_WINDOW_OVERLAP,
        tokenize=strict_tokenize,
    ) or [query_text]
    if tokenizer_failure:
        return [], (
            f"tokenizer failed while rebuilding query windows: "
            f"{tokenizer_failure[0]}"
        )
    if max_index >= len(windows):
        return [], (
            f"stored query_window_index {max_index} exceeds rebuilt "
            f"window count {len(windows)}"
        )
    return windows, ""


def process_one_shadow_job() -> bool:
    """Claim and score one pending event. Safe across multiple workers."""
    if not telemetry_enabled():
        return False
    cfg = _config()
    shadow = cfg.get("shadow") or {}
    if not shadow.get("enabled", True):
        return False
    from .config import get_reranking_config
    from .reranking import rerank_raw
    from .schema import _get_pool

    pool = _get_pool()
    event = None
    with pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                """UPDATE local.retrieval_events SET shadow_status = 'pending',
                          shadow_started_at = NULL
                   WHERE shadow_status = 'running'
                     AND shadow_started_at < now() - interval '5 minutes'"""
            )
            event = conn.execute(
                """SELECT id::text, query_text, query_ref, query_window_count,
                          model_snapshot, shadow_attempts, config_snapshot
                   FROM local.retrieval_events
                   WHERE shadow_status = 'pending'
                     AND created_at <= now() - (%s * interval '1 second')
                   ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 1""",
                (max(0, float(shadow.get("delay_seconds", 2))),),
            ).fetchone()
            if event:
                conn.execute(
                    """UPDATE local.retrieval_events SET shadow_status = 'running',
                              shadow_started_at = now(), shadow_attempts = shadow_attempts + 1
                       WHERE id = %s::uuid""", (event[0],)
                )
    if not event:
        return False
    event_id, query_text, query_ref, _, model_snapshot, attempts, config_snapshot = event
    max_attempts = max(1, int(shadow.get("max_attempts", 3)))
    try:
        rerank_cfg = get_reranking_config()
        expected_model = (model_snapshot or {}).get("reranker_model")
        if expected_model and expected_model != rerank_cfg.get("model"):
            _mark_shadow_skipped(
                pool, event_id, "reranker model identity changed since retrieval"
            )
            return True
        # Window boundaries depend on the EMBEDDING tokenizer, so an embedding
        # model swap invalidates stored query_window_index values even when
        # the reranker is unchanged.
        expected_embedding = (model_snapshot or {}).get("embedding_model")
        if expected_embedding:
            from .config import get_embedding_config
            from .embedding import get_embedding_model_identity

            if expected_embedding != get_embedding_model_identity(get_embedding_config()):
                _mark_shadow_skipped(
                    pool, event_id, "embedding model identity changed since retrieval"
                )
                return True
        # Rerank input text depends on the chunk-context augmentation flag;
        # scoring an old event under a different flag value would contaminate
        # the calibration dataset. Events recorded before stamping (missing
        # key) are scored as-is.
        event_flag = (config_snapshot or {}).get("contextual_embeddings")
        if event_flag is not None:
            from .config import get_contextual_embeddings_enabled

            if bool(event_flag) != bool(get_contextual_embeddings_enabled()):
                _mark_shadow_skipped(
                    pool, event_id,
                    "chunk-context augmentation mode changed since retrieval",
                )
                return True
        with pool.connection() as conn:
            docs, missing_count = _fetch_candidate_documents(
                conn, event_id, max(1, int(shadow.get("candidate_count", 20)))
            )
        if not query_text:
            _mark_shadow_skipped(
                pool, event_id,
                f"shadow query unavailable ({query_ref or 'prompt storage disabled'})",
            )
            return True
        max_index = max(
            (max(0, int(doc["query_window_index"] or 0)) for doc in docs),
            default=0,
        )
        windows, window_error = _rebuild_live_query_windows(query_text, max_index)
        if window_error:
            _mark_shadow_skipped(pool, event_id, window_error)
            return True
        grouped: dict[str, list[dict]] = {}
        for doc in docs:
            index = max(0, int(doc["query_window_index"] or 0))
            grouped.setdefault(windows[index], []).append(doc)
        scores: dict[str, tuple[float, float]] = {}
        total_ms = 0.0
        for query_window, group in grouped.items():
            raw = rerank_raw(query_window, [item["document"] for item in group], rerank_cfg)
            total_ms += float(raw.get("latency_ms", 0))
            for item, logit, probability in zip(group, raw["logits"], raw["probabilities"]):
                scores[item["candidate_key"]] = (float(logit), float(probability))
        with pool.connection() as conn:
            with conn.cursor() as cur:
                for key, (logit, probability) in scores.items():
                    cur.execute(
                        """UPDATE local.retrieval_candidates
                           SET raw_bge_logit = %s, bge_probability = %s
                           WHERE event_id = %s::uuid AND candidate_key = %s""",
                        (logit, probability, event_id, key),
                    )
                cur.execute(
                    """UPDATE local.retrieval_events
                       SET shadow_status = %s, shadow_finished_at = now(),
                           latency = latency || %s
                       WHERE id = %s::uuid""",
                    ("complete" if len(scores) == missing_count else "partial",
                     _json({"shadow_rerank_ms": round(total_ms, 2)}), event_id),
                )
            conn.commit()
        return True
    except Exception as exc:
        terminal = int(attempts or 0) + 1 >= max_attempts
        with pool.connection() as conn:
            conn.execute(
                """UPDATE local.retrieval_events SET shadow_status = %s,
                          shadow_error = %s, shadow_finished_at = CASE WHEN %s THEN now() ELSE NULL END
                   WHERE id = %s::uuid""",
                ("failed" if terminal else "pending", str(exc)[:1000], terminal, event_id),
            )
            conn.commit()
        logger.debug("Shadow reranking deferred for %s: %s", event_id, exc)
        return True


def cleanup_expired() -> int:
    from .schema import execute_query

    row = execute_query(
        """WITH deleted AS (
               DELETE FROM local.retrieval_events WHERE expires_at < now()
               RETURNING 1
           ) SELECT count(*) AS count FROM deleted""",
        fetch="one",
    )
    return int((row or {}).get("count", 0))


async def retrieval_telemetry_loop() -> None:
    """Background shadow scorer and retention janitor.

    The job body is synchronous (psycopg round-trips plus a blocking host
    rerank HTTP call up to host_timeout_ms) — run it in a worker thread so a
    shadow job can never stall the event loop that serves /hook/prompt-context
    and MCP traffic.
    """
    last_cleanup = 0.0
    while True:
        try:
            cfg = _config()
            shadow = cfg.get("shadow") or {}
            poll = max(0.25, float(shadow.get("poll_seconds", 2)))
            rate = max(0.01, float(shadow.get("max_jobs_per_second", 1)))
            poll = max(poll, 1.0 / rate)
            await asyncio.to_thread(process_one_shadow_job)
            if time.monotonic() - last_cleanup > 86400:
                await asyncio.to_thread(cleanup_expired)
                last_cleanup = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug("Retrieval telemetry worker idle: %s", exc)
            poll = 5.0
        await asyncio.sleep(poll)
