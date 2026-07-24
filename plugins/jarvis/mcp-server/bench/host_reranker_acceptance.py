"""Evaluate the live host BGE reranker on labeled personal retrieval cases.

Run inside the production container after indexing. The benchmark recalls the
top vector candidates with the deployed Granite service, scores them once with
the deployed BGE service, and then sweeps absolute BGE-logit gates. It never
increments retrieval counts or changes configuration.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
from datetime import datetime, timezone
from pathlib import Path


HERE = Path(__file__).parent
DEFAULT_CASES = HERE / "injection_quality_cases.json"
RESULTS = HERE / "results"


def _canonical_identifier(value: str) -> str:
    value = str(value or "").strip()
    if value.startswith("vault::"):
        value = value[len("vault::") :]
    return value.split("#chunk-", 1)[0]


def _matches_expected(candidate: dict, expected_ids: list[str]) -> bool:
    identifiers = {
        _canonical_identifier(value)
        for value in candidate.get("identifiers", [])
        if value
    }
    return any(_canonical_identifier(value) in identifiers for value in expected_ids)


def _evaluation_expected_ids(record: dict) -> list[str]:
    """Return only labels that exist in the live retrieval corpus, when known."""
    return record.get("available_expected_ids", record.get("expected_ids", []))


def annotate_label_availability(cases: list[dict]) -> tuple[list[dict], dict]:
    """Resolve labeled IDs against the live local and vault indexes.

    Acceptance datasets outlive individual memories. Missing labels must be
    reported, but must not be counted as reranker misses: no retrieval or
    reranking model can select a document that is absent from the corpus.
    """
    expected = sorted(
        {
            _canonical_identifier(identifier)
            for case in cases
            for identifier in case.get("expected_ids", [])
            if identifier
        }
    )
    if not expected:
        return [dict(case) for case in cases], {
            "distinct_expected": 0,
            "available": [],
            "missing": [],
            "skipped_cases": [],
        }

    from tools.schema import _get_pool

    found: set[str] = set()
    with _get_pool().connection() as conn:
        local_rows = conn.execute(
            """
            SELECT id
            FROM local.memories
            WHERE status = 'active' AND id = ANY(%s)
            """,
            (expected,),
        ).fetchall()
        found.update(_canonical_identifier(row[0]) for row in local_rows)

        vault_rows = conn.execute(
            """
            SELECT DISTINCT parent_file
            FROM obsidian.documents
            WHERE parent_file = ANY(%s)
               OR id = ANY(%s)
            """,
            (expected, expected),
        ).fetchall()
        found.update(_canonical_identifier(row[0]) for row in vault_rows)

    annotated = []
    skipped_cases = []
    for case in cases:
        row = dict(case)
        labeled = case.get("expected_ids", [])
        row["available_expected_ids"] = [
            identifier
            for identifier in labeled
            if _canonical_identifier(identifier) in found
        ]
        row["missing_expected_ids"] = [
            identifier
            for identifier in labeled
            if _canonical_identifier(identifier) not in found
        ]
        if labeled and not row["available_expected_ids"]:
            skipped_cases.append(case["id"])
        annotated.append(row)

    return annotated, {
        "distinct_expected": len(expected),
        "available": sorted(found),
        "missing": sorted(set(expected) - found),
        "skipped_cases": skipped_cases,
    }


def evaluate_gate(records: list[dict], threshold: float) -> dict:
    """Evaluate an absolute BGE-logit threshold on already-scored cases."""
    positive_total = positive_hits = skipped_missing_labels = 0
    negative_total = negative_rejections = 0
    selected_counts: list[int] = []
    failures: list[dict] = []

    for record in records:
        labeled = record.get("expected_ids", [])
        expected = _evaluation_expected_ids(record)
        if labeled and not expected:
            skipped_missing_labels += 1
            continue
        selected = [
            candidate
            for candidate in record.get("candidates", [])
            if float(candidate["bge_logit"]) >= threshold
        ]
        selected_counts.append(len(selected))
        if expected:
            positive_total += 1
            if any(_matches_expected(candidate, expected) for candidate in selected):
                positive_hits += 1
            else:
                failures.append({"id": record["id"], "kind": "miss"})
        else:
            negative_total += 1
            if not selected:
                negative_rejections += 1
            else:
                failures.append(
                    {
                        "id": record["id"],
                        "kind": "false_positive",
                        "top_logit": round(
                            max(float(item["bge_logit"]) for item in selected), 4
                        ),
                    }
                )

    false_positive_cases = negative_total - negative_rejections
    precision_denominator = positive_hits + false_positive_cases
    precision = (
        positive_hits / precision_denominator if precision_denominator else 1.0
    )
    recall = positive_hits / positive_total if positive_total else 1.0
    negative_rejection = (
        negative_rejections / negative_total if negative_total else 1.0
    )
    return {
        "threshold": round(float(threshold), 4),
        "probability": round(1.0 / (1.0 + math.exp(-threshold)), 6),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "negative_rejection": round(negative_rejection, 4),
        "positive_hits": positive_hits,
        "positive_total": positive_total,
        "skipped_missing_labels": skipped_missing_labels,
        "false_positive_cases": false_positive_cases,
        "negative_total": negative_total,
        "mean_selected": round(statistics.mean(selected_counts), 3),
        "failures": failures,
    }


def select_gate(
    evaluations: list[dict], minimum_negative_rejection: float = 0.95
) -> dict:
    """Maximize positive recall after satisfying the hard-negative constraint."""
    if not evaluations:
        raise ValueError("No gate evaluations provided")
    eligible = [
        row
        for row in evaluations
        if row["negative_rejection"] >= minimum_negative_rejection
    ]
    pool = eligible or evaluations
    return max(
        pool,
        key=lambda row: (
            row["recall"],
            row["precision"],
            row["negative_rejection"],
            -row["threshold"],
        ),
    )


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    position = max(0, math.ceil(fraction * len(ordered)) - 1)
    return float(ordered[position])


def summarize_ranking(records: list[dict]) -> dict:
    positive = [record for record in records if _evaluation_expected_ids(record)]
    skipped_missing_labels = sum(
        bool(record.get("expected_ids"))
        and not bool(_evaluation_expected_ids(record))
        for record in records
    )
    candidate_hits = vector_top1 = bge_top1 = 0
    vector_rr: list[float] = []
    bge_rr: list[float] = []

    for record in positive:
        expected = _evaluation_expected_ids(record)
        vector_order = sorted(
            record["candidates"], key=lambda item: item["vector_score"], reverse=True
        )
        bge_order = sorted(
            record["candidates"], key=lambda item: item["bge_logit"], reverse=True
        )

        def rank(order: list[dict]) -> int | None:
            for index, candidate in enumerate(order, start=1):
                if _matches_expected(candidate, expected):
                    return index
            return None

        vector_rank = rank(vector_order)
        bge_rank = rank(bge_order)
        if vector_rank is not None:
            candidate_hits += 1
            vector_rr.append(1.0 / vector_rank)
            vector_top1 += vector_rank == 1
        else:
            vector_rr.append(0.0)
        if bge_rank is not None:
            bge_rr.append(1.0 / bge_rank)
            bge_top1 += bge_rank == 1
        else:
            bge_rr.append(0.0)

    denominator = len(positive) or 1
    latencies = [float(record.get("rerank_ms", 0.0)) for record in records]
    return {
        "positive_cases": len(positive),
        "skipped_missing_labels": skipped_missing_labels,
        "candidate_recall": round(candidate_hits / denominator, 4),
        "vector_top1": round(vector_top1 / denominator, 4),
        "bge_top1": round(bge_top1 / denominator, 4),
        "vector_mrr": round(statistics.mean(vector_rr) if vector_rr else 0.0, 4),
        "bge_mrr": round(statistics.mean(bge_rr) if bge_rr else 0.0, 4),
        "rerank_latency_ms": {
            "p50": round(_percentile(latencies, 0.50), 1),
            "p95": round(_percentile(latencies, 0.95), 1),
            "max": round(max(latencies, default=0.0), 1),
        },
    }


def _retrieve_candidates(query: str, candidate_count: int) -> list[dict]:
    """Recall and rank production candidates without applying the cosine gate."""
    from tools.config import (
        get_decay_config,
        get_embedding_config,
        get_expansion_config,
        get_ranking_config,
    )
    from tools.embedding import get_embedding_service
    from tools.expansion import expand_query
    from tools.namespaces import SCHEMA_LOCAL
    from tools.paths import SENSITIVE_PATHS
    from tools.query import (
        _cross_schema_search,
        _format_core_result,
        _format_vault_result,
        _parse_optional_row_datetime,
        _parse_row_datetime,
    )
    from tools.ranking import DEFAULT_IMPORTANCE_WEIGHT, compute_unified_score, score_memory
    from tools.schema_registry import _core_like_schemas

    expansion = expand_query(query, get_expansion_config())
    vector = get_embedding_service().encode(expansion["expanded"])
    rows = _cross_schema_search(vector, max(100, candidate_count * 5))
    core_like = _core_like_schemas()
    decay_config = get_decay_config()
    use_decay = decay_config.get("enabled", True)
    importance_weight = get_ranking_config().get(
        "importance_weight", DEFAULT_IMPORTANCE_WEIGHT
    )
    active_model = get_embedding_config().get("model_id")
    best: dict[tuple[str, str], dict] = {}

    for row in rows:
        schema = row.get("_schema", "obsidian")
        meta = (
            _format_core_result(row)
            if schema in core_like
            else _format_vault_result(row)
        )
        if meta.get("directory", "") in SENSITIVE_PATHS:
            continue
        similarity = 1.0 - float(row["distance"])
        try:
            importance = float(meta.get("importance_score", 0.5))
        except (TypeError, ValueError):
            importance = 0.5
        if use_decay and schema in core_like:
            vector_score, _ = score_memory(
                similarity=similarity,
                base_importance=importance,
                created_at=_parse_row_datetime(
                    row.get("created_at") or meta.get("created_at")
                ),
                last_retrieved_at=_parse_optional_row_datetime(
                    meta.get("last_retrieved_at")
                ),
                retrieval_count=int(float(meta.get("retrieval_count", 0))),
                importance_weight=importance_weight,
                decay_config=decay_config,
            )
        else:
            vector_score = compute_unified_score(
                similarity, importance, importance_weight=importance_weight
            )
        parent = meta.get("parent_file") or str(row["id"]).split("::", 1)[-1]
        candidate = {
            "id": row["id"],
            "parent_file": parent,
            "document": row.get("document") or "",
            "schema": schema,
            "similarity": similarity,
            "vector_score": vector_score,
            "identifiers": [row["id"], parent],
            "embedding_model": active_model,
        }
        key = (parent, schema)
        if key not in best or vector_score > best[key]["vector_score"]:
            best[key] = candidate

    return sorted(
        best.values(), key=lambda item: item["vector_score"], reverse=True
    )[:candidate_count]


def collect_records(cases: list[dict], candidate_count: int) -> list[dict]:
    from tools.config import get_reranking_config
    from tools.reranking import rerank_raw

    config = get_reranking_config()
    if config.get("backend") != "host":
        raise RuntimeError("Host BGE acceptance requires reranking.backend=host")

    records = []
    for position, case in enumerate(cases, start=1):
        candidates = _retrieve_candidates(case["query"], candidate_count)
        diagnostics = rerank_raw(
            case["query"], [item["document"] for item in candidates], config
        )
        for candidate, logit, probability in zip(
            candidates, diagnostics["logits"], diagnostics["probabilities"]
        ):
            candidate["bge_logit"] = logit
            candidate["bge_probability"] = probability
            candidate.pop("document", None)
        records.append(
            {
                "id": case["id"],
                "query": case["query"],
                "expected_ids": case.get("expected_ids", []),
                "available_expected_ids": case.get(
                    "available_expected_ids", case.get("expected_ids", [])
                ),
                "missing_expected_ids": case.get("missing_expected_ids", []),
                "candidates": candidates,
                "rerank_ms": diagnostics["latency_ms"],
            }
        )
        print(
            f"[{position:02d}/{len(cases):02d}] {case['id']}: "
            f"{len(candidates)} candidates, {diagnostics['latency_ms']:.1f} ms",
            flush=True,
        )
    return records


def render_report(payload: dict) -> str:
    ranking = payload["ranking"]
    selected = payload["selected_gate"]
    latency = ranking["rerank_latency_ms"]
    lines = [
        "# Host BGE production acceptance",
        "",
        f"**Run:** {payload['run_at']} · **cases:** {payload['case_count']} · "
        f"**candidates:** {payload['candidate_count']}",
        "",
        "## Ranking",
        "",
        f"- Evaluated positive labels: `{ranking['positive_cases']}` "
        f"(`{ranking['skipped_missing_labels']}` stale cases skipped)",
        f"- Candidate recall: `{ranking['candidate_recall']:.3f}`",
        f"- Vector → BGE top-1: `{ranking['vector_top1']:.3f}` → `{ranking['bge_top1']:.3f}`",
        f"- Vector → BGE MRR: `{ranking['vector_mrr']:.3f}` → `{ranking['bge_mrr']:.3f}`",
        f"- BGE latency p50/p95/max: `{latency['p50']:.1f}` / "
        f"`{latency['p95']:.1f}` / `{latency['max']:.1f}` ms",
        "",
        "## Absolute BGE gate",
        "",
        f"Selected logit `{selected['threshold']:.3f}` "
        f"(sigmoid `{selected['probability']:.6f}`) under the "
        f"`{payload['minimum_negative_rejection']:.0%}` hard-negative constraint.",
        "",
        "| logit | sigmoid | precision | recall | negative rejection | mean selected |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["gate_evaluations"]:
        marker = " **←**" if row["threshold"] == selected["threshold"] else ""
        lines.append(
            f"| {row['threshold']:.2f}{marker} | {row['probability']:.5f} | "
            f"{row['precision']:.3f} | {row['recall']:.3f} | "
            f"{row['negative_rejection']:.3f} | {row['mean_selected']:.2f} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--candidate-count", type=int, default=20)
    parser.add_argument("--minimum-negative-rejection", type=float, default=0.95)
    parser.add_argument(
        "--logit-thresholds",
        default="-12,-10,-8,-6,-4,-2,0,2,4,6,8,10,12",
    )
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    cases_payload = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = cases_payload["cases"]
    from tools.schema_registry import rebuild_registry

    rebuild_registry()
    cases, label_availability = annotate_label_availability(cases)
    records = collect_records(cases, max(1, args.candidate_count))
    thresholds = sorted(
        {float(value.strip()) for value in args.logit_thresholds.split(",")}
    )
    evaluations = [evaluate_gate(records, value) for value in thresholds]
    selected = select_gate(evaluations, args.minimum_negative_rejection)
    payload = {
        "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "dataset_version": cases_payload.get("version", 1),
        "dataset_as_of": cases_payload.get("as_of"),
        "case_count": len(cases),
        "candidate_count": args.candidate_count,
        "minimum_negative_rejection": args.minimum_negative_rejection,
        "label_availability": label_availability,
        "ranking": summarize_ranking(records),
        "gate_evaluations": evaluations,
        "selected_gate": selected,
        "records": records,
    }
    report = render_report(payload)
    print(report, end="")

    if not args.no_write:
        RESULTS.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        stem = RESULTS / f"{stamp}-host-bge-acceptance"
        stem.with_suffix(".json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        stem.with_suffix(".md").write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
