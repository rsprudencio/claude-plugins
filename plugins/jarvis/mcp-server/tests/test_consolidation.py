"""Tests for tools/consolidation.py — LLM-driven memory consolidation."""

import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime, timezone

from tools.consolidation import (
    MemoryCluster,
    ConsolidationResult,
    build_consolidation_prompt,
    parse_consolidation_response,
    assess_confidence,
    apply_consolidation,
    undo_consolidation,
    find_consolidation_candidates,
    dry_run_consolidation,
    _load_cluster_contents,
)


class TestMemoryCluster:
    """MemoryCluster data structure."""

    def test_size(self):
        c = MemoryCluster(memory_ids=["a", "b", "c"], avg_similarity=0.9, total_importance=2.0)
        assert c.size == 3

    def test_idempotency_key_deterministic(self):
        """Same IDs always produce same key regardless of order."""
        c1 = MemoryCluster(memory_ids=["b", "a", "c"], avg_similarity=0.9, total_importance=2.0)
        c2 = MemoryCluster(memory_ids=["c", "a", "b"], avg_similarity=0.9, total_importance=2.0)
        assert c1.idempotency_key == c2.idempotency_key

    def test_different_ids_different_key(self):
        c1 = MemoryCluster(memory_ids=["a", "b"], avg_similarity=0.9, total_importance=2.0)
        c2 = MemoryCluster(memory_ids=["a", "c"], avg_similarity=0.9, total_importance=2.0)
        assert c1.idempotency_key != c2.idempotency_key


class TestBuildConsolidationPrompt:
    """Prompt construction for LLM."""

    def test_includes_memory_contents(self):
        cluster = MemoryCluster(
            memory_ids=["id1", "id2"],
            avg_similarity=0.9,
            total_importance=1.6,
            contents=[
                {"id": "id1", "document": "First observation", "importance_score": 0.8},
                {"id": "id2", "document": "Second observation", "importance_score": 0.8},
            ],
        )
        prompt = build_consolidation_prompt(cluster)
        assert "First observation" in prompt
        assert "Second observation" in prompt
        assert "[ID: id1]" in prompt
        assert "[ID: id2]" in prompt

    def test_includes_importance(self):
        cluster = MemoryCluster(
            memory_ids=["id1"],
            avg_similarity=0.9,
            total_importance=0.8,
            contents=[
                {"id": "id1", "document": "Test", "importance_score": 0.8},
            ],
        )
        prompt = build_consolidation_prompt(cluster)
        assert "importance: 0.8" in prompt


class TestParseConsolidationResponse:
    """Parse LLM JSON responses."""

    def test_valid_json(self):
        response = json.dumps({
            "content": "Consolidated summary",
            "importance": 0.8,
            "supersedes": ["id1", "id2"],
            "contradictions": [],
        })
        result = parse_consolidation_response(response)
        assert result["content"] == "Consolidated summary"
        assert result["supersedes"] == ["id1", "id2"]

    def test_json_in_code_block(self):
        response = '```json\n{"content": "summary", "importance": 0.8, "supersedes": [], "contradictions": []}\n```'
        result = parse_consolidation_response(response)
        assert result["content"] == "summary"

    def test_json_in_plain_code_block(self):
        response = '```\n{"content": "summary", "importance": 0.8, "supersedes": [], "contradictions": []}\n```'
        result = parse_consolidation_response(response)
        assert result["content"] == "summary"

    def test_invalid_json(self):
        result = parse_consolidation_response("not json at all")
        assert "error" in result

    def test_with_contradictions(self):
        response = json.dumps({
            "content": "Summary with conflicts",
            "importance": 0.7,
            "supersedes": ["a", "b"],
            "contradictions": [
                {"claim": "X vs Y", "sources": ["a", "b"]}
            ],
        })
        result = parse_consolidation_response(response)
        assert len(result["contradictions"]) == 1


