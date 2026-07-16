"""Tests for tools/ranking.py — unified retrieval scoring (Layer 4).

One formula for every schema:

    score = similarity + importance_weight * (effective_importance - 0.5)

No clamp, importance is an additive nudge, memories get decay-adjusted
importance via score_memory().
"""

import pytest
from datetime import datetime, timedelta, timezone

from tools.ranking import (
    DEFAULT_IMPORTANCE_WEIGHT,
    _parse_datetime,
    compute_unified_score,
    score_memory,
)


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


class TestComputeUnifiedScore:
    """score = similarity + importance_weight * (effective_importance - 0.5)."""

    def test_neutral_importance_returns_similarity(self):
        """Importance 0.5 is neutral — score equals raw similarity."""
        assert compute_unified_score(0.87, 0.5) == pytest.approx(0.87)

    def test_high_importance_nudges_up(self):
        # 0.8 + 0.24 * (1.0 - 0.5) = 0.92
        assert compute_unified_score(0.8, 1.0) == pytest.approx(0.92)

    def test_low_importance_nudges_down(self):
        # 0.8 + 0.24 * (0.0 - 0.5) = 0.68
        assert compute_unified_score(0.8, 0.0) == pytest.approx(0.68)

    def test_no_clamp_above_one(self):
        """Scores above 1.0 are preserved — the old clamp pinned saturated
        chunks in an arbitrary tie at exactly 1.0."""
        score = compute_unified_score(1.0, 1.0)
        assert score == pytest.approx(1.12)
        assert score > 1.0

    def test_no_clamp_below_zero(self):
        score = compute_unified_score(0.05, 0.0)
        assert score == pytest.approx(-0.07)

    def test_custom_importance_weight(self):
        # 0.8 + 0.5 * (0.9 - 0.5) = 1.0
        assert compute_unified_score(0.8, 0.9, importance_weight=0.5) == pytest.approx(1.0)

    def test_default_weight_constant(self):
        assert DEFAULT_IMPORTANCE_WEIGHT == pytest.approx(0.24)

    def test_ordering_dominated_by_similarity(self):
        """The importance nudge (±0.12 max) can never override a similarity
        gap larger than importance_weight."""
        strong_match_low_imp = compute_unified_score(0.9, 0.0)
        weak_match_high_imp = compute_unified_score(0.65, 1.0)
        assert strong_match_low_imp > weak_match_high_imp


class TestScoreMemory:
    """Memory scoring: decay-adjusted importance through the unified formula."""

    def test_fresh_memory_uses_base_importance(self):
        score, eff_imp = score_memory(
            similarity=0.9,
            base_importance=0.8,
            created_at=NOW,
            now=NOW,
        )
        assert eff_imp == pytest.approx(0.8, abs=0.01)
        # 0.9 + 0.24 * (0.8 - 0.5) = 0.972
        assert score == pytest.approx(0.972, abs=0.01)

    def test_decay_reduces_old_memory(self):
        """Old memory gets lower score than new one with same base importance."""
        score_new, _ = score_memory(
            similarity=0.8, base_importance=0.7, created_at=NOW, now=NOW
        )
        score_old, _ = score_memory(
            similarity=0.8,
            base_importance=0.7,
            created_at=NOW - timedelta(days=180),
            now=NOW,
        )
        assert score_new > score_old

    def test_retrieval_boosts_old_memory(self):
        """Recently retrieved old memory scores higher than untouched old one."""
        created = NOW - timedelta(days=180)

        score_retrieved, _ = score_memory(
            similarity=0.8,
            base_importance=0.7,
            created_at=created,
            last_retrieved_at=NOW - timedelta(days=1),
            retrieval_count=3,
            now=NOW,
        )
        score_untouched, _ = score_memory(
            similarity=0.8,
            base_importance=0.7,
            created_at=created,
            now=NOW,
        )
        assert score_retrieved > score_untouched

    def test_decay_config_passed_through(self):
        """Custom decay config affects effective importance."""
        _, eff_fast = score_memory(
            similarity=0.8,
            base_importance=0.7,
            created_at=NOW - timedelta(days=60),
            decay_config={"rate_per_month": 0.2},
            now=NOW,
        )
        _, eff_slow = score_memory(
            similarity=0.8,
            base_importance=0.7,
            created_at=NOW - timedelta(days=60),
            decay_config={"rate_per_month": 0.01},
            now=NOW,
        )
        assert eff_fast < eff_slow

    def test_custom_importance_weight(self):
        score, _ = score_memory(
            similarity=0.9,
            base_importance=1.0,
            created_at=NOW,
            importance_weight=0.1,
            now=NOW,
        )
        # eff_imp ≈ 1.0 → 0.9 + 0.1 * 0.5 = 0.95
        assert score == pytest.approx(0.95, abs=0.01)


class TestCrossSchemaCommensurability:
    """Regression guards for defect #6 — memories and vault chunks now share
    one scale, so a better-matching memory always outranks a worse-matching
    chunk (passage-ranking-redesign.md, Layer 4)."""

    def test_perfect_memory_outranks_saturated_chunk(self):
        """The headline defect: under the old formulas a perfect-match memory
        capped at 0.94 while a clamped vault chunk reached 1.0 — a memory
        could NEVER win. Under unified scoring the memory wins.

        Old world: memory = 0.7*1.0 + 0.3*0.8 = 0.94
                   chunk  = min(1.0, 0.95 + (0.9-0.5)*0.24 + 0.08) = 1.0  → chunk wins
        New world: memory = 1.0 + 0.24*(0.8-0.5)  = 1.072
                   chunk  = 0.95 + 0.24*(0.9-0.5) = 1.046             → memory wins
        """
        memory_score, _ = score_memory(
            similarity=1.0,
            base_importance=0.8,
            created_at=NOW,
            now=NOW,
        )
        chunk_score = compute_unified_score(0.95, 0.9)
        assert memory_score > chunk_score

    def test_equal_similarity_equal_importance_ties(self):
        """Same similarity + same (effective) importance → same score,
        regardless of schema."""
        memory_score, _ = score_memory(
            similarity=0.9, base_importance=0.7, created_at=NOW, now=NOW
        )
        chunk_score = compute_unified_score(0.9, 0.7)
        assert memory_score == pytest.approx(chunk_score, abs=0.005)

    def test_importance_lifts_both_schemas_identically(self):
        """Under the old formulas the same importance value lifted a chunk and
        dragged a memory. Now the nudge is identical for both."""
        base_chunk = compute_unified_score(0.8, 0.5)
        boosted_chunk = compute_unified_score(0.8, 0.9)
        chunk_lift = boosted_chunk - base_chunk

        base_mem, _ = score_memory(
            similarity=0.8, base_importance=0.5, created_at=NOW, now=NOW
        )
        boosted_mem, _ = score_memory(
            similarity=0.8, base_importance=0.9, created_at=NOW, now=NOW
        )
        mem_lift = boosted_mem - base_mem

        assert chunk_lift > 0
        assert mem_lift > 0
        assert chunk_lift == pytest.approx(mem_lift, abs=0.01)

    def test_no_saturation_ties_at_top(self):
        """Distinct high similarities stay distinct — the old clamp collapsed
        517 real chunks into an arbitrary tie at exactly 1.0000."""
        scores = [
            compute_unified_score(sim, 0.9) for sim in (0.99, 0.97, 0.95, 0.93)
        ]
        assert len(set(scores)) == len(scores)
        assert scores == sorted(scores, reverse=True)
