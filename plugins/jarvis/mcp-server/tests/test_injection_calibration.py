"""Tests for the real-usage injection threshold calibration harness."""

from bench.injection_calibration import evaluate_threshold, select_threshold


CASES = [
    {"id": "positive", "query": "known", "expected_ids": ["memory-a"]},
    {"id": "negative", "query": "noise", "expected_ids": []},
]


def test_evaluate_threshold_scores_positive_and_negative_cases():
    def search(query, threshold):
        matches = [{"id": "memory-a"}] if query == "known" else []
        return {"matches": matches, "query_ms": 12.0}

    result = evaluate_threshold(CASES, 0.82, search)

    assert result["precision"] == 1.0
    assert result["recall"] == 1.0
    assert result["negative_rejection"] == 1.0
    assert result["failures"] == []


def test_evaluate_threshold_accepts_stable_parent_file_for_vault_chunk():
    cases = [
        {
            "id": "vault-positive",
            "query": "phase five migration",
            "expected_ids": ["roadmaps/v3/v3-migration-phase5.md"],
        }
    ]

    result = evaluate_threshold(
        cases,
        0.85,
        lambda _query, _threshold: {
            "matches": [
                {
                    "id": "1784061140046",
                    "parent_file": "roadmaps/v3/v3-migration-phase5.md",
                }
            ],
            "query_ms": 4,
        },
    )

    assert result["recall"] == 1.0
    assert result["failures"] == []


def test_false_positive_reduces_precision_and_negative_rejection():
    def search(query, threshold):
        return {"matches": [{"id": "memory-a"}], "query_ms": 5.0}

    result = evaluate_threshold(CASES, 0.80, search)

    assert result["recall"] == 1.0
    assert result["precision"] == 0.5
    assert result["negative_rejection"] == 0.0
    assert result["failures"][0]["kind"] == "false_positive"


def test_selection_requires_negative_quality_then_maximizes_recall():
    evaluations = [
        {"threshold": 0.80, "precision": 0.95, "recall": 1.0, "negative_rejection": 0.90, "mean_matches": 1.2},
        {"threshold": 0.82, "precision": 1.0, "recall": 0.90, "negative_rejection": 0.95, "mean_matches": 0.8},
        {"threshold": 0.85, "precision": 1.0, "recall": 0.70, "negative_rejection": 1.0, "mean_matches": 0.5},
    ]

    selected = select_threshold(evaluations, minimum_negative_rejection=0.95)

    assert selected["threshold"] == 0.82


def test_selection_uses_lowest_threshold_when_quality_and_recall_tie():
    evaluations = [
        {"threshold": 0.876, "precision": 1.0, "recall": 0.26, "negative_rejection": 1.0, "mean_matches": 0.29},
        {"threshold": 0.878, "precision": 1.0, "recall": 0.26, "negative_rejection": 1.0, "mean_matches": 0.26},
    ]

    selected = select_threshold(evaluations)

    assert selected["threshold"] == 0.876