class TestAssessConfidence:
    """Confidence scoring for consolidation candidates."""

    def test_high_cohesion_no_contradictions(self):
        cluster = MemoryCluster(memory_ids=["a", "b", "c"], avg_similarity=0.92, total_importance=2.0)
        score = assess_confidence(cluster, contradictions=[])
        assert score == pytest.approx(0.92, abs=0.01)

    def test_contradiction_penalty(self):
        cluster = MemoryCluster(memory_ids=["a", "b", "c"], avg_similarity=0.92, total_importance=2.0)
        score = assess_confidence(cluster, contradictions=[{"claim": "x"}])
        assert score == pytest.approx(0.82, abs=0.01)

    def test_many_contradictions_low_confidence(self):
        cluster = MemoryCluster(memory_ids=["a", "b", "c"], avg_similarity=0.85, total_importance=2.0)
        contradictions = [{"claim": f"c{i}"} for i in range(5)]
        score = assess_confidence(cluster, contradictions)
        assert score == pytest.approx(0.35, abs=0.01)

    def test_floor_at_zero(self):
        cluster = MemoryCluster(memory_ids=["a", "b"], avg_similarity=0.5, total_importance=1.0)
        contradictions = [{"claim": f"c{i}"} for i in range(10)]
        score = assess_confidence(cluster, contradictions)
        assert score >= 0.0


class TestApplyConsolidation:
    """Transactional apply of consolidation results."""

    @patch("tools.consolidation.get_embedding_service")
    @patch("tools.consolidation._get_pool")
    def test_successful_apply(self, mock_pool, mock_embed):
        mock_embed.return_value.encode.return_value = [0.1] * 384

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = None  # No existing consolidation
        mock_cur.rowcount = 3
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_pool.return_value.connection.return_value.__enter__ = lambda s: mock_conn
        mock_pool.return_value.connection.return_value.__exit__ = MagicMock(return_value=False)

        cluster = MemoryCluster(memory_ids=["a", "b", "c"], avg_similarity=0.9, total_importance=2.4)
        result = ConsolidationResult(
            content="Consolidated",
            importance=0.8,
            supersedes=["a", "b", "c"],
            cluster=cluster,
        )

        output = apply_consolidation(result, run_id="test-run-1")
        assert output["run_id"] == "test-run-1"
        assert output["superseded_count"] == 3
        assert "new_id" in output

    @patch("tools.consolidation.get_embedding_service")
    @patch("tools.consolidation._get_pool")
    def test_idempotent_skip(self, mock_pool, mock_embed):
        """Same cluster applied twice is idempotent."""
        mock_embed.return_value.encode.return_value = [0.1] * 384

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.fetchone.return_value = ("consolidated::abc123",)  # Already exists
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_pool.return_value.connection.return_value.__enter__ = lambda s: mock_conn
        mock_pool.return_value.connection.return_value.__exit__ = MagicMock(return_value=False)

        cluster = MemoryCluster(memory_ids=["a", "b", "c"], avg_similarity=0.9, total_importance=2.4)
        result = ConsolidationResult(
            content="Consolidated",
            importance=0.8,
            supersedes=["a", "b", "c"],
            cluster=cluster,
        )

        output = apply_consolidation(result)
        assert output["skipped"] is True

    def test_no_cluster_error(self):
        result = ConsolidationResult(
            content="No cluster",
            importance=0.5,
            supersedes=[],
        )
        output = apply_consolidation(result)
        assert "error" in output


class TestUndoConsolidation:
    """Reversing a consolidation run."""

    @patch("tools.consolidation._get_pool")
    def test_undo_restores_originals(self, mock_pool):
        mock_conn = MagicMock()
        mock_cur = MagicMock()
        # First execute: restore originals (3 rows)
        # Second execute: delete consolidated (1 row)
        mock_cur.rowcount = 3
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_pool.return_value.connection.return_value.__enter__ = lambda s: mock_conn
        mock_pool.return_value.connection.return_value.__exit__ = MagicMock(return_value=False)

        output = undo_consolidation("test-run-1")
        assert output["run_id"] == "test-run-1"
        assert output["restored_count"] == 3
        assert output["deleted_count"] == 3  # Same mock rowcount


