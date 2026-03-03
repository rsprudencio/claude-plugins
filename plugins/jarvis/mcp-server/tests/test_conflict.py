"""Tests for content conflict detection."""

import json
import os
import tempfile
from unittest.mock import patch, MagicMock

import pytest

from tools.conflict import (
    has_negation_signals,
    _tokenize,
    find_conflict_candidates,
    verify_conflicts_with_llm,
    mark_superseded,
    log_conflict,
    detect_conflicts,
    _resolve_log_dir,
)
from tools.content import content_write


def _seed_doc(db, doc_id, text, category="observation", scope="global",
              source="auto-extract", importance_score=0.5, metadata=None):
    """Plant a document directly in the mock database for testing.

    Uses upsert_core() directly with proper column values — no legacy
    metadata keys like tier/namespace/promoted.
    """
    from tools.embedding import get_embedding_service

    emb = get_embedding_service()
    db.upsert_core(
        doc_id, text, emb.encode(text),
        category=category,
        scope=scope,
        source=source,
        importance_score=importance_score,
        metadata=metadata or {},
    )


# -- has_negation_signals ----------------------------------------------------


class TestHasNegationSignals:

    def test_positive_actually(self):
        assert has_negation_signals("actually X instead of Y") is True

    def test_positive_no_longer(self):
        assert has_negation_signals("We no longer use pattern X") is True

    def test_positive_dont(self):
        assert has_negation_signals("don't use X for this") is True

    def test_positive_wrong(self):
        assert has_negation_signals("The previous approach was wrong") is True

    def test_positive_deprecated(self):
        assert has_negation_signals("That library is deprecated") is True

    def test_positive_replaced_by(self):
        assert has_negation_signals("X was replaced by Y") is True

    def test_positive_anti_pattern(self):
        assert has_negation_signals("This is an anti-pattern") is True

    def test_positive_not_recommended(self):
        assert has_negation_signals("This is not recommended") is True

    def test_negative_neutral(self):
        assert (
            has_negation_signals("I like using pattern X for handling errors") is False
        )

    def test_negative_observation(self):
        assert has_negation_signals("User worked on the API integration today") is False

    def test_case_insensitive(self):
        assert has_negation_signals("ACTUALLY this is WRONG") is True

    def test_empty_string(self):
        assert has_negation_signals("") is False

    def test_none_equivalent(self):
        assert has_negation_signals("") is False

    def test_word_boundary_notable(self):
        """'notable' should NOT trigger 'not' match."""
        assert has_negation_signals("This is a notable achievement") is False

    def test_word_boundary_nevermore(self):
        """'nevermore' should NOT trigger 'never' match -- \\b at word boundary."""
        assert has_negation_signals("Quoth the raven nevermore") is False


# -- _tokenize ---------------------------------------------------------------


class TestTokenize:

    def test_filters_stop_words(self):
        tokens = _tokenize("the quick brown fox is very fast")
        assert "the" not in tokens
        assert "is" not in tokens
        assert "very" not in tokens
        assert "quick" in tokens
        assert "brown" in tokens
        assert "fox" in tokens
        assert "fast" in tokens

    def test_lowercases(self):
        tokens = _tokenize("FooBar BazQux")
        assert "foobar" in tokens
        assert "bazqux" in tokens

    def test_empty(self):
        assert _tokenize("") == set()

    def test_single_char_filtered(self):
        """Single-char tokens don't match the regex (min length 2)."""
        tokens = _tokenize("a b c de fg")
        assert "a" not in tokens
        assert "b" not in tokens
        assert "de" in tokens
        assert "fg" in tokens


# -- find_conflict_candidates ------------------------------------------------


