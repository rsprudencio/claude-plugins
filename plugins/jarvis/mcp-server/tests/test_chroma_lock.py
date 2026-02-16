"""Tests for ChromaDB write lock module."""

import os
import tempfile
import threading
import time

import pytest
from unittest.mock import patch

from tools.chroma_lock import (
    chroma_write_lock,
    begin_shutdown,
    is_shutting_down,
    _default_lock_path,
)


@pytest.fixture
def lock_path(tmp_path):
    """Provide a temporary lock file path for testing."""
    return str(tmp_path / ".write_lock")


@pytest.fixture(autouse=True)
def reset_shutdown_flag():
    """Reset the shutdown flag before each test."""
    import tools.chroma_lock as mod
    mod._shutting_down = False
    yield
    mod._shutting_down = False


class TestChromaWriteLock:
    """Tests for the chroma_write_lock context manager."""

    def test_basic_acquire_release(self, lock_path):
        """Lock can be acquired and released normally."""
        with chroma_write_lock(_lock_path=lock_path):
            assert os.path.exists(lock_path)
        # After release, lock file still exists (just unlocked)
        assert os.path.exists(lock_path)

    def test_lock_creates_file(self, lock_path):
        """Lock file is created if it doesn't exist."""
        assert not os.path.exists(lock_path)
        with chroma_write_lock(_lock_path=lock_path):
            assert os.path.exists(lock_path)

    def test_lock_creates_parent_directory(self, tmp_path):
        """Lock file parent directories are created."""
        deep_path = str(tmp_path / "a" / "b" / ".write_lock")
        with chroma_write_lock(_lock_path=deep_path):
            assert os.path.exists(deep_path)

    def test_lock_released_on_exception(self, lock_path):
        """Lock is released even if the body raises an exception."""
        with pytest.raises(ValueError, match="test error"):
            with chroma_write_lock(_lock_path=lock_path):
                raise ValueError("test error")

        # Should be able to re-acquire immediately
        with chroma_write_lock(timeout=0, _lock_path=lock_path):
            pass

    def test_non_blocking_succeeds_when_free(self, lock_path):
        """timeout=0 succeeds when lock is free."""
        with chroma_write_lock(timeout=0, _lock_path=lock_path):
            pass

    def test_non_blocking_fails_when_held(self, lock_path):
        """timeout=0 raises TimeoutError when lock is held by another."""
        import fcntl

        # Hold the lock from outside the context manager
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            with pytest.raises(TimeoutError, match="non-blocking"):
                with chroma_write_lock(timeout=0, _lock_path=lock_path):
                    pass
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_timeout_raises_after_deadline(self, lock_path):
        """Lock acquisition times out after the specified deadline."""
        import fcntl

        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            start = time.monotonic()
            with pytest.raises(TimeoutError, match="timeout after"):
                with chroma_write_lock(timeout=0.2, _lock_path=lock_path):
                    pass
            elapsed = time.monotonic() - start
            assert elapsed >= 0.15  # Should wait close to timeout
            assert elapsed < 1.0  # But not excessively
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def test_concurrent_threads_serialize(self, lock_path):
        """Two threads holding the lock don't overlap."""
        results = []

        def worker(name, delay):
            with chroma_write_lock(_lock_path=lock_path):
                results.append(f"{name}-start")
                time.sleep(delay)
                results.append(f"{name}-end")

        t1 = threading.Thread(target=worker, args=("A", 0.05))
        t2 = threading.Thread(target=worker, args=("B", 0.05))

        t1.start()
        time.sleep(0.01)  # Give A a head start
        t2.start()

        t1.join(timeout=5)
        t2.join(timeout=5)

        # A should complete before B starts (serialized)
        assert results[0] == "A-start"
        assert results[1] == "A-end"
        assert results[2] == "B-start"
        assert results[3] == "B-end"

    def test_nested_lock_times_out(self, lock_path):
        """Nested lock acquisition from same thread times out (flock is per-fd on macOS).

        This is correct behavior — callers should not nest chroma_write_lock.
        Internal helpers (_delete_existing_chunks etc.) rely on the caller
        holding the lock instead of acquiring their own.
        """
        with chroma_write_lock(_lock_path=lock_path):
            with pytest.raises(TimeoutError):
                with chroma_write_lock(timeout=0.1, _lock_path=lock_path):
                    pass


class TestShutdownFlag:
    """Tests for graceful shutdown coordination."""

    def test_not_shutting_down_by_default(self):
        assert not is_shutting_down()

    def test_begin_shutdown_sets_flag(self):
        begin_shutdown()
        assert is_shutting_down()

    def test_shutdown_rejects_new_writes(self, lock_path):
        """New lock acquisitions are rejected during shutdown."""
        begin_shutdown()
        with pytest.raises(RuntimeError, match="shutting down"):
            with chroma_write_lock(_lock_path=lock_path):
                pass

    def test_inflight_write_completes_during_shutdown(self, lock_path):
        """A write that already holds the lock completes even after shutdown begins."""
        completed = False
        with chroma_write_lock(_lock_path=lock_path):
            # Simulate shutdown beginning while we hold the lock
            begin_shutdown()
            # We should still be able to finish our work
            completed = True
        assert completed

    def test_shutdown_then_new_write_rejected(self, lock_path):
        """After in-flight completes and shutdown is set, new writes fail."""
        with chroma_write_lock(_lock_path=lock_path):
            begin_shutdown()

        with pytest.raises(RuntimeError, match="shutting down"):
            with chroma_write_lock(_lock_path=lock_path):
                pass


class TestDefaultLockPath:
    """Tests for lock path resolution."""

    @patch("tools.chroma_lock.get_path")
    def test_default_path_uses_db_path(self, mock_get_path, tmp_path):
        """Lock file is co-located with the ChromaDB data directory."""
        mock_get_path.return_value = str(tmp_path / "memory_db")
        os.makedirs(str(tmp_path / "memory_db"), exist_ok=True)
        path = _default_lock_path()
        assert path == str(tmp_path / "memory_db" / ".write_lock")
        mock_get_path.assert_called_once_with("db_path", ensure_exists=True)