class TestFindConsolidationCandidates:
    """ANN-based candidate selection."""

    @patch("tools.consolidation._get_pool")
    def test_empty_when_few_memories(self, mock_pool):
        """Returns empty when fewer memories than min_cluster_size."""
        class Desc:
            def __init__(self, name):
                self.name = name

        mock_conn = MagicMock()
        mock_cur = MagicMock()
        mock_cur.description = [Desc("id"), Desc("importance_score"), Desc("embedding")]
        mock_cur.fetchall.return_value = [("m1", 0.8, [0.1] * 384)]  # Only 1 memory
        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_pool.return_value.connection.return_value.__enter__ = lambda s: mock_conn
        mock_pool.return_value.connection.return_value.__exit__ = MagicMock(return_value=False)

        result = find_consolidation_candidates(min_cluster_size=3)
        assert result == []

    @patch("tools.consolidation._get_pool")
    def test_clusters_found_with_similar_memories(self, mock_pool):
        """Similar memories are grouped into clusters."""
        embedding = [0.1] * 384

        mock_conn = MagicMock()
        mock_cur = MagicMock()

        # MagicMock(name=...) sets the internal mock name, NOT .name attr.
        # We need objects with actual .name attributes for zip(columns, row).
        class Desc:
            def __init__(self, name):
                self.name = name

        mock_cur.description = [Desc("id"), Desc("importance_score"), Desc("embedding")]

        memories = [
            ("m1", 0.8, embedding),
            ("m2", 0.7, embedding),
            ("m3", 0.9, embedding),
            ("m4", 0.6, embedding),
        ]

        # fetchall calls: first = all memories, then neighbors for each
        neighbors_m1 = [("m2", 0.05), ("m3", 0.06)]  # very close (distance)
        neighbors_m2 = [("m1", 0.05), ("m3", 0.06)]
        neighbors_m3 = [("m1", 0.06), ("m2", 0.06)]
        neighbors_m4 = [("m1", 1.5)]  # far away

        call_count = [0]
        def mock_fetchall():
            call_count[0] += 1
            if call_count[0] == 1:
                return memories
            idx = call_count[0] - 2
            neighbors = [neighbors_m1, neighbors_m2, neighbors_m3, neighbors_m4]
            return neighbors[idx] if idx < len(neighbors) else []

        mock_cur.fetchall = mock_fetchall

        mock_conn.cursor.return_value.__enter__ = lambda s: mock_cur
        mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_pool.return_value.connection.return_value.__enter__ = lambda s: mock_conn
        mock_pool.return_value.connection.return_value.__exit__ = MagicMock(return_value=False)

        result = find_consolidation_candidates(
            min_cluster_size=3,
            similarity_threshold=0.85,
        )
        assert len(result) >= 1
        assert result[0].size >= 3


class TestDryRunConsolidation:
    """Dry run shows candidates without making changes."""

    @patch("tools.consolidation._load_cluster_contents")
    @patch("tools.consolidation.find_consolidation_candidates")
    def test_dry_run_returns_summaries(self, mock_find, mock_load):
        cluster = MemoryCluster(
            memory_ids=["a", "b", "c"],
            avg_similarity=0.9,
            total_importance=2.4,
            contents=[
                {"document": "First memory content here"},
                {"document": "Second memory content"},
                {"document": "Third memory content"},
            ],
        )
        mock_find.return_value = [cluster]
        mock_load.return_value = cluster

        results = dry_run_consolidation()
        assert len(results) == 1
        assert results[0]["size"] == 3
        assert results[0]["cluster"] == 1
        assert len(results[0]["previews"]) == 3


class TestConfigGetters:
    """Config getters for decay, ranking, consolidation."""

    @patch("tools.config.get_config", return_value={})
    def test_get_decay_config_defaults(self, mock_cfg):
        from tools.config import get_decay_config
        cfg = get_decay_config()
        assert cfg["enabled"] is True
        assert cfg["rate_per_month"] == 0.05
        assert cfg["min_importance"] == 0.05

    @patch("tools.config.get_config", return_value={})
    def test_get_ranking_config_defaults(self, mock_cfg):
        from tools.config import get_ranking_config
        cfg = get_ranking_config()
        # Unified scoring (Layer 4): single additive importance nudge —
        # the old similarity_weight/importance_weight blend is gone.
        assert "similarity_weight" not in cfg
        assert cfg["importance_weight"] == 0.24
        assert cfg["overfetch_factor"] == 5

    @patch("tools.config.get_config", return_value={})
    def test_get_consolidation_config_defaults(self, mock_cfg):
        from tools.config import get_consolidation_config
        cfg = get_consolidation_config()
        assert cfg["enabled"] is False
        assert cfg["similarity_threshold"] == 0.85
        assert cfg["auto_apply"] is False

    @patch("tools.config.get_config", return_value={"memory": {"decay": {"rate_per_month": 0.1}}})
    def test_decay_config_override(self, mock_cfg):
        from tools.config import get_decay_config
        cfg = get_decay_config()
        assert cfg["rate_per_month"] == 0.1
        assert cfg["enabled"] is True  # default preserved
