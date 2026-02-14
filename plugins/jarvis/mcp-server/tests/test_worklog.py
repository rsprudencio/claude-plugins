"""Tests for worklog extraction functions in extract_observation.py."""
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add hooks-handlers to path for importing
HOOKS_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "hooks-handlers"
)
sys.path.insert(0, HOOKS_DIR)

from extract_observation import (
    _WORKLOG_ACTIVITY_TYPES,
    _DEDUP_JACCARD_THRESHOLD,
    _DEDUP_RELEVANCE_THRESHOLD,
    _HAIKU_MAX_TOKENS,
    discover_workstreams,
    is_duplicate_worklog,
    is_duplicate_observation,
    _has_jaccard_duplicate,
    normalize_worklog_response,
    store_worklog,
    jaccard_similarity,
    build_session_prompt,
)

# Pre-import modules so they can be patched
import tools.tier2  # noqa: F401
import tools.query  # noqa: F401


# ──────────────────────────────────────────────
# TestNormalizeWorklogResponse
# ──────────────────────────────────────────────


class TestNormalizeWorklogResponse:
    """Tests for normalize_worklog_response()."""

    def test_valid_worklog(self):
        """Valid worklog dict is extracted."""
        parsed = {
            "observations": [],
            "worklog": {
                "task_summary": "Adding Docker support",
                "workstream": "Jarvis Plugin",
                "activity_type": "coding",
                "tags": ["docker", "infra"],
            },
        }
        result = normalize_worklog_response(parsed)
        assert len(result) == 1
        assert result[0]["task_summary"] == "Adding Docker support"
        assert result[0]["workstream"] == "Jarvis Plugin"
        assert result[0]["activity_type"] == "coding"
        assert result[0]["tags"] == ["docker", "infra"]

    def test_null_worklog(self):
        """Null worklog returns empty list."""
        parsed = {"observations": [], "worklog": None}
        result = normalize_worklog_response(parsed)
        assert result == []

    def test_missing_worklog_key(self):
        """Missing worklog key returns empty list."""
        parsed = {"observations": []}
        result = normalize_worklog_response(parsed)
        assert result == []

    def test_worklogs_array_fallback(self):
        """Accepts worklogs array (takes first element)."""
        parsed = {
            "worklogs": [
                {"task_summary": "First task", "workstream": "misc", "activity_type": "coding"},
                {"task_summary": "Second task", "workstream": "misc", "activity_type": "coding"},
            ]
        }
        result = normalize_worklog_response(parsed)
        assert len(result) == 1
        assert result[0]["task_summary"] == "First task"

    def test_empty_task_summary(self):
        """Empty task_summary is rejected."""
        parsed = {"worklog": {"task_summary": "", "workstream": "misc", "activity_type": "coding"}}
        result = normalize_worklog_response(parsed)
        assert result == []

    def test_whitespace_task_summary(self):
        """Whitespace-only task_summary is rejected."""
        parsed = {"worklog": {"task_summary": "   ", "workstream": "misc", "activity_type": "coding"}}
        result = normalize_worklog_response(parsed)
        assert result == []

    def test_missing_task_summary(self):
        """Missing task_summary is rejected."""
        parsed = {"worklog": {"workstream": "misc", "activity_type": "coding"}}
        result = normalize_worklog_response(parsed)
        assert result == []

    def test_invalid_activity_type_defaults_to_other(self):
        """Invalid activity_type defaults to 'other'."""
        parsed = {"worklog": {"task_summary": "Something", "activity_type": "invalid_type"}}
        result = normalize_worklog_response(parsed)
        assert len(result) == 1
        assert result[0]["activity_type"] == "other"

    def test_missing_workstream_defaults_to_misc(self):
        """Missing workstream defaults to 'misc'."""
        parsed = {"worklog": {"task_summary": "Something"}}
        result = normalize_worklog_response(parsed)
        assert len(result) == 1
        assert result[0]["workstream"] == "misc"

    def test_empty_workstream_defaults_to_misc(self):
        """Empty workstream defaults to 'misc'."""
        parsed = {"worklog": {"task_summary": "Something", "workstream": ""}}
        result = normalize_worklog_response(parsed)
        assert result[0]["workstream"] == "misc"

    def test_missing_tags_defaults_to_empty(self):
        """Missing tags defaults to empty list."""
        parsed = {"worklog": {"task_summary": "Something"}}
        result = normalize_worklog_response(parsed)
        assert result[0]["tags"] == []

    def test_non_list_tags_defaults_to_empty(self):
        """Non-list tags defaults to empty list."""
        parsed = {"worklog": {"task_summary": "Something", "tags": "not-a-list"}}
        result = normalize_worklog_response(parsed)
        assert result[0]["tags"] == []

    def test_none_input(self):
        """None input returns empty list."""
        assert normalize_worklog_response(None) == []

    def test_non_dict_input(self):
        """Non-dict input returns empty list."""
        assert normalize_worklog_response("not a dict") == []

    def test_task_summary_stripped(self):
        """Task summary is stripped of whitespace."""
        parsed = {"worklog": {"task_summary": "  Adding feature  "}}
        result = normalize_worklog_response(parsed)
        assert result[0]["task_summary"] == "Adding feature"

    def test_workstream_stripped(self):
        """Workstream is stripped of whitespace."""
        parsed = {"worklog": {"task_summary": "Something", "workstream": "  VMPulse  "}}
        result = normalize_worklog_response(parsed)
        assert result[0]["workstream"] == "VMPulse"


