"""Tests for tools/ranking.py — two-phase retrieval with blended scoring."""

import pytest
from datetime import datetime, timedelta, timezone

from tools.ranking import compute_blended_score, rerank_candidates, _parse_datetime


NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestParseDatetime:
    """Helper datetime parser handles various inputs."""

    def test_none(self):
        assert _parse_datetime(None) is None

    def test_datetime_with_tz(self):
        dt = datetime(2026, 1, 1, tzinfo=timezone.utc)
        assert _parse_datetime(dt) == dt

    def test_datetime_naive(self):
        dt = datetime(2026, 1, 1)
        result = _parse_datetime(dt)
        assert result.tzinfo == timezone.utc

    def test_iso_string(self):
        result = _parse_datetime("2026-01-01T00:00:00Z")
        assert result is not None
        assert result.year == 2026

    def test_invalid_string(self):
        assert _parse_datetime("not-a-date") is None


class TestComputeBlendedScore:
    """Blended score = sim_weight * similarity + imp_weight * effective_importance."""

    def test_high_similarity_high_importance(self):
        score, eff_imp = compute_blended_score(
            similarity=0.9,
            base_importance=0.8,
            created_at=NOW,
            similarity_weight=0.7,
            importance_weight=0.3,
            now=NOW,
        )
        # 0.7 * 0.9 + 0.3 * 0.8 = 0.63 + 0.24 = 0.87
        assert score == pytest.approx(0.87, abs=0.02)

    def test_high_similarity_low_importance(self):
        score, _ = compute_blended_score(
            similarity=0.9,
            base_importance=0.2,
            created_at=NOW,
            similarity_weight=0.7,
            importance_weight=0.3,
            now=NOW,
        )
        # 0.7 * 0.9 + 0.3 * 0.2 = 0.63 + 0.06 = 0.69
        assert score == pytest.approx(0.69, abs=0.02)

    def test_low_similarity_high_importance(self):
        score, _ = compute_blended_score(
            similarity=0.3,
            base_importance=0.9,
            created_at=NOW,
            similarity_weight=0.7,
            importance_weight=0.3,
            now=NOW,
        )
        # 0.7 * 0.3 + 0.3 * 0.9 = 0.21 + 0.27 = 0.48
        assert score == pytest.approx(0.48, abs=0.02)

    def test_decay_reduces_old_memory(self):
        """Old memory gets lower score than new one with same base importance."""
        score_new, _ = compute_blended_score(
            similarity=0.8,
            base_importance=0.7,
            created_at=NOW,
            now=NOW,
        )
        score_old, _ = compute_blended_score(
            similarity=0.8,
            base_importance=0.7,
            created_at=NOW - timedelta(days=180),
            now=NOW,
        )
        assert score_new > score_old

    def test_retrieval_boosts_old_memory(self):
        """Recently retrieved old memory scores higher than untouched old one."""
        created = NOW - timedelta(days=180)

        score_retrieved, _ = compute_blended_score(
            similarity=0.8,
            base_importance=0.7,
            created_at=created,
            last_retrieved_at=NOW - timedelta(days=1),
            now=NOW,
        )
        score_untouched, _ = compute_blended_score(
            similarity=0.8,
            base_importance=0.7,
            created_at=created,
            now=NOW,
        )
        assert score_retrieved > score_untouched

    def test_custom_weights(self):
        """Equal weights blend 50/50."""
        score, _ = compute_blended_score(
            similarity=0.8,
            base_importance=0.6,
            created_at=NOW,
            similarity_weight=0.5,
            importance_weight=0.5,
            now=NOW,
        )
        # 0.5 * 0.8 + 0.5 * 0.6 = 0.70
        assert score == pytest.approx(0.70, abs=0.02)

    def test_effective_importance_returned(self):
        """Returns effective importance alongside blended score."""
        _, eff_imp = compute_blended_score(
            similarity=0.8,
            base_importance=0.7,
            created_at=NOW,
            now=NOW,
        )
        assert 0.0 <= eff_imp <= 1.0
        assert eff_imp == pytest.approx(0.7, abs=0.01)

    def test_decay_config_passed_through(self):
        """Custom decay config affects effective importance."""
        _, eff_fast = compute_blended_score(
            similarity=0.8,
            base_importance=0.7,
            created_at=NOW - timedelta(days=60),
            decay_config={"rate_per_month": 0.2},
            now=NOW,
        )
        _, eff_slow = compute_blended_score(
            similarity=0.8,
            base_importance=0.7,
            created_at=NOW - timedelta(days=60),
            decay_config={"rate_per_month": 0.01},
            now=NOW,
        )
        assert eff_fast < eff_slow


