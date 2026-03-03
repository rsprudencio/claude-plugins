"""Tests for tools/decay.py — time-based importance decay with retrieval reinforcement."""

import pytest
from datetime import datetime, timedelta, timezone

from tools.decay import compute_effective_importance


NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=timezone.utc)


class TestBasicDecay:
    """Importance decreases over time for unaccessed memories."""

    def test_no_decay_for_new_memory(self):
        """A memory created right now has ~full base importance."""
        result = compute_effective_importance(
            base_importance=0.8,
            created_at=NOW,
            now=NOW,
        )
        assert result == pytest.approx(0.8, abs=0.01)

    def test_decay_after_one_month(self):
        """After 1 month at 5% rate: 0.8 * 0.95 = 0.76."""
        created = NOW - timedelta(days=30)
        result = compute_effective_importance(
            base_importance=0.8,
            created_at=created,
            now=NOW,
        )
        assert result == pytest.approx(0.76, abs=0.01)

    def test_decay_after_six_months(self):
        """After 6 months: 0.8 * 0.95^6 ≈ 0.588."""
        created = NOW - timedelta(days=180)
        result = compute_effective_importance(
            base_importance=0.8,
            created_at=created,
            now=NOW,
        )
        assert result == pytest.approx(0.588, abs=0.02)

    def test_decay_after_one_year(self):
        """After 12 months: 0.8 * 0.95^12 ≈ 0.431."""
        created = NOW - timedelta(days=365)
        result = compute_effective_importance(
            base_importance=0.8,
            created_at=created,
            now=NOW,
        )
        assert result == pytest.approx(0.431, abs=0.02)

    def test_monotonic_decrease(self):
        """Importance strictly decreases with age (no retrieval)."""
        results = []
        for months in range(0, 13):
            created = NOW - timedelta(days=months * 30)
            r = compute_effective_importance(
                base_importance=0.8, created_at=created, now=NOW
            )
            results.append(r)
        for i in range(1, len(results)):
            assert results[i] < results[i - 1], f"Month {i} should be < month {i-1}"


class TestDecayFloor:
    """Effective importance never drops below min_importance."""

    def test_floor_enforced(self):
        """Very old memory never drops below floor."""
        ancient = NOW - timedelta(days=3650)  # 10 years
        result = compute_effective_importance(
            base_importance=0.5,
            created_at=ancient,
            min_importance=0.05,
            now=NOW,
        )
        assert result >= 0.05

    def test_custom_floor(self):
        """Custom floor is respected."""
        ancient = NOW - timedelta(days=3650)
        result = compute_effective_importance(
            base_importance=0.5,
            created_at=ancient,
            min_importance=0.1,
            now=NOW,
        )
        assert result >= 0.1

    def test_floor_zero(self):
        """Floor of 0 allows full decay."""
        ancient = NOW - timedelta(days=3650)
        result = compute_effective_importance(
            base_importance=0.5,
            created_at=ancient,
            min_importance=0.0,
            now=NOW,
        )
        assert result >= 0.0
        assert result < 0.05  # Should be very small


class TestRetrievalReinforcement:
    """Retrieval boost decays over time (EWMA-style, not cumulative)."""

    def test_recent_retrieval_boost(self):
        """Memory retrieved yesterday gets full boost."""
        created = NOW - timedelta(days=90)
        retrieved = NOW - timedelta(days=1)
        result = compute_effective_importance(
            base_importance=0.5,
            created_at=created,
            last_retrieved_at=retrieved,
            now=NOW,
        )
        # Without retrieval (baseline)
        baseline = compute_effective_importance(
            base_importance=0.5, created_at=created, now=NOW
        )
        assert result > baseline
        assert result - baseline == pytest.approx(0.15, abs=0.02)

    def test_retrieval_boost_halves_after_half_life(self):
        """Boost halves after retrieval_half_life_days."""
        created = NOW - timedelta(days=90)

        result_recent = compute_effective_importance(
            base_importance=0.5,
            created_at=created,
            last_retrieved_at=NOW,
            retrieval_half_life_days=30,
            now=NOW,
        )
        result_old = compute_effective_importance(
            base_importance=0.5,
            created_at=created,
            last_retrieved_at=NOW - timedelta(days=30),
            retrieval_half_life_days=30,
            now=NOW,
        )
        baseline = compute_effective_importance(
            base_importance=0.5, created_at=created, now=NOW
        )

        boost_recent = result_recent - baseline
        boost_old = result_old - baseline
        # boost_old should be ~half of boost_recent
        assert boost_old == pytest.approx(boost_recent / 2.0, abs=0.02)

    def test_old_retrieval_minimal_boost(self):
        """Retrieval from 90+ days ago provides negligible boost."""
        created = NOW - timedelta(days=180)
        retrieved = NOW - timedelta(days=90)
        result = compute_effective_importance(
            base_importance=0.5,
            created_at=created,
            last_retrieved_at=retrieved,
            now=NOW,
        )
        baseline = compute_effective_importance(
            base_importance=0.5, created_at=created, now=NOW
        )
        boost = result - baseline
        assert boost < 0.03  # Negligible after 3 half-lives

    def test_no_retrieval_no_boost(self):
        """None last_retrieved_at gives zero retrieval boost."""
        created = NOW - timedelta(days=30)
        with_retrieval = compute_effective_importance(
            base_importance=0.5,
            created_at=created,
            last_retrieved_at=NOW,
            now=NOW,
        )
        without_retrieval = compute_effective_importance(
            base_importance=0.5,
            created_at=created,
            last_retrieved_at=None,
            now=NOW,
        )
        assert with_retrieval > without_retrieval


class TestDecayRate:
    """Custom decay rates work correctly."""

    def test_zero_decay_rate(self):
        """Zero decay rate means no decay (importance stays constant)."""
        old = NOW - timedelta(days=365)
        result = compute_effective_importance(
            base_importance=0.8,
            created_at=old,
            decay_rate=0.0,
            now=NOW,
        )
        assert result == pytest.approx(0.8, abs=0.001)

    def test_high_decay_rate(self):
        """High decay rate causes rapid decline."""
        old = NOW - timedelta(days=30)
        result = compute_effective_importance(
            base_importance=0.8,
            created_at=old,
            decay_rate=0.2,  # 20% per month
            now=NOW,
        )
        assert result == pytest.approx(0.64, abs=0.02)  # 0.8 * 0.8


class TestCeiling:
    """Effective importance capped at 1.0."""

    def test_high_importance_plus_retrieval(self):
        """Even with high base + retrieval boost, can't exceed 1.0."""
        result = compute_effective_importance(
            base_importance=0.95,
            created_at=NOW,
            last_retrieved_at=NOW,
            retrieval_boost_max=0.15,
            now=NOW,
        )
        assert result <= 1.0


class TestTimezoneHandling:
    """Timezone-naive datetimes are treated as UTC."""

    def test_naive_created_at(self):
        """Naive datetime works (assumes UTC)."""
        naive = datetime(2026, 2, 1, 12, 0, 0)
        result = compute_effective_importance(
            base_importance=0.8,
            created_at=naive,
            now=NOW,
        )
        assert 0.05 <= result <= 1.0

    def test_naive_retrieved_at(self):
        """Naive last_retrieved_at works."""
        result = compute_effective_importance(
            base_importance=0.5,
            created_at=NOW - timedelta(days=30),
            last_retrieved_at=datetime(2026, 2, 28, 12, 0, 0),
            now=NOW,
        )
        assert 0.05 <= result <= 1.0