# ──────────────────────────────────────────────
# TestWorklogJaccardSimilarity
# ──────────────────────────────────────────────


class TestJaccardSimilarity:
    """Tests for jaccard_similarity()."""

    def test_identical_texts(self):
        """Identical texts have similarity 1.0."""
        assert jaccard_similarity("hello world", "hello world") == 1.0

    def test_completely_different(self):
        """Completely different texts have similarity 0.0."""
        assert jaccard_similarity("hello world", "foo bar") == 0.0

    def test_partial_overlap(self):
        """Partial overlap gives intermediate score."""
        sim = jaccard_similarity("adding docker support", "adding worklog support")
        # Words: {adding, docker, support} vs {adding, worklog, support}
        # Intersection: {adding, support} = 2
        # Union: {adding, docker, support, worklog} = 4
        # Jaccard: 2/4 = 0.5
        assert sim == 0.5

    def test_case_insensitive(self):
        """Similarity is case-insensitive."""
        assert jaccard_similarity("Hello World", "hello world") == 1.0

    def test_both_empty(self):
        """Both empty returns 0.0."""
        assert jaccard_similarity("", "") == 0.0

    def test_one_empty(self):
        """One empty returns 0.0."""
        assert jaccard_similarity("hello", "") == 0.0

    def test_high_overlap_similar_sentences(self):
        """Similar sentences about the same task have high overlap."""
        a = "Adding worklog feature to Jarvis"
        b = "Adding worklog auto-extract to Jarvis plugin"
        sim = jaccard_similarity(a, b)
        # {adding, worklog, feature, to, jarvis} vs {adding, worklog, auto-extract, to, jarvis, plugin}
        # Intersection: {adding, worklog, to, jarvis} = 4
        # Union: {adding, worklog, feature, to, jarvis, auto-extract, plugin} = 7
        # Jaccard: 4/7 ≈ 0.571
        assert 0.5 < sim < 0.7


# ──────────────────────────────────────────────
# TestIsDuplicateWorklog
# ──────────────────────────────────────────────


