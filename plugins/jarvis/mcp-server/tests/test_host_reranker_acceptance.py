from __future__ import annotations

from bench.host_reranker_acceptance import (
    evaluate_gate,
    select_gate,
    summarize_ranking,
)


def _candidate(identifier: str, vector: float, logit: float) -> dict:
    return {
        "identifiers": [identifier],
        "vector_score": vector,
        "bge_logit": logit,
    }


def test_absolute_gate_can_reject_negative_case_and_keep_positive_hit():
    records = [
        {
            "id": "positive",
            "expected_ids": ["notes/right.md"],
            "candidates": [
                _candidate("vault::notes/wrong.md#chunk-0", 0.9, -4.0),
                _candidate("vault::notes/right.md#chunk-2", 0.8, 5.0),
            ],
        },
        {
            "id": "negative",
            "expected_ids": [],
            "candidates": [_candidate("notes/noise.md", 0.9, -3.0)],
        },
    ]

    result = evaluate_gate(records, 0.0)

    assert result["precision"] == 1.0
    assert result["recall"] == 1.0
    assert result["negative_rejection"] == 1.0


def test_select_gate_prioritizes_recall_after_negative_constraint():
    selected = select_gate(
        [
            {"threshold": 0.0, "recall": 1.0, "precision": 0.6, "negative_rejection": 0.8},
            {"threshold": 2.0, "recall": 0.8, "precision": 0.9, "negative_rejection": 1.0},
            {"threshold": 4.0, "recall": 0.5, "precision": 1.0, "negative_rejection": 1.0},
        ],
        minimum_negative_rejection=0.95,
    )

    assert selected["threshold"] == 2.0


def test_ranking_summary_compares_vector_and_bge_orders():
    records = [
        {
            "id": "positive",
            "expected_ids": ["notes/right.md"],
            "rerank_ms": 42.0,
            "candidates": [
                _candidate("notes/wrong.md", 0.9, -4.0),
                _candidate("notes/right.md", 0.8, 5.0),
            ],
        }
    ]

    summary = summarize_ranking(records)

    assert summary["candidate_recall"] == 1.0
    assert summary["vector_top1"] == 0.0
    assert summary["bge_top1"] == 1.0
    assert summary["bge_mrr"] > summary["vector_mrr"]


def test_missing_labels_are_reported_but_not_counted_as_model_misses():
    records = [
        {
            "id": "stale-positive",
            "expected_ids": ["deleted-memory"],
            "available_expected_ids": [],
            "rerank_ms": 1.0,
            "candidates": [_candidate("unrelated", 0.9, 5.0)],
        },
        {
            "id": "live-positive",
            "expected_ids": ["notes/right.md"],
            "available_expected_ids": ["notes/right.md"],
            "rerank_ms": 1.0,
            "candidates": [_candidate("notes/right.md", 0.8, 5.0)],
        },
    ]

    gate = evaluate_gate(records, 0.0)
    ranking = summarize_ranking(records)

    assert gate["positive_total"] == 1
    assert gate["positive_hits"] == 1
    assert gate["skipped_missing_labels"] == 1
    assert ranking["positive_cases"] == 1
    assert ranking["candidate_recall"] == 1.0
    assert ranking["skipped_missing_labels"] == 1
