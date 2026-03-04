"""Tests for PreCompact injection dedup gate (session-scoped)."""

import json
import os
import sys
import time
import pytest
from pathlib import Path
from unittest.mock import patch
from io import StringIO

# Import from standalone hooks-handlers location
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "hooks-handlers"))

from precompact_dedup import (
    compute_content_hash,
    read_injection_state,
    write_injection_state,
    filter_already_injected,
    clear_injection_state,
    cleanup_stale_states,
    _injection_state_path,
    MARKER_MAX_AGE_SECONDS,
)


@pytest.fixture
def temp_state_dir(tmp_path):
    """Use a temporary directory for session-scoped state files."""
    with patch("precompact_dedup.STATE_DIR", tmp_path):
        yield tmp_path


class TestComputeContentHash:
    """Tests for content hashing."""

    def test_deterministic(self):
        h1 = compute_content_hash("hello world")
        h2 = compute_content_hash("hello world")
        assert h1 == h2

    def test_normalized_whitespace(self):
        h1 = compute_content_hash("hello  world")
        h2 = compute_content_hash("hello world")
        assert h1 == h2

    def test_case_insensitive(self):
        h1 = compute_content_hash("Hello World")
        h2 = compute_content_hash("hello world")
        assert h1 == h2

    def test_different_content_different_hash(self):
        h1 = compute_content_hash("hello world")
        h2 = compute_content_hash("goodbye world")
        assert h1 != h2

    def test_truncated_to_16_chars(self):
        h = compute_content_hash("test content")
        assert len(h) == 16

    def test_empty_string(self):
        h = compute_content_hash("")
        assert len(h) == 16


class TestInjectionState:
    """Tests for session-scoped injection state read/write."""

    def test_write_and_read(self, temp_state_dir):
        write_injection_state("session-1", ["hash1", "hash2"], ["src1", "src2"])
        state = read_injection_state("session-1")
        assert state["session_id"] == "session-1"
        assert set(state["content_hashes"]) == {"hash1", "hash2"}
        assert set(state["sources"]) == {"src1", "src2"}
        assert "timestamp" in state

    def test_read_nonexistent(self, temp_state_dir):
        state = read_injection_state("session-1")
        assert state == {}

    def test_read_empty_session_id(self, temp_state_dir):
        """Empty session_id returns empty dict."""
        state = read_injection_state("")
        assert state == {}

    def test_write_empty_session_id_noop(self, temp_state_dir):
        """Write with empty session_id is a no-op."""
        write_injection_state("", ["hash1"], ["src1"])
        assert not list(temp_state_dir.glob("*_injection.json"))

    def test_session_scoped_file_path(self, temp_state_dir):
        """State file is named <session_id>_injection.json."""
        write_injection_state("abc-123", ["hash1"], ["src1"])
        assert (temp_state_dir / "abc-123_injection.json").exists()

    def test_write_merges_hashes(self, temp_state_dir):
        """Subsequent writes merge (accumulate) hashes, not overwrite."""
        write_injection_state("session-1", ["hash1", "hash2"], ["src1", "src2"])
        write_injection_state("session-1", ["hash2", "hash3"], ["src2", "src3"])
        state = read_injection_state("session-1")
        assert set(state["content_hashes"]) == {"hash1", "hash2", "hash3"}
        assert set(state["sources"]) == {"src1", "src2", "src3"}

    def test_concurrent_sessions_isolated(self, temp_state_dir):
        """State for session A does not affect session B."""
        write_injection_state("session-a", ["hash-a"], ["src-a"])
        write_injection_state("session-b", ["hash-b"], ["src-b"])

        state_a = read_injection_state("session-a")
        state_b = read_injection_state("session-b")

        assert state_a["content_hashes"] == ["hash-a"]
        assert state_b["content_hashes"] == ["hash-b"]