class TestIsDuplicateWorklog:
    """Tests for is_duplicate_worklog()."""

    def test_no_existing_worklogs(self):
        """No duplicates when no existing worklogs."""
        with patch("tools.tier2.tier2_list", return_value={"success": True, "documents": []}):
            assert is_duplicate_worklog("New task", "session-1") is False

    def test_tier2_list_failure(self):
        """Treat tier2_list failure as no duplicates."""
        with patch("tools.tier2.tier2_list", return_value={"success": False}):
            assert is_duplicate_worklog("New task", "session-1") is False

    def test_identical_worklog_is_duplicate(self):
        """Identical worklog text is detected as duplicate."""
        existing = [{"content": "Adding Docker support", "metadata": {}}]
        with patch("tools.tier2.tier2_list", return_value={"success": True, "documents": existing}):
            assert is_duplicate_worklog("Adding Docker support", "session-1") is True

    def test_similar_worklog_above_threshold(self):
        """Similar worklog above threshold is duplicate."""
        # These share enough words to exceed 0.7 Jaccard
        existing = [{"content": "adding worklog feature to Jarvis plugin code", "metadata": {}}]
        with patch("tools.tier2.tier2_list", return_value={"success": True, "documents": existing}):
            result = is_duplicate_worklog("adding worklog feature to Jarvis plugin", "session-1")
            assert result is True

    def test_different_worklog_below_threshold(self):
        """Different worklog below threshold is not duplicate."""
        existing = [{"content": "Debugging VMPulse alerts", "metadata": {}}]
        with patch("tools.tier2.tier2_list", return_value={"success": True, "documents": existing}):
            assert is_duplicate_worklog("Adding Docker support for Jarvis MCP", "session-1") is False

    def test_passes_session_id_to_tier2_list(self):
        """Verifies session_id is passed to tier2_list."""
        mock_list = MagicMock(return_value={"success": True, "documents": []})
        with patch("tools.tier2.tier2_list", mock_list):
            is_duplicate_worklog("Test", "my-session-42")
            mock_list.assert_called_once_with(
                content_type="worklog",
                session_id="my-session-42",
                sort_by="created_at_desc",
            )


# ──────────────────────────────────────────────
# TestHasJaccardDuplicate (shared helper)
# ──────────────────────────────────────────────


class TestHasJaccardDuplicate:
    """Tests for _has_jaccard_duplicate() shared helper."""

    def test_no_candidates(self):
        """No duplicates when candidate list is empty."""
        assert _has_jaccard_duplicate("some text", []) is False

    def test_identical_match(self):
        """Identical text is a duplicate."""
        assert _has_jaccard_duplicate("hello world", ["hello world"]) is True

    def test_below_threshold(self):
        """Sufficiently different text is not a duplicate."""
        assert _has_jaccard_duplicate("Adding Docker support", ["Debugging VMPulse alerts"]) is False

    def test_custom_threshold(self):
        """Custom threshold is respected."""
        # These have moderate overlap
        candidates = ["adding worklog feature to plugin"]
        # With low threshold, it's a match
        assert _has_jaccard_duplicate("adding worklog feature", candidates, threshold=0.5) is True
        # With high threshold, it's not
        assert _has_jaccard_duplicate("adding worklog feature", candidates, threshold=0.95) is False


# ──────────────────────────────────────────────
# TestIsDuplicateObservation
# ──────────────────────────────────────────────


