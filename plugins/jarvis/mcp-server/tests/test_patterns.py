"""Tests for pattern detection module."""
import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

import pytest

from tools.patterns import (
    extract_signature,
    jaccard_set,
    classify_pattern_type,
    generate_title,
    compute_confidence,
    create_or_merge_candidate,
    cleanup_candidates,
    promote_candidate,
    scan_once,
    pattern_detection_loop,
    reset_candidates,
    _candidates,
    PatternCandidate,
    _signature_key,
    _fetch_recent_observations,
    STOP_WORDS,
)


@pytest.fixture(autouse=True)
def clean_candidates():
    """Reset module-level candidates between tests."""
    reset_candidates()
    yield
    reset_candidates()


# ── extract_signature tests ──────────────────────────────────────────────────


class TestExtractSignature:

    def test_basic_content(self):
        sig = extract_signature("The user fixed a critical error in the config module")
        assert "fixed" in sig
        assert "critical" in sig
        assert "error" in sig
        assert "config" in sig
        assert "module" in sig
        # Stop words should be filtered
        assert "the" not in sig
        assert "in" not in sig

    def test_tags_included(self):
        sig = extract_signature("Some content", tags=["refactoring", "python"])
        assert "refactoring" in sig
        assert "python" in sig

    def test_title_included(self):
        sig = extract_signature("content", title="Error handling pattern")
        assert "error" in sig
        assert "handling" in sig
        assert "pattern" in sig

    def test_empty_input(self):
        sig = extract_signature("", tags=None, title=None)
        assert sig == set()

    def test_stop_words_filtered(self):
        sig = extract_signature("the a an is was were are be been being have has")
        # All stop words, should be empty or near-empty
        assert len(sig - STOP_WORDS) == len(sig)

    def test_short_tokens_filtered(self):
        """Tokens shorter than 2 chars should be filtered by the regex."""
        sig = extract_signature("a b c de fg this is x y z test")
        assert "de" in sig
        assert "fg" in sig
        assert "test" in sig

    def test_combined_sources(self):
        sig = extract_signature(
            "Fixed error handling",
            tags=["bugfix"],
            title="Config error"
        )
        assert "fixed" in sig
        assert "error" in sig
        assert "handling" in sig
        assert "bugfix" in sig
        assert "config" in sig


# ── jaccard_set tests ────────────────────────────────────────────────────────


class TestJaccardSet:

    def test_identical_sets(self):
        assert jaccard_set({"a", "b", "c"}, {"a", "b", "c"}) == 1.0

    def test_disjoint_sets(self):
        assert jaccard_set({"a", "b"}, {"c", "d"}) == 0.0

    def test_partial_overlap(self):
        # {a,b,c} & {b,c,d} = {b,c}, union = {a,b,c,d}
        assert jaccard_set({"a", "b", "c"}, {"b", "c", "d"}) == 2 / 4

    def test_empty_sets(self):
        assert jaccard_set(set(), set()) == 0.0

    def test_one_empty(self):
        assert jaccard_set({"a", "b"}, set()) == 0.0

    def test_subset(self):
        # {a,b} & {a,b,c} = {a,b}, union = {a,b,c}
        result = jaccard_set({"a", "b"}, {"a", "b", "c"})
        assert abs(result - 2 / 3) < 1e-10


# ── classify_pattern_type tests ──────────────────────────────────────────────


class TestClassifyPatternType:

    def test_bug_keywords(self):
        sig = {"error", "crash", "fix", "config"}
        assert classify_pattern_type(sig) == "bug"

    def test_refactor_keywords(self):
        sig = {"refactor", "extract", "method", "class"}
        assert classify_pattern_type(sig) == "refactor"

    def test_architecture_keywords(self):
        sig = {"interface", "abstraction", "module", "boundary"}
        assert classify_pattern_type(sig) == "architecture"

    def test_anti_pattern_keywords(self):
        sig = {"workaround", "hack", "config"}
        assert classify_pattern_type(sig) == "anti-pattern"

    def test_best_practice_keywords(self):
        sig = {"test", "validate", "coverage"}
        assert classify_pattern_type(sig) == "best-practice"

    def test_general_fallback(self):
        """With < 2 keyword hits, falls back to 'general'."""
        sig = {"random", "tokens", "nothing", "special"}
        assert classify_pattern_type(sig) == "general"

    def test_single_keyword_not_enough(self):
        """A single keyword match should still return 'general'."""
        sig = {"error", "random", "tokens"}
        assert classify_pattern_type(sig) == "general"

    def test_content_supplements_signature(self):
        """Content tokens are also considered for classification."""
        sig = {"config"}
        result = classify_pattern_type(sig, content="error crash failure broken")
        assert result == "bug"


