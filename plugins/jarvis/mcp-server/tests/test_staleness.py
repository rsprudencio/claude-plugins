"""Unit tests for observation staleness tracking module."""

import os
import time

import pytest

from tools.staleness import (
    MTIME_TOLERANCE,
    check_staleness,
    deserialize_mtimes,
    record_file_mtimes,
)


class TestRecordFileMtimes:
    """Tests for record_file_mtimes()."""

    def test_basic_recording(self, tmp_path):
        """Records mtime for existing files."""
        f1 = tmp_path / "a.py"
        f2 = tmp_path / "b.py"
        f1.write_text("content a")
        f2.write_text("content b")

        mtimes = record_file_mtimes([str(f1), str(f2)])

        assert len(mtimes) == 2
        assert mtimes[str(f1)] > 0
        assert mtimes[str(f2)] > 0

    def test_missing_file(self, tmp_path):
        """Missing files recorded as 0.0."""
        missing = str(tmp_path / "nonexistent.py")
        mtimes = record_file_mtimes([missing])

        assert mtimes[missing] == 0.0

    def test_empty_input(self):
        """Empty list returns empty dict."""
        assert record_file_mtimes([]) == {}


class TestDeserializeMtimes:
    """Tests for deserialize_mtimes()."""

    def test_dict_passthrough(self, tmp_path):
        """Native dict (from pgvector jsonb) passes through."""
        f = tmp_path / "test.py"
        f.write_text("content")
        original = record_file_mtimes([str(f)])

        recovered = deserialize_mtimes(original)
        assert recovered == original

    def test_empty_dict(self):
        """Empty dict returns empty dict."""
        assert deserialize_mtimes({}) == {}

    def test_non_dict_returns_empty(self):
        """Non-dict input returns empty dict."""
        assert deserialize_mtimes("a string") == {}
        assert deserialize_mtimes("") == {}
        assert deserialize_mtimes(None) == {}
        assert deserialize_mtimes([1, 2, 3]) == {}
        assert deserialize_mtimes(42) == {}

    def test_coerces_values_to_float(self):
        """String-valued mtimes are coerced to float."""
        assert deserialize_mtimes({"a.py": "123.4"}) == {"a.py": 123.4}

    def test_bad_values_return_empty(self):
        """Non-numeric values cause empty dict return."""
        assert deserialize_mtimes({"a.py": "not a number"}) == {}


class TestCheckStaleness:
    """Tests for check_staleness()."""

    def test_no_changes(self, tmp_path):
        """Files unchanged since recording are not stale."""
        f = tmp_path / "unchanged.py"
        f.write_text("original")

        mtimes = record_file_mtimes([str(f)])
        result = check_staleness(mtimes)

        assert result["is_stale"] is False
        assert result["stale_count"] == 0
        assert result["total_count"] == 1
        assert result["stale_files"] == []

    def test_modified_file(self, tmp_path):
        """Modified file is detected as stale."""
        f = tmp_path / "changing.py"
        f.write_text("original")

        mtimes = record_file_mtimes([str(f)])

        # Ensure mtime changes (sleep past tolerance + filesystem granularity)
        time.sleep(MTIME_TOLERANCE + 0.05)
        f.write_text("modified")

        result = check_staleness(mtimes)

        assert result["is_stale"] is True
        assert result["stale_count"] == 1
        assert str(f) in result["stale_files"]

    def test_deleted_file(self, tmp_path):
        """Deleted file (was present at recording) is detected as stale."""
        f = tmp_path / "will_be_deleted.py"
        f.write_text("temporary")

        mtimes = record_file_mtimes([str(f)])
        f.unlink()

        result = check_staleness(mtimes)

        assert result["is_stale"] is True
        assert result["stale_count"] == 1
        assert str(f) in result["stale_files"]

    def test_deleted_file_was_missing(self, tmp_path):
        """File that was missing at recording time and still missing is not stale."""
        missing = str(tmp_path / "never_existed.py")
        mtimes = {missing: 0.0}  # Recorded as missing

        result = check_staleness(mtimes)

        assert result["is_stale"] is False
        assert result["stale_count"] == 0

    def test_empty_input(self):
        """Empty mtimes dict is not stale."""
        result = check_staleness({})

        assert result["is_stale"] is False
        assert result["stale_count"] == 0
        assert result["total_count"] == 0

    def test_mixed_fresh_and_stale(self, tmp_path):
        """Mix of fresh and stale files reports only stale ones."""
        fresh = tmp_path / "fresh.py"
        stale = tmp_path / "stale.py"
        fresh.write_text("content")
        stale.write_text("content")

        mtimes = record_file_mtimes([str(fresh), str(stale)])

        # Only modify one file
        time.sleep(MTIME_TOLERANCE + 0.05)
        stale.write_text("changed")

        result = check_staleness(mtimes)

        assert result["is_stale"] is True
        assert result["stale_count"] == 1
        assert result["total_count"] == 2
        assert str(stale) in result["stale_files"]
        assert str(fresh) not in result["stale_files"]