class TestIsDuplicateObservation:
    """Tests for is_duplicate_observation() — embedding relevance based."""

    def test_no_results(self):
        """No duplicates when semantic search returns nothing."""
        with patch("tools.query.query_vault",
                   return_value={"success": True, "results": []}):
            assert is_duplicate_observation("New insight") is False

    def test_query_failure(self):
        """Treat query failure as no duplicates."""
        with patch("tools.query.query_vault",
                   return_value={"success": False}):
            assert is_duplicate_observation("New insight") is False

    def test_high_relevance_is_duplicate(self):
        """Observation with relevance >= 0.95 is a duplicate."""
        results = [{"relevance": 0.98, "preview": "User prefers dark mode"}]
        with patch("tools.query.query_vault",
                   return_value={"success": True, "results": results}):
            assert is_duplicate_observation("User prefers dark mode") is True

    def test_low_relevance_not_duplicate(self):
        """Observation with relevance < 0.95 is not a duplicate."""
        results = [{"relevance": 0.80, "preview": "Debugging VMPulse alerts"}]
        with patch("tools.query.query_vault",
                   return_value={"success": True, "results": results}):
            assert is_duplicate_observation("Adding Docker support for Jarvis") is False

    def test_custom_threshold(self):
        """Custom threshold overrides default."""
        results = [{"relevance": 0.85, "preview": "Some observation"}]
        with patch("tools.query.query_vault",
                   return_value={"success": True, "results": results}):
            # Below default (0.95) but above custom (0.80)
            assert is_duplicate_observation("Similar", threshold=0.80) is True
            assert is_duplicate_observation("Similar", threshold=0.90) is False

    def test_passes_observation_type_filter(self):
        """Verifies filter={'type': 'observation'} and n_results=1."""
        mock_query = MagicMock(return_value={"success": True, "results": []})
        with patch("tools.query.query_vault", mock_query):
            is_duplicate_observation("Test content")
            mock_query.assert_called_once_with(
                query="Test content",
                n_results=1,
                filter={"type": "observation"},
            )

    def test_default_threshold_is_relevance(self):
        """Default threshold matches _DEDUP_RELEVANCE_THRESHOLD (0.95)."""
        assert _DEDUP_RELEVANCE_THRESHOLD == 0.95


# ──────────────────────────────────────────────
# TestDiscoverWorkstreams
# ──────────────────────────────────────────────


class TestDiscoverWorkstreams:
    """Tests for discover_workstreams()."""

    def test_empty_when_no_worklogs(self):
        """Returns empty list when no worklogs exist."""
        with patch("tools.tier2.tier2_list", return_value={"success": True, "documents": []}):
            assert discover_workstreams() == []

    def test_extracts_unique_workstreams(self):
        """Extracts unique workstream names from metadata."""
        docs = [
            {"content": "Task A", "metadata": {"workstream": "VMPulse"}},
            {"content": "Task B", "metadata": {"workstream": "Jarvis Plugin"}},
            {"content": "Task C", "metadata": {"workstream": "VMPulse"}},  # duplicate
        ]
        with patch("tools.tier2.tier2_list", return_value={"success": True, "documents": docs}):
            result = discover_workstreams()
            assert result == ["Jarvis Plugin", "VMPulse"]  # sorted

    def test_excludes_misc(self):
        """Excludes 'misc' workstream from results."""
        docs = [
            {"content": "Task A", "metadata": {"workstream": "VMPulse"}},
            {"content": "Task B", "metadata": {"workstream": "misc"}},
        ]
        with patch("tools.tier2.tier2_list", return_value={"success": True, "documents": docs}):
            result = discover_workstreams()
            assert result == ["VMPulse"]
            assert "misc" not in result

    def test_handles_missing_metadata(self):
        """Gracefully handles entries without workstream metadata."""
        docs = [
            {"content": "Task A", "metadata": {"workstream": "VMPulse"}},
            {"content": "Task B", "metadata": {}},
        ]
        with patch("tools.tier2.tier2_list", return_value={"success": True, "documents": docs}):
            result = discover_workstreams()
            assert result == ["VMPulse"]

    def test_failure_returns_empty(self):
        """Returns empty list on tier2_list failure."""
        with patch("tools.tier2.tier2_list", return_value={"success": False}):
            assert discover_workstreams() == []


# ──────────────────────────────────────────────
# TestStoreWorklog
# ──────────────────────────────────────────────