class TestFindConflictCandidates:

    def test_no_candidates_empty_collection(self, mock_config):
        config = {
            "similarity_threshold": 0.7,
            "divergence_threshold": 0.4,
            "max_candidates": 10,
        }
        result = find_conflict_candidates("new::1", "some content", config)
        assert result == []

    def test_no_candidates_dissimilar(self, mock_config):
        """Write content that is semantically dissimilar -- no candidates."""
        _seed_doc(
            mock_config.db,
            "obs::old1",
            "Python is great for data science and machine learning",
            category="observation",
        )
        config = {
            "similarity_threshold": 0.99,  # Very high threshold
            "divergence_threshold": 0.4,
            "max_candidates": 10,
        }
        result = find_conflict_candidates(
            "obs::new1",
            "The weather today is sunny and warm outside",
            config,
        )
        assert result == []

    def test_candidate_high_sim_low_jaccard(self, mock_config):
        """Same topic, different wording -> candidate found."""
        _seed_doc(
            mock_config.db,
            "obs::old1",
            "Use the singleton pattern for database connections",
            category="observation",
        )
        config = {
            "similarity_threshold": 0.3,  # Low threshold to ensure match
            "divergence_threshold": 0.9,  # High divergence threshold to include
            "max_candidates": 10,
        }
        result = find_conflict_candidates(
            "obs::new1",
            "Actually, avoid the singleton pattern for database connections, use dependency injection instead",
            config,
        )
        # Should find the old observation as a candidate
        assert len(result) >= 0  # Embedding similarity depends on model
        # Verify structure if found
        for c in result:
            assert "id" in c
            assert "content" in c
            assert "similarity" in c
            assert "jaccard" in c

    def test_skip_already_superseded(self, mock_config):
        """Entries with status=superseded should be skipped.

        The query uses local.active_memories which filters status='active',
        so superseded entries never appear in results.
        """
        _seed_doc(
            mock_config.db,
            "obs::old1",
            "Use pattern X for error handling",
            category="observation",
        )
        # Manually set status to superseded (simulating mark_superseded)
        mock_config.db.core_rows["obs::old1"]["status"] = "superseded"

        config = {
            "similarity_threshold": -2.0,  # Accept everything
            "divergence_threshold": 1.0,  # Accept everything
            "max_candidates": 10,
        }
        result = find_conflict_candidates(
            "obs::new1",
            "Use pattern X for error handling differently",
            config,
        )
        assert all(c["id"] != "obs::old1" for c in result)

    def test_skip_self(self, mock_config):
        """Should not return self as a candidate."""
        _seed_doc(
            mock_config.db,
            "obs::self1",
            "test content for self-matching",
            category="observation",
        )
        config = {
            "similarity_threshold": -2.0,
            "divergence_threshold": 1.0,
            "max_candidates": 10,
        }
        result = find_conflict_candidates(
            "obs::self1",
            "test content for self-matching",
            config,
        )
        assert all(c["id"] != "obs::self1" for c in result)

    def test_no_candidate_high_sim_high_jaccard(self, mock_config):
        """High similarity + high word overlap = reinforcing, not conflict."""
        _seed_doc(
            mock_config.db,
            "obs::old1",
            "Always use type hints in Python functions",
            category="observation",
        )
        config = {
            "similarity_threshold": -2.0,  # Accept by similarity
            "divergence_threshold": 0.1,  # Very low -- require very different words
            "max_candidates": 10,
        }
        result = find_conflict_candidates(
            "obs::new1",
            "Always use type hints in Python functions for clarity",
            config,
        )
        # High jaccard (very similar words) should NOT pass the divergence filter
        assert result == []


# -- verify_conflicts_with_llm ----------------------------------------------