# ── compute_confidence tests ─────────────────────────────────────────────────


class TestComputeConfidence:

    def test_minimum_confidence(self):
        """Frequency=1 gives base 0.3 + 0.04 = 0.34."""
        result = compute_confidence(1, ["obs1"])
        assert abs(result - 0.34) < 1e-10

    def test_max_confidence(self):
        """Frequency=10 gives 0.3 + 0.4 = 0.7."""
        result = compute_confidence(10, ["obs1"])
        assert abs(result - 0.7) < 1e-10

    def test_capped_frequency(self):
        """Frequency > 10 is capped at 10."""
        result = compute_confidence(20, ["obs1"])
        assert abs(result - 0.7) < 1e-10

    def test_intermediate(self):
        """Frequency=5 gives 0.3 + 0.2 = 0.5."""
        result = compute_confidence(5, ["obs1"])
        assert abs(result - 0.5) < 1e-10


# ── generate_title tests ────────────────────────────────────────────────────


class TestGenerateTitle:

    def test_format(self):
        title = generate_title({"error", "config", "handling"}, "bug")
        assert title.startswith("Recurring bug:")
        # Should have sorted tokens
        assert "config" in title
        assert "error" in title
        assert "handling" in title

    def test_max_tokens(self):
        """Only up to 4 tokens in the title."""
        sig = {"alpha", "beta", "gamma", "delta", "epsilon"}
        title = generate_title(sig, "general")
        # Sorted: alpha, beta, delta, epsilon (first 4)
        assert "alpha" in title
        assert "beta" in title
        assert "delta" in title
        assert "epsilon" in title
        assert "gamma" not in title


# ── Candidate management tests ───────────────────────────────────────────────


class TestCandidateManagement:

    def test_create_candidate(self):
        sig = {"error", "config", "handling", "module"}
        result = create_or_merge_candidate(sig, "obs::123", "error in config", 0.3)
        assert result is not None
        assert result.frequency == 1
        assert "obs::123" in result.observation_ids
        assert len(_candidates) == 1

    def test_merge_similar_candidate(self):
        """A similar observation should merge into existing candidate."""
        sig1 = {"error", "config", "handling", "module"}
        create_or_merge_candidate(sig1, "obs::1", "error handling", 0.3)

        sig2 = {"error", "config", "handling", "parser"}
        result = create_or_merge_candidate(sig2, "obs::2", "config error", 0.3)

        assert result is not None
        assert result.frequency == 2
        assert "obs::1" in result.observation_ids
        assert "obs::2" in result.observation_ids
        # Merged signature should contain tokens from both
        assert "parser" in result.signature

    def test_no_merge_below_threshold(self):
        """Dissimilar observations create separate candidates."""
        sig1 = {"error", "config", "handling", "module"}
        create_or_merge_candidate(sig1, "obs::1", "error", 0.3)

        # Completely different
        sig2 = {"test", "coverage", "integration", "pytest"}
        create_or_merge_candidate(sig2, "obs::2", "testing", 0.3)

        assert len(_candidates) == 2

    def test_too_small_signature(self):
        """Signatures with < 3 tokens are rejected."""
        sig = {"error", "fix"}
        result = create_or_merge_candidate(sig, "obs::1", "error", 0.3)
        assert result is None
        assert len(_candidates) == 0

    def test_duplicate_obs_id_not_added(self):
        sig = {"error", "config", "handling", "module"}
        create_or_merge_candidate(sig, "obs::1", "error", 0.3)
        create_or_merge_candidate(sig, "obs::1", "error", 0.3)
        for c in _candidates.values():
            assert c.observation_ids.count("obs::1") == 1


