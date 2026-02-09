"""Tests for query expansion module."""
import pytest
from tools.expansion import (
    expand_query,
    _extract_expansion_terms,
    _deduplicate_terms,
    DEFAULT_SYNONYMS,
    DEFAULT_INTENT_PATTERNS,
)


class TestExpandQuery:
    """Tests for the full expansion pipeline."""

    def test_synonym_expansion(self):
        result = expand_query("auth flow setup")
        assert result["enabled"] is True
        assert len(result["terms_added"]) > 0
        assert "authentication" in result["terms_added"] or "authorization" in result["terms_added"]
        assert result["expanded"] != result["original"]

    def test_no_match_returns_original(self):
        result = expand_query("quantum entanglement theory")
        assert result["expanded"] == result["original"]
        assert result["terms_added"] == []

    def test_disabled_passthrough(self):
        result = expand_query("auth flow", config={"enabled": False})
        assert result["enabled"] is False
        assert result["expanded"] == "auth flow"
        assert result["terms_added"] == []

    def test_intent_detection_how_to(self):
        result = expand_query("how to deploy services")
        assert result["intent"] == "how-to"
        assert any(t in result["terms_added"] for t in ["guide", "steps", "tutorial"])

    def test_intent_detection_rationale(self):
        result = expand_query("why did we choose PostgreSQL")
        assert result["intent"] == "rationale"

    def test_intent_detection_decision(self):
        result = expand_query("should we use Redis for caching")
        assert result["intent"] == "decision"
        assert any(t in result["terms_added"] for t in ["decision", "tradeoff", "recommendation"])

    def test_max_terms_limit(self):
        # "auth" + "db" + "how to" should generate many terms, but cap at max
        result = expand_query("how to auth with db", config={"max_expansion_terms": 3})
        assert len(result["terms_added"]) <= 3

    def test_deduplication_with_query(self):
        # "authentication" is in query, should not be added
        result = expand_query("auth authentication setup")
        assert "authentication" not in result["terms_added"]

    def test_custom_synonyms(self):
        config = {"synonyms": {"ml": ["machine-learning", "neural", "model"]}}
        result = expand_query("ml pipeline", config=config)
        assert "machine-learning" in result["terms_added"]

    def test_multiple_synonyms_combined(self):
        result = expand_query("api config changes")
        # Should have terms from both "api" and "config"
        assert len(result["terms_added"]) > 2


class TestExtractExpansionTerms:
    """Tests for term extraction logic."""

    def test_synonym_match(self):
        terms, intent = _extract_expansion_terms("db query", DEFAULT_SYNONYMS, DEFAULT_INTENT_PATTERNS)
        assert "database" in terms
        assert intent is None

    def test_intent_match(self):
        terms, intent = _extract_expansion_terms("how to test", DEFAULT_SYNONYMS, DEFAULT_INTENT_PATTERNS)
        assert intent == "how-to"

    def test_no_match(self):
        terms, intent = _extract_expansion_terms("hello world", DEFAULT_SYNONYMS, DEFAULT_INTENT_PATTERNS)
        assert terms == [] or all(t not in DEFAULT_SYNONYMS for t in terms)
        assert intent is None


class TestDeduplicateTerms:
    """Tests for term deduplication."""

    def test_removes_query_words(self):
        unique = _deduplicate_terms("database query", ["database", "postgres", "sql"])
        assert "database" not in unique
        assert "postgres" in unique

    def test_removes_duplicates(self):
        unique = _deduplicate_terms("test", ["a", "b", "a", "c", "b"])
        assert unique == ["a", "b", "c"]

    def test_case_insensitive(self):
        unique = _deduplicate_terms("Database", ["database", "postgres"])
        assert "database" not in unique
        assert "postgres" in unique
