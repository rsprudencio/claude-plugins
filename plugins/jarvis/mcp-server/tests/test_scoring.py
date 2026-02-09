"""Tests for importance scoring module."""
import math
import pytest
from datetime import datetime, timezone, timedelta
from tools.scoring import (
    compute_importance,
    _compute_type_weight,
    _compute_concept_bonus,
    _compute_recency_bonus,
    _compute_retrieval_bonus,
    _parse_frontmatter_importance,
    DEFAULT_TYPE_WEIGHTS,
)


class TestTypeWeight:
    """Tests for vault type weight lookup."""

    def test_known_types(self):
        assert _compute_type_weight("journal", {}) == 0.65
        assert _compute_type_weight("note", {}) == 0.55
        assert _compute_type_weight("work", {}) == 0.60
        assert _compute_type_weight("inbox", {}) == 0.3
        assert _compute_type_weight("incident-log", {}) == 0.7
        assert _compute_type_weight("decision", {}) == 0.8

    def test_unknown_type_uses_default(self):
        assert _compute_type_weight("random-type", {}) == 0.5

    def test_config_override(self):
        custom = {"journal": 0.9, "custom": 0.75}
        assert _compute_type_weight("journal", custom) == 0.9
        assert _compute_type_weight("custom", custom) == 0.75
        # Non-overridden type uses default
        assert _compute_type_weight("note", custom) == 0.55


class TestConceptBonus:
    """Tests for concept pattern matching bonus."""

    def test_decision_pattern(self):
        bonus = _compute_concept_bonus("We made a decision to use PostgreSQL", {})
        assert bonus == 0.1

    def test_incident_pattern(self):
        bonus = _compute_concept_bonus("Major outage in production cluster", {})
        assert bonus == 0.15

    def test_todo_pattern(self):
        bonus = _compute_concept_bonus("TODO: refactor this module", {})
        assert bonus == 0.05

    def test_multiple_patterns_sum(self):
        # decision (0.1) + TODO (0.05) = 0.15
        bonus = _compute_concept_bonus("Decision: TODO implement auth", {})
        assert abs(bonus - 0.15) < 1e-10

    def test_bonus_capped_at_02(self):
        # incident (0.15) + decision (0.1) + TODO (0.05) = 0.3 -> capped at 0.2
        text = "Incident postmortem: decision to FIXME the architecture"
        bonus = _compute_concept_bonus(text, {})
        assert bonus == 0.2

    def test_no_match(self):
        bonus = _compute_concept_bonus("Just a regular note about cooking", {})
        assert bonus == 0.0

    def test_case_insensitive(self):
        bonus = _compute_concept_bonus("ARCHITECTURE design patterns", {})
        assert bonus == 0.1

    def test_custom_patterns(self):
        custom = {r"\brecipe\b": 0.12}
        bonus = _compute_concept_bonus("My best recipe for pasta", custom)
        assert bonus == 0.12


class TestRecencyBonus:
    """Tests for exponential decay recency bonus."""

    def test_very_recent_max_bonus(self):
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        bonus = _compute_recency_bonus(now_iso, 7.0)
        assert 0.09 <= bonus <= 0.1  # Close to max

    def test_one_halflife_gives_half_bonus(self):
        one_week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")
        bonus = _compute_recency_bonus(one_week_ago, 7.0)
        assert 0.04 <= bonus <= 0.06  # ~0.05 (half of 0.1)

    def test_very_old_near_zero(self):
        old = "2020-01-01T00:00:00Z"
        bonus = _compute_recency_bonus(old, 7.0)
        assert bonus < 0.001

    def test_none_returns_zero(self):
        assert _compute_recency_bonus(None, 7.0) == 0.0

    def test_invalid_date_returns_zero(self):
        assert _compute_recency_bonus("not-a-date", 7.0) == 0.0

    def test_zero_halflife_returns_zero(self):
        now_iso = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        assert _compute_recency_bonus(now_iso, 0.0) == 0.0


class TestRetrievalBonus:
    """Tests for log-scaled retrieval bonus."""

    def test_zero_retrievals(self):
        assert _compute_retrieval_bonus(0) == 0.0

    def test_negative_retrievals(self):
        assert _compute_retrieval_bonus(-1) == 0.0

    def test_one_retrieval(self):
        bonus = _compute_retrieval_bonus(1)
        assert bonus == round(math.log2(2) / 10, 4)  # 0.1

    def test_moderate_retrievals(self):
        bonus = _compute_retrieval_bonus(7)  # log2(8)/10 = 0.3/10 = 0.03
        assert 0.0 < bonus <= 0.1

    def test_capped_at_max(self):
        # Even huge retrieval counts cap at 0.1
        assert _compute_retrieval_bonus(10000) == 0.1


class TestFrontmatterImportance:
    """Tests for frontmatter importance parsing."""

    def test_numeric_string(self):
        assert _parse_frontmatter_importance("0.8") == 0.8

    def test_numeric_clamped_high(self):
        assert _parse_frontmatter_importance("1.5") == 1.0

    def test_numeric_clamped_low(self):
        assert _parse_frontmatter_importance("-0.5") == 0.0

    def test_named_high(self):
        assert _parse_frontmatter_importance("high") == 0.8

    def test_named_critical(self):
        assert _parse_frontmatter_importance("critical") == 0.95

    def test_named_medium(self):
        assert _parse_frontmatter_importance("medium") == 0.5

    def test_named_low(self):
        assert _parse_frontmatter_importance("low") == 0.3

    def test_none_returns_none(self):
        assert _parse_frontmatter_importance(None) is None

    def test_unrecognized_returns_none(self):
        assert _parse_frontmatter_importance("banana") is None

    def test_case_insensitive_named(self):
        assert _parse_frontmatter_importance("HIGH") == 0.8
        assert _parse_frontmatter_importance("Critical") == 0.95


class TestComputeImportance:
    """Integration tests for the full scoring function."""

    def test_basic_note(self):
        score = compute_importance("Some note content", vault_type="note")
        assert 0.5 <= score <= 0.8  # base 0.6 + possible bonuses

    def test_frontmatter_overrides_type_weight(self):
        score = compute_importance(
            "Simple content",
            vault_type="inbox",  # 0.3 base
            frontmatter_importance="0.9",
        )
        assert score >= 0.9  # Uses frontmatter, not inbox weight

    def test_concept_bonus_adds_to_score(self):
        plain = compute_importance("Normal content", vault_type="note")
        decision = compute_importance("Architecture decision for auth", vault_type="note")
        assert decision > plain

    def test_score_clamped_to_one(self):
        # Everything at max
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        score = compute_importance(
            "Incident outage decision architecture TODO FIXME",
            vault_type="decision",
            frontmatter_importance="0.95",
            created_at=now,
            retrieval_count=10000,
        )
        assert score == 1.0

    def test_score_clamped_to_zero(self):
        # Custom config with 0 type weight, no patterns
        score = compute_importance(
            "plain text",
            vault_type="unknown",
            config={"type_weights": {"unknown": 0.0}, "concept_patterns": {}},
        )
        assert score >= 0.0

    def test_config_overrides_defaults(self):
        custom = {
            "type_weights": {"note": 0.99},
            "concept_patterns": {},
        }
        score = compute_importance("Plain note", vault_type="note", config=custom)
        assert score >= 0.99
