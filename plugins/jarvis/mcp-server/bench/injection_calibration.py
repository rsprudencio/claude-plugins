"""Calibrate the passive-injection quality gate against labeled real usage.

Run inside the production container so the script uses the deployed ONNX model,
database, schemas, ranking, and semantic deduplication configuration:

    python -m bench.injection_calibration

The evaluation never increments retrieval counts. The bundled cases are dated
and user-specific by design; refresh their labels when the indexed corpus changes.
"""

from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


HERE = Path(__file__).parent
DEFAULT_CASES = HERE / "injection_quality_cases.json"
RESULTS = HERE / "results"


def evaluate_threshold(
    cases: list[dict],
    threshold: float,
    search: Callable[[str, float], dict],
) -> dict:
    """Evaluate one threshold with case-level precision and recall."""
    positive_total = positive_hits = 0
    negative_total = negative_rejections = 0
    match_counts: list[int] = []
    latencies: list[float] = []
    failures: list[dict] = []

    for case in cases:
        result = search(case["query"], threshold)
        matches = result.get("matches") or []
        match_identifiers: set[str] = set()
        match_labels: list[str] = []
        for match in matches:
            direct_id = str(match.get("id") or "")
            parent_file = str(match.get("parent_file") or "")
            match_identifiers.update(
                value for value in (direct_id, parent_file) if value
            )
            match_labels.append(parent_file or direct_id)
        expected = [str(value) for value in case.get("expected_ids", [])]
        match_counts.append(len(matches))
        latencies.append(float(result.get("query_ms", 0.0)))

        if expected:
            positive_total += 1
            hit = any(value in match_identifiers for value in expected)
            if hit:
                positive_hits += 1
            else:
                failures.append(
                    {"id": case["id"], "kind": "miss", "matches": match_labels}
                )
        else:
            negative_total += 1
            if not matches:
                negative_rejections += 1
            else:
                failures.append(
                    {
                        "id": case["id"],
                        "kind": "false_positive",
                        "matches": match_labels,
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
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0

    return {
        "threshold": round(float(threshold), 4),
        "precision": round(precision, 4),
        "recall": round(recall, 4),
        "negative_rejection": round(negative_rejection, 4),
        "f1": round(f1, 4),
        "positive_hits": positive_hits,
        "positive_total": positive_total,
        "false_positive_cases": false_positive_cases,
        "negative_total": negative_total,
        "mean_matches": round(statistics.mean(match_counts), 3),
        "mean_query_ms": round(statistics.mean(latencies), 1),
        "max_query_ms": round(max(latencies, default=0.0), 1),
        "failures": failures,
    }


def select_threshold(
    evaluations: list[dict], minimum_negative_rejection: float = 0.95
) -> dict:
    """Maximize recall after the hard-negative quality constraint is met."""
    eligible = [
        row
        for row in evaluations
        if row["negative_rejection"] >= minimum_negative_rejection
    ]
    if not evaluations:
        raise ValueError("No threshold evaluations provided")
    if not eligible:
        return max(
            evaluations,
            key=lambda row: (
                row["negative_rejection"],
                row["precision"],
                row["recall"],
                -row["threshold"],
            ),
        )
    return max(
        eligible,
        key=lambda row: (
            row["recall"],
            row["precision"],
            -row["threshold"],
        ),
    )


def render_report(payload: dict) -> str:
    selected = payload["selected"]
    lines = [
        "# Jarvis injection quality calibration",
        "",
        f"**Run:** {payload['run_at']} · **cases:** {payload['case_count']}",
        "",
        f"**Selected raw-cosine threshold: `{selected['threshold']:.3f}`**",
        "",
        "| threshold | precision | recall | negative rejection | mean matches | mean ms |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in payload["evaluations"]:
        marker = " **←**" if row["threshold"] == selected["threshold"] else ""
        lines.append(
            f"| {row['threshold']:.3f}{marker} | {row['precision']:.3f} | "
            f"{row['recall']:.3f} | {row['negative_rejection']:.3f} | "
            f"{row['mean_matches']:.2f} | {row['mean_query_ms']:.1f} |"
        )
    lines.extend(["", "## Selected-threshold failures", ""])
    if selected["failures"]:
        for failure in selected["failures"]:
            lines.append(
                f"- `{failure['id']}` — {failure['kind']}; matches: "
                f"{', '.join(failure['matches']) or 'none'}"
            )
    else:
        lines.append("None.")
    return "\n".join(lines) + "\n"


def _parse_thresholds(raw: str) -> list[float]:
    values = sorted({round(float(item.strip()), 4) for item in raw.split(",")})
    if not values or any(value < -1 or value > 1 for value in values):
        raise ValueError("Thresholds must be comma-separated values in [-1, 1]")
    return values


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument(
        "--thresholds",
        default=(
            "0.780,0.790,0.800,0.810,0.815,0.820,0.825,0.830,0.840,"
            "0.850,0.855,0.860,0.862,0.864,0.866,0.868,0.870,0.872,"
            "0.874,0.876,0.878,0.880"
        ),
    )
    parser.add_argument("--minimum-negative-rejection", type=float, default=0.95)
    parser.add_argument("--no-write", action="store_true")
    args = parser.parse_args(argv)

    cases_payload = json.loads(args.cases.read_text(encoding="utf-8"))
    cases = cases_payload["cases"]
    thresholds = _parse_thresholds(args.thresholds)

    from tools.query import semantic_context

    def search(query: str, threshold: float) -> dict:
        return semantic_context(
            query=query,
            threshold=threshold,
            budget=8000,
            skip_retrieval_increment=True,
            schemas=None,
            max_results=20,
        )

    evaluations = [
        evaluate_threshold(cases, threshold, search) for threshold in thresholds
    ]
    selected = select_threshold(evaluations, args.minimum_negative_rejection)
    payload = {
        "run_at": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "dataset_version": cases_payload.get("version", 1),
        "dataset_as_of": cases_payload.get("as_of"),
        "case_count": len(cases),
        "minimum_negative_rejection": args.minimum_negative_rejection,
        "selected": selected,
        "evaluations": evaluations,
    }
    report = render_report(payload)
    print(report, end="")

    if not args.no_write:
        RESULTS.mkdir(exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        stem = RESULTS / f"{stamp}-injection-quality"
        stem.with_suffix(".json").write_text(
            json.dumps(payload, indent=2) + "\n", encoding="utf-8"
        )
        stem.with_suffix(".md").write_text(report, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
