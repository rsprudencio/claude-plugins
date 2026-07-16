"""Tests for the retrieval benchmark harness.

The split-selection test guards a bug that actually shipped: MTEB declares
`eval_splits = ['dev', 'test']` for STS-B, so taking `eval_splits[0]` silently
evaluated on the DEV split. That inflated the score from 0.77 to 0.83 and would
have made a model look better than it is. Evaluation bugs are silent by nature —
they produce a plausible number, not an error.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

pytest.importorskip("scipy", reason="bench extra not installed")

from bench.harness import _calibrate_theta, _ndcg_at_10, _pick_split  # noqa: E402


def _task(eval_splits):
    return SimpleNamespace(metadata=SimpleNamespace(eval_splits=eval_splits))


class TestPickSplit:
    def test_prefers_test_over_dev_even_when_dev_is_first(self):
        """The actual bug: eval_splits[0] == 'dev' for STS-B."""
        assert _pick_split(_task(["dev", "test"]), {"train", "dev", "test"}) == "test"

    def test_uses_test_when_only_declared_split(self):
        assert _pick_split(_task(["test"]), {"train", "test"}) == "test"

    def test_falls_back_to_a_declared_split_when_no_test(self):
        assert _pick_split(_task(["validation"]), {"train", "validation"}) == "validation"

    def test_never_returns_train_when_an_eval_split_exists(self):
        assert _pick_split(_task(["dev"]), {"train", "dev"}) != "train"


class TestNdcg:
    def test_perfect_ranking_scores_one(self):
        scores = np.array([[0.9], [0.5], [0.1]])
        assert _ndcg_at_10(scores, ["a", "b", "c"], ["q"], {"q": {"a"}}) == pytest.approx(1.0)

    def test_relevant_doc_ranked_second_is_discounted(self):
        scores = np.array([[0.9], [0.5], [0.1]])
        # log2(2+1) = 1.585 -> 1/1.585
        assert _ndcg_at_10(scores, ["a", "b", "c"], ["q"], {"q": {"b"}}) == pytest.approx(
            1 / np.log2(3), rel=1e-6)

    def test_relevant_doc_outside_top10_scores_zero(self):
        scores = np.array([[0.0]] + [[1.0]] * 12)          # target is rank 13
        ids = ["target"] + ["d%d" % i for i in range(12)]
        assert _ndcg_at_10(scores, ids, ["q"], {"q": {"target"}}) == 0.0


class TestThetaCalibration:
    def test_finds_separating_threshold(self):
        cos = np.array([0.95, 0.92, 0.90, 0.60, 0.55, 0.50])
        gold = np.array([5.0, 4.0, 3.0, 1.0, 0.0, 0.0])
        theta, acc = _calibrate_theta(cos, gold)
        assert 0.60 < theta <= 0.90
        assert acc == pytest.approx(1.0)

    def test_returns_nan_when_a_class_is_missing(self):
        cos = np.array([0.9, 0.8])
        gold = np.array([5.0, 4.0])                        # no irrelevant pairs
        theta, acc = _calibrate_theta(cos, gold)
        assert np.isnan(theta) and np.isnan(acc)