class TestFilterAlreadyInjected:
    """Tests for the dedup filter."""

    def test_no_state_returns_all(self, temp_state_dir):
        matches = [
            {"content": "memory one", "source": "vault::a.md"},
            {"content": "memory two", "source": "vault::b.md"},
        ]
        filtered = filter_already_injected(matches, "session-1")
        assert len(filtered) == 2

    def test_no_session_id_returns_all(self, temp_state_dir):
        """Without session_id, no filtering occurs."""
        write_injection_state("session-1", [compute_content_hash("memory one")], ["src1"])
        matches = [{"content": "memory one", "source": "vault::a.md"}]
        filtered = filter_already_injected(matches, "")
        assert len(filtered) == 1

    def test_filters_matching_hashes(self, temp_state_dir):
        content1 = "memory one"
        content2 = "memory two"
        content3 = "memory three"

        # Inject first two
        hashes = [compute_content_hash(content1), compute_content_hash(content2)]
        write_injection_state("session-1", hashes, ["src1", "src2"])

        # New search returns all three
        matches = [
            {"content": content1, "source": "vault::a.md"},
            {"content": content2, "source": "vault::b.md"},
            {"content": content3, "source": "vault::c.md"},
        ]
        filtered = filter_already_injected(matches, "session-1")

        # Only the new one should pass through
        assert len(filtered) == 1
        assert filtered[0]["content"] == content3

    def test_all_filtered(self, temp_state_dir):
        content1 = "already seen"
        hashes = [compute_content_hash(content1)]
        write_injection_state("session-1", hashes, ["src1"])

        matches = [{"content": content1, "source": "vault::a.md"}]
        filtered = filter_already_injected(matches, "session-1")
        assert len(filtered) == 0

    def test_none_filtered_new_content(self, temp_state_dir):
        hashes = [compute_content_hash("old content")]
        write_injection_state("session-1", hashes, ["src1"])

        matches = [
            {"content": "completely new 1", "source": "vault::a.md"},
            {"content": "completely new 2", "source": "vault::b.md"},
        ]
        filtered = filter_already_injected(matches, "session-1")
        assert len(filtered) == 2

    def test_expired_state_returns_all(self, temp_state_dir):
        """State older than 24h is ignored — all matches pass through."""
        content1 = "memory one"
        hashes = [compute_content_hash(content1)]
        write_injection_state("session-1", hashes, ["src1"])

        # Manually set old timestamp
        state_file = temp_state_dir / "session-1_injection.json"
        with open(state_file) as f:
            data = json.load(f)
        data["timestamp"] = time.time() - MARKER_MAX_AGE_SECONDS - 1
        with open(state_file, "w") as f:
            json.dump(data, f)

        matches = [{"content": content1, "source": "vault::a.md"}]
        filtered = filter_already_injected(matches, "session-1")
        assert len(filtered) == 1  # Not filtered because state is expired


class TestClearInjectionState:
    """Tests for clearing injection state (PreCompact hook)."""

    def test_clears_existing_state(self, temp_state_dir):
        write_injection_state("session-1", ["hash1"], ["src1"])
        assert (temp_state_dir / "session-1_injection.json").exists()

        clear_injection_state("session-1")
        assert not (temp_state_dir / "session-1_injection.json").exists()

    def test_clear_nonexistent_noop(self, temp_state_dir):
        """Clearing nonexistent state is a no-op."""
        clear_injection_state("session-1")  # Should not raise

    def test_clear_empty_session_id_noop(self, temp_state_dir):
        """Clearing with empty session_id is a no-op."""
        clear_injection_state("")  # Should not raise

    def test_clear_only_affects_target_session(self, temp_state_dir):
        """Clearing session A does not affect session B."""
        write_injection_state("session-a", ["hash-a"], ["src-a"])
        write_injection_state("session-b", ["hash-b"], ["src-b"])

        clear_injection_state("session-a")

        assert not (temp_state_dir / "session-a_injection.json").exists()
        assert (temp_state_dir / "session-b_injection.json").exists()
        state_b = read_injection_state("session-b")
        assert state_b["content_hashes"] == ["hash-b"]