class TestCleanupCandidates:

    def test_expired_removal(self):
        now = datetime.now(timezone.utc)
        old_time = (now - timedelta(days=10)).isoformat()

        _candidates["old"] = PatternCandidate(
            key="old",
            signature=frozenset({"a", "b", "c"}),
            pattern_type="general",
            frequency=1,
            first_seen=old_time,
            last_seen=old_time,
            observation_ids=["obs::1"],
        )
        _candidates["new"] = PatternCandidate(
            key="new",
            signature=frozenset({"d", "e", "f"}),
            pattern_type="general",
            frequency=1,
            first_seen=now.isoformat(),
            last_seen=now.isoformat(),
            observation_ids=["obs::2"],
        )

        removed = cleanup_candidates(max_candidates=200, expiry_days=7)
        assert removed == 1
        assert "old" not in _candidates
        assert "new" in _candidates

    def test_lru_eviction(self):
        now = datetime.now(timezone.utc)
        for i in range(5):
            ts = (now - timedelta(hours=5 - i)).isoformat()
            _candidates[f"c{i}"] = PatternCandidate(
                key=f"c{i}",
                signature=frozenset({f"t{i}"}),
                pattern_type="general",
                frequency=1,
                first_seen=ts,
                last_seen=ts,
                observation_ids=[f"obs::{i}"],
            )

        removed = cleanup_candidates(max_candidates=3, expiry_days=30)
        assert removed == 2
        assert len(_candidates) == 3
        # Oldest should be removed (c0, c1)
        assert "c0" not in _candidates
        assert "c1" not in _candidates

    def test_no_cleanup_needed(self):
        now = datetime.now(timezone.utc).isoformat()
        _candidates["ok"] = PatternCandidate(
            key="ok",
            signature=frozenset({"a", "b", "c"}),
            pattern_type="general",
            frequency=1,
            first_seen=now,
            last_seen=now,
            observation_ids=["obs::1"],
        )
        removed = cleanup_candidates(max_candidates=200, expiry_days=7)
        assert removed == 0
        assert len(_candidates) == 1


# ── Promotion tests ──────────────────────────────────────────────────────────