class TestRerankCandidates:
    """Batch re-ranking of query candidates."""

    def _make_candidate(self, similarity, importance, age_days=0, retrieved_days_ago=None):
        c = {
            "similarity": similarity,
            "base_importance": importance,
            "created_at": NOW - timedelta(days=age_days),
        }
        if retrieved_days_ago is not None:
            c["last_retrieved_at"] = NOW - timedelta(days=retrieved_days_ago)
        return c

    def test_sorted_by_blended_score(self):
        """Result is sorted by blended_score descending."""
        candidates = [
            self._make_candidate(0.5, 0.5),
            self._make_candidate(0.9, 0.9),
            self._make_candidate(0.7, 0.7),
        ]
        result = rerank_candidates(candidates, now=NOW)
        scores = [c["blended_score"] for c in result]
        assert scores == sorted(scores, reverse=True)

    def test_adds_blended_score_field(self):
        """Each candidate gets blended_score and effective_importance."""
        candidates = [self._make_candidate(0.8, 0.7)]
        result = rerank_candidates(candidates, now=NOW)
        assert "blended_score" in result[0]
        assert "effective_importance" in result[0]

    def test_decay_reorders_old_vs_new(self):
        """Old unaccessed memory ranks below newer one with lower base importance."""
        candidates = [
            self._make_candidate(0.8, 0.9, age_days=365),  # Old, high importance
            self._make_candidate(0.8, 0.7, age_days=1),    # New, lower importance
        ]
        result = rerank_candidates(candidates, now=NOW)
        # New memory should rank higher because old one decayed
        assert result[0]["base_importance"] == 0.7  # The newer one

    def test_retrieval_rescues_old_memory(self):
        """Recently retrieved old memory can outrank untouched new one."""
        candidates = [
            self._make_candidate(0.8, 0.7, age_days=180, retrieved_days_ago=1),
            self._make_candidate(0.8, 0.5, age_days=1),
        ]
        result = rerank_candidates(candidates, now=NOW)
        # Old but recently retrieved should rank higher
        assert result[0]["base_importance"] == 0.7

    def test_empty_candidates(self):
        """Empty list returns empty."""
        assert rerank_candidates([], now=NOW) == []

    def test_single_candidate(self):
        """Single candidate still gets scored."""
        result = rerank_candidates(
            [self._make_candidate(0.8, 0.7)], now=NOW
        )
        assert len(result) == 1
        assert "blended_score" in result[0]

    def test_iso_string_dates(self):
        """String dates are parsed correctly."""
        candidates = [{
            "similarity": 0.8,
            "base_importance": 0.7,
            "created_at": "2026-01-01T00:00:00Z",
        }]
        result = rerank_candidates(candidates, now=NOW)
        assert "blended_score" in result[0]

    def test_missing_dates_use_now(self):
        """Missing created_at defaults to now (no decay)."""
        candidates = [{"similarity": 0.8, "base_importance": 0.7}]
        result = rerank_candidates(candidates, now=NOW)
        # No decay applied when created_at defaults to now
        assert result[0]["effective_importance"] == pytest.approx(0.7, abs=0.01)

    def test_custom_weights(self):
        """Custom similarity/importance weights are applied."""
        candidates = [self._make_candidate(0.9, 0.3)]
        result = rerank_candidates(
            candidates,
            similarity_weight=0.5,
            importance_weight=0.5,
            now=NOW,
        )
        # 0.5 * 0.9 + 0.5 * 0.3 = 0.60
        assert result[0]["blended_score"] == pytest.approx(0.60, abs=0.02)