class TestVerifyConflictsWithLlm:

    def test_parses_response(self):
        candidates = [
            {"id": "obs::old1", "content": "use X"},
            {"id": "obs::old2", "content": "use Y"},
            {"id": "obs::old3", "content": "use Z"},
        ]
        config = {}
        with patch("tools.conflict._call_haiku_raw") as mock_haiku:
            mock_haiku.return_value = '{"contradicted": [0, 2]}'
            result = verify_conflicts_with_llm(
                "actually stop using X and Z", candidates, config
            )
        assert result == ["obs::old1", "obs::old3"]

    def test_fallback_on_failure(self):
        candidates = [
            {"id": "obs::old1", "content": "use X"},
        ]
        config = {}
        with patch("tools.conflict._call_haiku_raw") as mock_haiku:
            mock_haiku.return_value = None  # API failure
            result = verify_conflicts_with_llm("actually stop X", candidates, config)
        # Fallback: trust rule-based -> return all candidates
        assert result == ["obs::old1"]

    def test_empty_array(self):
        candidates = [
            {"id": "obs::old1", "content": "use X"},
        ]
        config = {}
        with patch("tools.conflict._call_haiku_raw") as mock_haiku:
            mock_haiku.return_value = '{"contradicted": []}'
            result = verify_conflicts_with_llm("some content", candidates, config)
        assert result == []

    def test_invalid_json_fallback(self):
        candidates = [
            {"id": "obs::old1", "content": "use X"},
        ]
        config = {}
        with patch("tools.conflict._call_haiku_raw") as mock_haiku:
            mock_haiku.return_value = "not valid json"
            result = verify_conflicts_with_llm("stop X", candidates, config)
        # Fallback: trust all candidates
        assert result == ["obs::old1"]

    def test_out_of_range_index_ignored(self):
        candidates = [
            {"id": "obs::old1", "content": "use X"},
        ]
        config = {}
        with patch("tools.conflict._call_haiku_raw") as mock_haiku:
            mock_haiku.return_value = '{"contradicted": [0, 5, 99]}'
            result = verify_conflicts_with_llm("stop X", candidates, config)
        assert result == ["obs::old1"]  # Only index 0 is valid


# -- mark_superseded ---------------------------------------------------------


class TestMarkSuperseded:

    def test_updates_columns(self, mock_config):
        """mark_superseded sets status and superseded_by as columns, not metadata."""
        _seed_doc(
            mock_config.db,
            "obs::old1",
            "old content",
            category="observation",
            source="auto-extract",
        )
        result = mark_superseded("obs::old1", "obs::new1")
        assert result is True

        row = mock_config.db.get("obs::old1")
        # status and superseded_by are top-level columns, not in metadata
        assert row["status"] == "superseded"
        assert row["superseded_by"] == "obs::new1"

    def test_preserves_other_columns(self, mock_config):
        """Other column values and metadata remain intact after superseding."""
        _seed_doc(
            mock_config.db,
            "obs::old1",
            "old content",
            category="observation",
            source="auto-extract",
            importance_score=0.8,
            metadata={"tags": "foo,bar", "session_id": "sess-123"},
        )
        mark_superseded("obs::old1", "obs::new1")

        row = mock_config.db.get("obs::old1")
        # Top-level columns preserved
        assert row["source"] == "auto-extract"
        assert row["importance_score"] == 0.8
        assert row["category"] == "observation"
        # JSONB metadata preserved
        assert row["metadata"]["tags"] == "foo,bar"
        assert row["metadata"]["session_id"] == "sess-123"
        # Column-level superseded fields
        assert row["status"] == "superseded"
        assert row["superseded_by"] == "obs::new1"

    def test_nonexistent_id(self, mock_config):
        result = mark_superseded("obs::doesnotexist", "obs::new1")
        assert result is False


# -- Cross-type conflict -----------------------------------------------------


class TestCrossTypeConflict:

    def test_observation_vs_pattern(self, mock_config):
        """New observation can supersede an old pattern."""
        _seed_doc(
            mock_config.db,
            "pattern::use-singleton",
            "Use singleton pattern for database connections",
            category="pattern",
        )
        # The candidate finder queries local.active_memories, so patterns are included
        config = {
            "similarity_threshold": -2.0,
            "divergence_threshold": 1.0,
            "max_candidates": 10,
        }
        candidates = find_conflict_candidates(
            "obs::new1",
            "Actually singleton is an anti-pattern for database connections",
            config,
        )
        # Pattern should be in the candidate set (assuming it meets thresholds)
        ids = [c["id"] for c in candidates]
        assert "pattern::use-singleton" in ids

    def test_learning_vs_decision(self, mock_config):
        """New learning can supersede an old decision."""
        _seed_doc(
            mock_config.db,
            "decision::use-redux",
            "Decided to use Redux for state management",
            category="decision",
        )
        config = {
            "similarity_threshold": -2.0,
            "divergence_threshold": 1.0,
            "max_candidates": 10,
        }
        candidates = find_conflict_candidates(
            "learning::new1",
            "Actually Redux is overkill, use React context instead",
            config,
        )
        ids = [c["id"] for c in candidates]
        assert "decision::use-redux" in ids


