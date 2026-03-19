"""Integration tests for staleness tracking in query_vault and semantic_context."""

import time

import pytest

from tools.query import query_vault, semantic_context
from tools.staleness import MTIME_TOLERANCE
from tools.content import content_write


class TestQueryVaultStaleness:
    """Staleness integration tests for query_vault()."""

    def _write_obs_with_mtimes(self, file_mtimes: dict, content: str = "Test observation"):
        """Helper: write an observation with file_mtimes metadata."""
        return content_write(
            content=content,
            content_type="observation",
            importance_score=0.8,
            extra_metadata={
                "relevant_files": ",".join(file_mtimes.keys()),
                "file_mtimes": file_mtimes,
            },
        )

    def test_stale_observation_penalized(self, mock_config, tmp_path):
        """Stale observations get relevance penalty and stale flag in results."""
        f = tmp_path / "changed.py"
        f.write_text("original code")
        mtimes = {str(f): f.stat().st_mtime}

        self._write_obs_with_mtimes(mtimes, content="Python code review findings")

        # Modify the file so it becomes stale
        time.sleep(MTIME_TOLERANCE + 0.05)
        f.write_text("modified code")

        result = query_vault("Python code review")
        assert result["success"] is True
        assert len(result["results"]) > 0

        obs = result["results"][0]
        assert obs["stale"] is True
        assert str(f) in obs["stale_files"]

    def test_fresh_observation_no_stale_flag(self, mock_config, tmp_path):
        """Fresh observations have no stale field in results."""
        f = tmp_path / "unchanged.py"
        f.write_text("stable code")
        mtimes = {str(f): f.stat().st_mtime}

        self._write_obs_with_mtimes(mtimes, content="Stable architecture observation")

        result = query_vault("Stable architecture observation")
        assert result["success"] is True
        assert len(result["results"]) > 0

        obs = result["results"][0]
        assert "stale" not in obs

    def test_no_mtimes_metadata(self, mock_config):
        """Observations without file_mtimes metadata are unaffected."""
        content_write(
            content="Observation without file tracking",
            content_type="observation",
            importance_score=0.8,
        )

        result = query_vault("Observation without file tracking")
        assert result["success"] is True
        assert len(result["results"]) > 0

        obs = result["results"][0]
        assert "stale" not in obs

    def test_staleness_disabled_via_config(self, mock_config, tmp_path):
        """When staleness is disabled, no penalty or stale flag applied."""
        # Disable staleness in config
        mock_config.set(memory={"staleness": {"enabled": False}})

        f = tmp_path / "changed.py"
        f.write_text("original")
        mtimes = {str(f): f.stat().st_mtime}

        self._write_obs_with_mtimes(mtimes, content="Disabled staleness observation")

        # Modify the file
        time.sleep(MTIME_TOLERANCE + 0.05)
        f.write_text("modified")

        result = query_vault("Disabled staleness observation")
        assert result["success"] is True
        assert len(result["results"]) > 0

        obs = result["results"][0]
        assert "stale" not in obs


class TestSemanticContextStaleness:
    """Staleness integration tests for semantic_context()."""

    def test_stale_observation_in_semantic_context(self, mock_config, tmp_path):
        """semantic_context marks stale observations."""
        f = tmp_path / "evolving.py"
        f.write_text("v1 implementation")
        mtimes = {str(f): f.stat().st_mtime}

        content_write(
            content="Semantic context staleness test observation",
            content_type="observation",
            importance_score=0.9,
            extra_metadata={
                "relevant_files": str(f),
                "file_mtimes": mtimes,
            },
        )

        # Make file stale
        time.sleep(MTIME_TOLERANCE + 0.05)
        f.write_text("v2 implementation")

        result = semantic_context("staleness test observation", threshold=0.0, budget=10000)
        assert len(result["matches"]) > 0

        match = result["matches"][0]
        assert match.get("stale") is True

    def test_multiple_files_only_changed_listed(self, mock_config, tmp_path):
        """When only some tracked files change, only those appear in stale_files."""
        fresh = tmp_path / "fresh.py"
        stale = tmp_path / "stale.py"
        fresh.write_text("stable")
        stale.write_text("will change")

        mtimes = {str(fresh): fresh.stat().st_mtime, str(stale): stale.stat().st_mtime}

        content_write(
            content="Multi-file staleness tracking test",
            content_type="observation",
            importance_score=0.9,
            extra_metadata={
                "relevant_files": f"{fresh},{stale}",
                "file_mtimes": mtimes,
            },
        )

        # Only modify one file
        time.sleep(MTIME_TOLERANCE + 0.05)
        stale.write_text("changed content")

        result = query_vault("Multi-file staleness tracking")
        assert result["success"] is True
        assert len(result["results"]) > 0

        obs = result["results"][0]
        assert obs["stale"] is True
        assert str(stale) in obs["stale_files"]
        assert str(fresh) not in obs["stale_files"]


class TestAnnotateStalenessRemoteSkip:
    """Remote schema entries must skip staleness checks.

    Remote observations have file_mtimes from the remote machine. Those paths
    never exist locally, so os.stat() always fails → permanent false-positive
    staleness penalty. The fix: skip entries whose _schema starts with 'remote_'.
    """

    def test_remote_entries_not_penalized(self):
        """Entries from remote schemas keep their original relevance."""
        from tools.query import _annotate_staleness

        entry = {
            "doc_id": "obs::1234567890",
            "metadata": {
                "file_mtimes": {
                    "/Users/other-user/.claude/projects/memory.md": 1772036655.36,
                },
            },
            "relevance": 0.85,
            "_schema": "remote_personio",
        }
        _annotate_staleness([entry], {"penalty": 0.15})

        assert entry["relevance"] == 0.85
        assert "is_stale" not in entry

    def test_local_entries_still_penalized(self, tmp_path):
        """Local entries with missing files are still penalized."""
        from tools.query import _annotate_staleness

        nonexistent = str(tmp_path / "deleted.py")
        entry = {
            "doc_id": "obs::9876543210",
            "metadata": {
                "file_mtimes": {nonexistent: 1772036655.36},
            },
            "relevance": 0.85,
            "_schema": "local",
        }
        _annotate_staleness([entry], {"penalty": 0.15})

        assert entry["relevance"] == pytest.approx(0.70, abs=0.01)
        assert entry.get("is_stale") is True