class TestPromotion:

    def test_promote_writes_correct_metadata(self, mock_config):
        now = datetime.now(timezone.utc).isoformat()
        candidate = PatternCandidate(
            key="test-key",
            signature=frozenset({"error", "config", "handling", "repeated"}),
            pattern_type="bug",
            frequency=5,
            first_seen=now,
            last_seen=now,
            observation_ids=["obs::1", "obs::2", "obs::3", "obs::4", "obs::5"],
            title="Recurring bug: config, error, handling, repeated",
        )

        with patch("tools.patterns.tier2_list") as mock_list:
            mock_list.return_value = {"success": True, "documents": []}
            with patch("tools.patterns.tier2_write") as mock_write:
                mock_write.return_value = {"success": True, "id": "pattern::test"}
                result = promote_candidate(candidate, merge_threshold=0.7)

        assert result["success"]
        # Check tier2_write was called with expected args
        call_kwargs = mock_write.call_args[1]
        assert call_kwargs["content_type"] == "pattern"
        assert call_kwargs["source"] == "pattern-detection"
        assert "auto-detected" in call_kwargs["tags"]
        assert call_kwargs["extra_metadata"]["pattern_type"] == "bug"
        assert call_kwargs["extra_metadata"]["observation_count"] == "5"
        assert "error" in call_kwargs["extra_metadata"]["signature_tokens"]

    def test_promote_dedup_against_existing(self, mock_config):
        now = datetime.now(timezone.utc).isoformat()
        candidate = PatternCandidate(
            key="test-key",
            signature=frozenset({"error", "config", "handling"}),
            pattern_type="bug",
            frequency=3,
            first_seen=now,
            last_seen=now,
            observation_ids=["obs::1", "obs::2", "obs::3"],
            title="Recurring bug: config, error, handling",
        )

        # Existing pattern with very similar signature
        existing_doc = {
            "id": "pattern::existing-bug",
            "content": "Existing pattern",
            "metadata": {
                "signature_tokens": "config,error,handling,module",
            }
        }

        with patch("tools.patterns.tier2_list") as mock_list:
            mock_list.return_value = {"success": True, "documents": [existing_doc]}
            with patch("tools.patterns.tier2_write") as mock_write:
                result = promote_candidate(candidate, merge_threshold=0.7)

        assert result["success"]
        assert result["action"] == "merged"
        assert result["existing_id"] == "pattern::existing-bug"
        # tier2_write should NOT have been called (dedup)
        mock_write.assert_not_called()

    def test_no_promote_below_threshold(self, mock_config):
        """Candidates below frequency threshold should not be promoted via scan_once."""
        now = datetime.now(timezone.utc).isoformat()
        _candidates["low"] = PatternCandidate(
            key="low",
            signature=frozenset({"error", "config", "handling"}),
            pattern_type="bug",
            frequency=2,  # Below default threshold of 3
            first_seen=now,
            last_seen=now,
            observation_ids=["obs::1", "obs::2"],
            title="Low frequency",
        )

        config = {
            "lookback_minutes": 10,
            "similarity_threshold": 0.3,
            "promotion_threshold": 3,
            "merge_threshold": 0.7,
            "max_candidates": 200,
            "candidate_expiry_days": 7,
        }

        with patch("tools.patterns._fetch_recent_observations") as mock_fetch:
            mock_fetch.return_value = []
            result = scan_once(config)

        assert result["promoted"] == 0
        assert "low" in _candidates  # Still there

    def test_promote_at_threshold(self, mock_config):
        """Candidates meeting frequency threshold should be promoted."""
        now = datetime.now(timezone.utc).isoformat()
        _candidates["ready"] = PatternCandidate(
            key="ready",
            signature=frozenset({"error", "config", "handling"}),
            pattern_type="bug",
            frequency=3,
            first_seen=now,
            last_seen=now,
            observation_ids=["obs::1", "obs::2", "obs::3"],
            title="Recurring bug: config, error, handling",
        )

        config = {
            "lookback_minutes": 10,
            "similarity_threshold": 0.3,
            "promotion_threshold": 3,
            "merge_threshold": 0.7,
            "max_candidates": 200,
            "candidate_expiry_days": 7,
        }

        with patch("tools.patterns._fetch_recent_observations") as mock_fetch:
            mock_fetch.return_value = []
            with patch("tools.patterns.tier2_list") as mock_list:
                mock_list.return_value = {"success": True, "documents": []}
                with patch("tools.patterns.tier2_write") as mock_write:
                    mock_write.return_value = {"success": True, "id": "pattern::test"}
                    result = scan_once(config)

        assert result["promoted"] == 1
        assert "ready" not in _candidates  # Removed after promotion


# ── Integration tests (scan pipeline) ────────────────────────────────────────