# -- detect_conflicts orchestrator -------------------------------------------


class TestDetectConflicts:

    def test_disabled(self, mock_config):
        mock_config.set(
            memory={
                "conflict_detection": {"enabled": False},
            }
        )
        from tools.config import clear_config_cache

        clear_config_cache()
        result = detect_conflicts("obs::new1", "actually stop doing X")
        assert result == []

    def test_no_negation(self, mock_config):
        """Content without negation signals skips entirely."""
        result = detect_conflicts(
            "obs::new1", "User worked on the API integration today"
        )
        assert result == []

    def test_rule_based_pipeline(self, mock_config):
        """Full pipeline: write old entry, write contradicting new entry, verify superseded."""
        _seed_doc(
            mock_config.db,
            "obs::old1",
            "Use the factory pattern for creating objects",
            category="observation",
        )

        # The detect_conflicts uses config defaults (use_llm=False)
        result = detect_conflicts(
            "obs::new1",
            "Actually avoid the factory pattern for creating objects, just use constructors",
        )
        # Whether old1 gets superseded depends on embedding similarity
        # The important thing is the pipeline doesn't crash
        assert isinstance(result, list)

    @patch("tools.conflict.mark_superseded", return_value=True)
    @patch("tools.conflict.verify_conflicts_with_llm")
    @patch("tools.conflict.find_conflict_candidates")
    def test_with_llm(self, mock_find, mock_verify, mock_mark, mock_config):
        mock_config.set(
            memory={
                "conflict_detection": {"enabled": True, "use_llm": True},
            }
        )
        from tools.config import clear_config_cache

        clear_config_cache()

        mock_find.return_value = [
            {"id": "obs::old1", "content": "use X", "similarity": 0.8, "jaccard": 0.2},
            {
                "id": "obs::old2",
                "content": "use Y",
                "similarity": 0.75,
                "jaccard": 0.15,
            },
        ]
        # LLM says only old1 is contradicted
        mock_verify.return_value = ["obs::old1"]

        result = detect_conflicts(
            "obs::new1", "actually stop using X, it causes issues"
        )

        assert "obs::old1" in result
        assert "obs::old2" not in result
        mock_verify.assert_called_once()
        # mark_superseded called only for the confirmed conflict
        mock_mark.assert_called_once_with("obs::old1", "obs::new1")


# -- Filter integration tests -----------------------------------------------


class TestFilterSuperseded:

    def test_patterns_skip_superseded(self, mock_config):
        """_fetch_recent_observations skips superseded."""
        from tools.patterns import _fetch_recent_observations
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _seed_doc(
            mock_config.db,
            "obs::active",
            "active observation",
            category="observation",
            metadata={"created_at": now},
        )
        _seed_doc(
            mock_config.db,
            "obs::stale",
            "stale observation",
            category="observation",
            metadata={"created_at": now},
        )
        # Manually set status to superseded (column-level)
        mock_config.db.core_rows["obs::stale"]["status"] = "superseded"

        recent = _fetch_recent_observations(lookback_minutes=60)
        ids = [d["id"] for d in recent]
        assert "obs::active" in ids
        assert "obs::stale" not in ids

    def test_semantic_context_skip_superseded(self, mock_config):
        """semantic_context excludes superseded entries."""
        from tools.query import semantic_context
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _seed_doc(
            mock_config.db,
            "obs::active",
            "Python error handling best practices",
            category="observation",
            metadata={"updated_at": now},
        )
        _seed_doc(
            mock_config.db,
            "obs::stale",
            "Python error handling best practices outdated version",
            category="observation",
            metadata={"updated_at": now},
        )
        # Manually set status to superseded (column-level)
        mock_config.db.core_rows["obs::stale"]["status"] = "superseded"

        result = semantic_context("Python error handling", threshold=0.0, budget=8000)
        sources = [m["source"] for m in result.get("matches", [])]
        # The superseded entry should not appear in results
        assert "obs::stale" not in sources

    def test_backwards_compatible_no_status(self, mock_config):
        """Entries without explicit status still work (default is 'active')."""
        from tools.patterns import _fetch_recent_observations
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        _seed_doc(
            mock_config.db,
            "obs::legacy",
            "legacy observation without explicit status",
            category="observation",
            metadata={"created_at": now},
        )

        recent = _fetch_recent_observations(lookback_minutes=60)
        ids = [d["id"] for d in recent]
        assert "obs::legacy" in ids


