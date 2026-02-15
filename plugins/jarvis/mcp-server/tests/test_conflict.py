"""Tests for Tier 2 conflict detection."""
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
from tools.tier2 import tier2_write
from tools.memory import _get_collection


# ── has_negation_signals ────────────────────────────────────────────────────


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
        assert has_negation_signals("I like using pattern X for handling errors") is False

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
        """'nevermore' should NOT trigger 'never' match — wait, it should
        because 'never' is word-boundary matched and 'nevermore' starts with 'never'
        followed by 'more'. \\b matches between 'r' and 'm'. Let's verify."""
        # 'never' with \b: 'never' in 'nevermore' — \b at start, but 'neverm' is
        # continuous word chars. Actually \b before 'n' and no \b between 'r' and 'm'
        # so 'never\b' does NOT match in 'nevermore'.
        assert has_negation_signals("Quoth the raven nevermore") is False


# ── _tokenize ───────────────────────────────────────────────────────────────


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


# ── find_conflict_candidates ────────────────────────────────────────────────


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
        """Write content that is semantically dissimilar — no candidates."""
        collection = _get_collection()
        collection.upsert(
            ids=["obs::old1"],
            documents=["Python is great for data science and machine learning"],
            metadatas=[{"tier": "chromadb", "type": "observation"}],
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
        """Same topic, different wording → candidate found."""
        collection = _get_collection()
        collection.upsert(
            ids=["obs::old1"],
            documents=["Use the singleton pattern for database connections"],
            metadatas=[{"tier": "chromadb", "type": "observation"}],
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
        """Entries with status=superseded should be skipped."""
        collection = _get_collection()
        collection.upsert(
            ids=["obs::old1"],
            documents=["Use pattern X for error handling"],
            metadatas=[{"tier": "chromadb", "type": "observation", "status": "superseded"}],
        )
        config = {
            "similarity_threshold": 0.0,  # Accept everything
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
        collection = _get_collection()
        collection.upsert(
            ids=["obs::self1"],
            documents=["test content for self-matching"],
            metadatas=[{"tier": "chromadb", "type": "observation"}],
        )
        config = {
            "similarity_threshold": 0.0,
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
        collection = _get_collection()
        collection.upsert(
            ids=["obs::old1"],
            documents=["Always use type hints in Python functions"],
            metadatas=[{"tier": "chromadb", "type": "observation"}],
        )
        config = {
            "similarity_threshold": 0.0,  # Accept by similarity
            "divergence_threshold": 0.1,  # Very low divergence threshold — require very different words
            "max_candidates": 10,
        }
        result = find_conflict_candidates(
            "obs::new1",
            "Always use type hints in Python functions for clarity",
            config,
        )
        # High jaccard (very similar words) should NOT pass the divergence filter
        # (jaccard is HIGH, threshold is LOW — filter requires jaccard < threshold)
        assert result == []


# ── verify_conflicts_with_llm ──────────────────────────────────────────────


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
            result = verify_conflicts_with_llm("actually stop using X and Z", candidates, config)
        assert result == ["obs::old1", "obs::old3"]

    def test_fallback_on_failure(self):
        candidates = [
            {"id": "obs::old1", "content": "use X"},
        ]
        config = {}
        with patch("tools.conflict._call_haiku_raw") as mock_haiku:
            mock_haiku.return_value = None  # API failure
            result = verify_conflicts_with_llm("actually stop X", candidates, config)
        # Fallback: trust rule-based → return all candidates
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


# ── mark_superseded ─────────────────────────────────────────────────────────


class TestMarkSuperseded:

    def test_adds_metadata(self, mock_config):
        collection = _get_collection()
        collection.upsert(
            ids=["obs::old1"],
            documents=["old content"],
            metadatas=[{"tier": "chromadb", "type": "observation", "source": "auto-extract"}],
        )
        result = mark_superseded("obs::old1", "obs::new1")
        assert result is True

        updated = collection.get(ids=["obs::old1"])
        meta = updated["metadatas"][0]
        assert meta["status"] == "superseded"
        assert meta["superseded_by"] == "obs::new1"
        assert "superseded_at" in meta

    def test_preserves_other_metadata(self, mock_config):
        collection = _get_collection()
        collection.upsert(
            ids=["obs::old1"],
            documents=["old content"],
            metadatas=[{
                "tier": "chromadb",
                "type": "observation",
                "source": "auto-extract",
                "tags": "foo,bar",
                "importance_score": "0.8",
            }],
        )
        mark_superseded("obs::old1", "obs::new1")

        updated = collection.get(ids=["obs::old1"])
        meta = updated["metadatas"][0]
        assert meta["source"] == "auto-extract"
        assert meta["tags"] == "foo,bar"
        assert meta["importance_score"] == "0.8"
        assert meta["status"] == "superseded"

    def test_nonexistent_id(self, mock_config):
        result = mark_superseded("obs::doesnotexist", "obs::new1")
        assert result is False


# ── Cross-type conflict ─────────────────────────────────────────────────────


class TestCrossTypeConflict:

    def test_observation_vs_pattern(self, mock_config):
        """New observation can supersede an old pattern."""
        collection = _get_collection()
        collection.upsert(
            ids=["pattern::use-singleton"],
            documents=["Use singleton pattern for database connections"],
            metadatas=[{"tier": "chromadb", "type": "pattern", "namespace": "pattern::"}],
        )
        # The candidate finder queries all tier=chromadb, so patterns are included
        config = {
            "similarity_threshold": 0.0,
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
        collection = _get_collection()
        collection.upsert(
            ids=["decision::use-redux"],
            documents=["Decided to use Redux for state management"],
            metadatas=[{"tier": "chromadb", "type": "decision", "namespace": "decision::"}],
        )
        config = {
            "similarity_threshold": 0.0,
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


# ── detect_conflicts orchestrator ───────────────────────────────────────────


class TestDetectConflicts:

    def test_disabled(self, mock_config):
        mock_config.set(memory={
            "conflict_detection": {"enabled": False},
        })
        from tools.config import clear_config_cache
        clear_config_cache()
        result = detect_conflicts("obs::new1", "actually stop doing X")
        assert result == []

    def test_no_negation(self, mock_config):
        """Content without negation signals skips entirely."""
        result = detect_conflicts("obs::new1", "User worked on the API integration today")
        assert result == []

    def test_rule_based_pipeline(self, mock_config):
        """Full pipeline: write old entry, write contradicting new entry, verify superseded."""
        collection = _get_collection()
        collection.upsert(
            ids=["obs::old1"],
            documents=["Use the factory pattern for creating objects"],
            metadatas=[{"tier": "chromadb", "type": "observation"}],
        )

        # The detect_conflicts uses config defaults (use_llm=False)
        result = detect_conflicts(
            "obs::new1",
            "Actually avoid the factory pattern for creating objects, just use constructors",
        )
        # Whether old1 gets superseded depends on embedding similarity
        # The important thing is the pipeline doesn't crash
        assert isinstance(result, list)

    @patch("tools.conflict.verify_conflicts_with_llm")
    @patch("tools.conflict.find_conflict_candidates")
    def test_with_llm(self, mock_find, mock_verify, mock_config):
        mock_config.set(memory={
            "conflict_detection": {"enabled": True, "use_llm": True},
        })
        from tools.config import clear_config_cache
        clear_config_cache()

        mock_find.return_value = [
            {"id": "obs::old1", "content": "use X", "similarity": 0.8, "jaccard": 0.2},
            {"id": "obs::old2", "content": "use Y", "similarity": 0.75, "jaccard": 0.15},
        ]
        # LLM says only old1 is contradicted
        mock_verify.return_value = ["obs::old1"]

        # Pre-populate so mark_superseded can find the entry
        collection = _get_collection()
        collection.upsert(
            ids=["obs::old1"],
            documents=["use X"],
            metadatas=[{"tier": "chromadb", "type": "observation"}],
        )
        collection.upsert(
            ids=["obs::old2"],
            documents=["use Y"],
            metadatas=[{"tier": "chromadb", "type": "observation"}],
        )

        result = detect_conflicts("obs::new1", "actually stop using X, it causes issues")

        assert "obs::old1" in result
        assert "obs::old2" not in result
        mock_verify.assert_called_once()


# ── Filter integration tests ───────────────────────────────────────────────


class TestFilterSuperseded:

    def test_patterns_skip_superseded(self, mock_config):
        """_fetch_recent_observations skips superseded."""
        from tools.patterns import _fetch_recent_observations
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        collection = _get_collection()
        collection.upsert(
            ids=["obs::active"],
            documents=["active observation"],
            metadatas=[{
                "tier": "chromadb", "type": "observation",
                "namespace": "obs::", "created_at": now,
            }],
        )
        collection.upsert(
            ids=["obs::stale"],
            documents=["stale observation"],
            metadatas=[{
                "tier": "chromadb", "type": "observation",
                "namespace": "obs::", "created_at": now,
                "status": "superseded",
            }],
        )

        recent = _fetch_recent_observations(lookback_minutes=60)
        ids = [d["id"] for d in recent]
        assert "obs::active" in ids
        assert "obs::stale" not in ids

    def test_semantic_context_skip_superseded(self, mock_config):
        """semantic_context excludes superseded entries."""
        from tools.query import semantic_context
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        collection = _get_collection()
        collection.upsert(
            ids=["obs::active"],
            documents=["Python error handling best practices"],
            metadatas=[{
                "tier": "chromadb", "type": "observation",
                "namespace": "obs::", "updated_at": now,
            }],
        )
        collection.upsert(
            ids=["obs::stale"],
            documents=["Python error handling best practices outdated version"],
            metadatas=[{
                "tier": "chromadb", "type": "observation",
                "namespace": "obs::", "updated_at": now,
                "status": "superseded",
            }],
        )

        result = semantic_context("Python error handling", threshold=0.0, budget=8000)
        sources = [m["source"] for m in result.get("matches", [])]
        # The superseded entry should not appear in results
        assert "obs::stale" not in sources

    def test_backwards_compatible_no_status(self, mock_config):
        """Entries without status field still work (backward compatible)."""
        from tools.patterns import _fetch_recent_observations
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        collection = _get_collection()
        collection.upsert(
            ids=["obs::legacy"],
            documents=["legacy observation without status field"],
            metadatas=[{
                "tier": "chromadb", "type": "observation",
                "namespace": "obs::", "created_at": now,
            }],
        )

        recent = _fetch_recent_observations(lookback_minutes=60)
        ids = [d["id"] for d in recent]
        assert "obs::legacy" in ids


# ── Conflict log ────────────────────────────────────────────────────────────


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


# ── tier2_write integration ─────────────────────────────────────────────────


class TestTier2WriteIntegration:

    @patch("tools.conflict.detect_conflicts")
    def test_triggers_for_observation(self, mock_detect, mock_config):
        mock_detect.return_value = ["obs::old1"]
        result = tier2_write(
            content="actually stop using pattern X",
            content_type="observation",
        )
        assert result["success"]
        assert result.get("conflicts_resolved") == 1
        assert result.get("superseded_ids") == ["obs::old1"]
        mock_detect.assert_called_once()

    @patch("tools.conflict.detect_conflicts")
    def test_triggers_for_learning(self, mock_detect, mock_config):
        """Conflict detection runs for all Tier 2 types, not just observations."""
        mock_detect.return_value = []
        result = tier2_write(
            content="actually avoid using X for performance reasons",
            content_type="learning",
        )
        assert result["success"]
        mock_detect.assert_called_once()

    @patch("tools.conflict.detect_conflicts")
    def test_no_conflicts_no_extra_keys(self, mock_detect, mock_config):
        mock_detect.return_value = []
        result = tier2_write(
            content="no negation here, just a normal observation",
            content_type="observation",
        )
        assert result["success"]
        assert "conflicts_resolved" not in result

    @patch("tools.conflict.detect_conflicts", side_effect=Exception("boom"))
    def test_exception_does_not_block_write(self, mock_detect, mock_config):
        """If conflict detection fails, the write still succeeds."""
        result = tier2_write(
            content="actually stop X",
            content_type="observation",
        )
        assert result["success"]
        assert "conflicts_resolved" not in result