class TestScanPipeline:

    def test_scan_empty_collection(self, mock_config):
        config = {
            "lookback_minutes": 10,
            "similarity_threshold": 0.3,
            "promotion_threshold": 3,
            "merge_threshold": 0.7,
            "max_candidates": 200,
            "candidate_expiry_days": 7,
        }

        with patch("tools.patterns._fetch_recent_observations") as mock_fetch:
            mock_fetch.return_value = []
            result = scan_once(config)

        assert result["observations"] == 0
        assert result["candidates_updated"] == 0
        assert result["promoted"] == 0

    def test_scan_with_observations(self, mock_config):
        """Full pipeline: observations -> candidates -> (no promotion yet)."""
        now = datetime.now(timezone.utc).isoformat()
        observations = [
            {
                "id": "obs::1",
                "content": "Found error in config handling that causes crash",
                "metadata": {"created_at": now, "tags": "bugfix"},
            },
            {
                "id": "obs::2",
                "content": "Another error in config parsing with crash report",
                "metadata": {"created_at": now, "tags": "bugfix"},
            },
        ]

        config = {
            "lookback_minutes": 10,
            "similarity_threshold": 0.3,
            "promotion_threshold": 3,
            "merge_threshold": 0.7,
            "max_candidates": 200,
            "candidate_expiry_days": 7,
        }

        with patch("tools.patterns._fetch_recent_observations") as mock_fetch:
            mock_fetch.return_value = observations
            result = scan_once(config)

        assert result["observations"] == 2
        assert result["candidates_updated"] == 2
        assert result["promoted"] == 0  # Only 2 obs, threshold is 3
        assert result["active_candidates"] >= 1

    def test_scan_promotes_when_threshold_met(self, mock_config):
        """Three similar observations should result in a promotion."""
        now = datetime.now(timezone.utc).isoformat()
        observations = [
            {
                "id": f"obs::{i}",
                "content": "Recurring error in config handling causes crash in module",
                "metadata": {"created_at": now, "tags": "bugfix,config"},
            }
            for i in range(1, 4)  # 3 observations
        ]

        config = {
            "lookback_minutes": 10,
            "similarity_threshold": 0.3,
            "promotion_threshold": 3,
            "merge_threshold": 0.7,
            "max_candidates": 200,
            "candidate_expiry_days": 7,
        }

        with patch("tools.patterns._fetch_recent_observations") as mock_fetch:
            mock_fetch.return_value = observations
            with patch("tools.patterns.tier2_list") as mock_list:
                mock_list.return_value = {"success": True, "documents": []}
                with patch("tools.patterns.tier2_write") as mock_write:
                    mock_write.return_value = {"success": True, "id": "pattern::test"}
                    result = scan_once(config)

        assert result["observations"] == 3
        assert result["promoted"] == 1

    def test_fetch_recent_observations_filters_by_time(self, mock_config):
        """Only observations within lookback window should be returned."""
        now = datetime.now(timezone.utc)
        recent = (now - timedelta(minutes=5)).isoformat()
        old = (now - timedelta(minutes=30)).isoformat()

        with patch("tools.patterns.tier2_list") as mock_list:
            mock_list.return_value = {
                "success": True,
                "documents": [
                    {"id": "obs::new", "content": "new", "metadata": {"created_at": recent}},
                    {"id": "obs::old", "content": "old", "metadata": {"created_at": old}},
                ],
            }
            result = _fetch_recent_observations(lookback_minutes=10)

        assert len(result) == 1
        assert result[0]["id"] == "obs::new"


# ── Loop disabled test ───────────────────────────────────────────────────────


class TestLoopControl:

    def test_loop_disabled(self):
        """Loop should sleep when disabled, not crash."""

        def mock_config():
            return {"enabled": False, "scan_interval_seconds": 0.01}

        async def _run():
            with patch("tools.config.get_pattern_detection_config", mock_config):
                with patch("tools.patterns._STARTUP_DELAY", 0):
                    task = asyncio.create_task(pattern_detection_loop())
                    await asyncio.sleep(0.05)
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

        asyncio.run(_run())
        # If we get here without error, the disabled path works


# ── Config getter tests ──────────────────────────────────────────────────────


class TestPatternDetectionConfig:

    def test_defaults(self, mock_config):
        from tools.config import get_pattern_detection_config
        config = get_pattern_detection_config()
        assert config["enabled"] is True
        assert config["scan_interval_seconds"] == 300
        assert config["similarity_threshold"] == 0.3
        assert config["promotion_threshold"] == 3
        assert config["max_candidates"] == 200
        assert config["candidate_expiry_days"] == 7
        assert config["lookback_minutes"] == 10
        assert config["merge_threshold"] == 0.7

    def test_override(self, mock_config):
        from tools.config import get_pattern_detection_config
        import tools.config as config_module

        mock_config.set(memory={
            "pattern_detection": {
                "enabled": False,
                "promotion_threshold": 5,
            }
        })
        config = get_pattern_detection_config()
        assert config["enabled"] is False
        assert config["promotion_threshold"] == 5
        # Defaults still apply for unset keys
        assert config["scan_interval_seconds"] == 300