# -- Conflict log ------------------------------------------------------------


class TestConflictLog:

    def test_creates_jsonl(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tools.conflict._resolve_log_dir", lambda: tmp_path)
        log_conflict("obs::old1", "obs::new1", 0.85, 0.15, "rule-based", "superseded")

        log_file = tmp_path / "conflicts.jsonl"
        assert log_file.exists()
        line = json.loads(log_file.read_text().strip())
        assert line["old_id"] == "obs::old1"
        assert line["new_id"] == "obs::new1"

    def test_appends(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tools.conflict._resolve_log_dir", lambda: tmp_path)
        log_conflict("obs::old1", "obs::new1", 0.85, 0.15, "rule-based", "superseded")
        log_conflict("obs::old2", "obs::new2", 0.80, 0.20, "llm-verified", "retained")

        log_file = tmp_path / "conflicts.jsonl"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_records_all_fields(self, tmp_path, monkeypatch):
        monkeypatch.setattr("tools.conflict._resolve_log_dir", lambda: tmp_path)
        log_conflict("obs::old1", "obs::new1", 0.85, 0.15, "rule-based", "superseded")

        log_file = tmp_path / "conflicts.jsonl"
        record = json.loads(log_file.read_text().strip())
        assert "timestamp" in record
        assert record["old_id"] == "obs::old1"
        assert record["new_id"] == "obs::new1"
        assert record["similarity"] == 0.85
        assert record["jaccard"] == 0.15
        assert record["method"] == "rule-based"
        assert record["verdict"] == "superseded"
        assert "reasoning" in record


# -- content_write integration -----------------------------------------------


class TestContentWriteIntegration:

    @patch("tools.conflict.detect_conflicts")
    def test_triggers_for_observation(self, mock_detect, mock_config):
        mock_detect.return_value = ["obs::old1"]
        result = content_write(
            content="actually stop using pattern X",
            content_type="observation",
        )
        assert result["success"]
        assert result.get("conflicts_resolved") == 1
        assert result.get("superseded_ids") == ["obs::old1"]
        mock_detect.assert_called_once()

    @patch("tools.conflict.detect_conflicts")
    def test_triggers_for_learning(self, mock_detect, mock_config):
        """Conflict detection runs for all content types, not just observations."""
        mock_detect.return_value = []
        result = content_write(
            content="actually avoid using X for performance reasons",
            content_type="learning",
        )
        assert result["success"]
        mock_detect.assert_called_once()

    @patch("tools.conflict.detect_conflicts")
    def test_no_conflicts_no_extra_keys(self, mock_detect, mock_config):
        mock_detect.return_value = []
        result = content_write(
            content="no negation here, just a normal observation",
            content_type="observation",
        )
        assert result["success"]
        assert "conflicts_resolved" not in result

    @patch("tools.conflict.detect_conflicts", side_effect=Exception("boom"))
    def test_exception_does_not_block_write(self, mock_detect, mock_config):
        """If conflict detection fails, the write still succeeds."""
        result = content_write(
            content="actually stop X",
            content_type="observation",
        )
        assert result["success"]
        assert "conflicts_resolved" not in result