class TestStoreWorklog:
    """Tests for store_worklog()."""

    def test_calls_tier2_write(self):
        """Verifies tier2_write is called with correct params."""
        mock_write = MagicMock(return_value={"success": True, "id": "worklog::123"})
        with patch("tools.tier2.tier2_write", mock_write):
            result = store_worklog(
                task_summary="Adding Docker support",
                workstream="Jarvis Plugin",
                activity_type="coding",
                tags=["docker"],
                source_label="auto-extract:stop-hook:worklog",
                project_path="/home/user/jarvis-plugin",
                git_branch="master",
                session_id="test-session",
                transcript_line=42,
            )

            mock_write.assert_called_once()
            call_kwargs = mock_write.call_args[1]
            assert call_kwargs["content"] == "Adding Docker support"
            assert call_kwargs["content_type"] == "worklog"
            assert call_kwargs["importance_score"] == 0.5
            assert call_kwargs["source"] == "auto-extract:stop-hook:worklog"
            assert call_kwargs["tags"] == ["docker"]
            extra = call_kwargs["extra_metadata"]
            assert extra["workstream"] == "Jarvis Plugin"
            assert extra["activity_type"] == "coding"
            assert extra["project_dir"] == "jarvis-plugin"
            assert extra["session_id"] == "test-session"

    def test_minimal_params(self):
        """Works with minimal parameters."""
        mock_write = MagicMock(return_value={"success": True, "id": "worklog::456"})
        with patch("tools.tier2.tier2_write", mock_write):
            result = store_worklog(
                task_summary="Quick task",
                workstream="misc",
                activity_type="other",
                tags=[],
                source_label="test",
            )
            assert result["success"]


# ──────────────────────────────────────────────
# TestBuildSessionPromptWorkstreams
# ──────────────────────────────────────────────


class TestBuildSessionPromptWorkstreams:
    """Tests for workstream integration in build_session_prompt()."""

    def _make_turn(self, user="Hello", assistant="Hi there"):
        return {
            "user_text": user,
            "assistant_text": assistant,
            "tool_names": [],
            "token_usage": "100 in, 50 out",
            "relevant_files": [],
        }

    def test_workstreams_included_in_prompt(self):
        """Known workstreams appear in the prompt."""
        turns = [self._make_turn()]
        prompt = build_session_prompt(
            turns, "Hello", 4000,
            workstreams=["VMPulse", "Jarvis Plugin"],
        )
        assert "VMPulse" in prompt
        assert "Jarvis Plugin" in prompt

    def test_no_workstreams_shows_none_yet(self):
        """Empty workstreams list shows fallback text."""
        turns = [self._make_turn()]
        prompt = build_session_prompt(
            turns, "Hello", 4000,
            workstreams=[],
        )
        assert "None yet" in prompt

    def test_none_workstreams_shows_none_yet(self):
        """None workstreams shows fallback text."""
        turns = [self._make_turn()]
        prompt = build_session_prompt(
            turns, "Hello", 4000,
            workstreams=None,
        )
        assert "None yet" in prompt

    def test_prompt_has_worklog_task(self):
        """Prompt contains TASK 2 for worklog extraction."""
        turns = [self._make_turn()]
        prompt = build_session_prompt(turns, "Hello", 4000)
        assert "TASK 2" in prompt
        assert "WORKLOG" in prompt


# ──────────────────────────────────────────────
# TestConstants
# ──────────────────────────────────────────────


class TestWorklogConstants:
    """Tests for worklog-related constants."""

    def test_haiku_max_tokens_increased(self):
        """Max tokens bumped to 1000 for worklog response."""
        assert _HAIKU_MAX_TOKENS == 1000

    def test_dedup_threshold(self):
        """Dedup threshold is 0.7."""
        assert _DEDUP_JACCARD_THRESHOLD == 0.7

    def test_activity_types(self):
        """All expected activity types present."""
        expected = {"coding", "debugging", "reviewing", "configuring",
                    "planning", "discussing", "researching", "other"}
        assert _WORKLOG_ACTIVITY_TYPES == expected