class TestCleanupStaleStates:
    """Tests for stale injection state cleanup."""

    def test_removes_old_files(self, temp_state_dir):
        """Files older than max_age are removed."""
        old_file = temp_state_dir / "old-session_injection.json"
        old_file.write_text('{"test": true}')
        # Set mtime to 2 days ago
        old_mtime = time.time() - 2 * MARKER_MAX_AGE_SECONDS
        os.utime(old_file, (old_mtime, old_mtime))

        cleanup_stale_states()
        assert not old_file.exists()

    def test_keeps_fresh_files(self, temp_state_dir):
        """Files newer than max_age are kept."""
        fresh_file = temp_state_dir / "fresh-session_injection.json"
        fresh_file.write_text('{"test": true}')

        cleanup_stale_states()
        assert fresh_file.exists()

    def test_mixed_old_and_new(self, temp_state_dir):
        """Only old files are removed, fresh ones kept."""
        old_file = temp_state_dir / "old_injection.json"
        old_file.write_text('{"test": true}')
        old_mtime = time.time() - 2 * MARKER_MAX_AGE_SECONDS
        os.utime(old_file, (old_mtime, old_mtime))

        fresh_file = temp_state_dir / "fresh_injection.json"
        fresh_file.write_text('{"test": true}')

        cleanup_stale_states()
        assert not old_file.exists()
        assert fresh_file.exists()

    def test_empty_dir_noop(self, temp_state_dir):
        """No error on empty directory."""
        cleanup_stale_states()  # Should not raise


class TestMain:
    """Tests for main() entry point."""

    def test_cleanup_flag(self, temp_state_dir):
        """--cleanup removes stale injection state files."""
        import precompact_dedup

        old_file = temp_state_dir / "stale_injection.json"
        old_file.write_text('{"test": true}')
        old_mtime = time.time() - 2 * MARKER_MAX_AGE_SECONDS
        os.utime(old_file, (old_mtime, old_mtime))

        with patch.object(sys, "argv", ["precompact_dedup.py", "--cleanup"]):
            with pytest.raises(SystemExit) as exc:
                precompact_dedup.main()
            assert exc.value.code == 0

        assert not old_file.exists()

    def test_hook_clears_state(self, temp_state_dir):
        """--hook reads session_id from stdin and clears injection state."""
        import precompact_dedup

        write_injection_state("test-session-42", ["hash1"], ["src1"])
        assert (temp_state_dir / "test-session-42_injection.json").exists()

        hook_input = json.dumps({"session_id": "test-session-42"})
        with patch.object(sys, "argv", ["precompact_dedup.py", "--hook"]), \
             patch.object(sys, "stdin", StringIO(hook_input)):
            with pytest.raises(SystemExit) as exc:
                precompact_dedup.main()
            assert exc.value.code == 0

        assert not (temp_state_dir / "test-session-42_injection.json").exists()

    def test_hook_no_session_id_noop(self, temp_state_dir):
        """--hook with no session_id is a no-op."""
        import precompact_dedup

        hook_input = json.dumps({})
        with patch.object(sys, "argv", ["precompact_dedup.py", "--hook"]), \
             patch.object(sys, "stdin", StringIO(hook_input)):
            with pytest.raises(SystemExit) as exc:
                precompact_dedup.main()
            assert exc.value.code == 0

    def test_hook_invalid_json_noop(self, temp_state_dir):
        """--hook with invalid JSON is a no-op."""
        import precompact_dedup

        with patch.object(sys, "argv", ["precompact_dedup.py", "--hook"]), \
             patch.object(sys, "stdin", StringIO("not json")):
            with pytest.raises(SystemExit) as exc:
                precompact_dedup.main()
            assert exc.value.code == 0

    def test_no_args_exits_zero(self, temp_state_dir):
        """No arguments exits cleanly."""
        import precompact_dedup

        with patch.object(sys, "argv", ["precompact_dedup.py"]):
            with pytest.raises(SystemExit) as exc:
                precompact_dedup.main()
            assert exc.value.code == 0
