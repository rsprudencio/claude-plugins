"""Cross-process write lock for ChromaDB operations.

Uses fcntl.flock() for advisory file locking. The OS automatically releases
the lock if the holding process crashes — no stale lock cleanup needed.

All ChromaDB write operations should be wrapped in chroma_write_lock() to
serialize concurrent writes from the MCP server, prompt_search hook, and
extract_observation hook.
"""

import fcntl
import logging
import os
import time
from contextlib import contextmanager

from .paths import get_path

logger = logging.getLogger("jarvis-core")

# Shutdown coordination
_shutting_down = False


def begin_shutdown():
    """Signal that no new writes should be accepted.

    Called during graceful shutdown. In-flight writes (already holding the lock)
    complete normally; new lock acquisitions are rejected immediately.
    """
    global _shutting_down
    _shutting_down = True


def is_shutting_down() -> bool:
    """Return True if the server is shutting down."""
    return _shutting_down


def _default_lock_path() -> str:
    """Resolve the lock file path, co-located with the ChromaDB data."""
    db_dir = get_path("db_path", ensure_exists=True)
    return os.path.join(db_dir, ".write_lock")


@contextmanager
def chroma_write_lock(timeout: float = 10.0, _lock_path: str = ""):
    """Exclusive write lock for ChromaDB operations.

    Uses fcntl.flock() with polling and exponential backoff for timeout support.
    The OS releases the lock automatically on process exit/crash.

    Args:
        timeout: Maximum seconds to wait for the lock (0 = non-blocking).
                 Raises TimeoutError if exceeded.
        _lock_path: Override lock file path (for testing). Empty string = default.

    Raises:
        TimeoutError: If the lock cannot be acquired within timeout.
        RuntimeError: If the server is shutting down.
    """
    if _shutting_down:
        raise RuntimeError("ChromaDB is shutting down — write rejected")

    lock_file = _lock_path or _default_lock_path()

    # Ensure parent directory exists
    os.makedirs(os.path.dirname(lock_file), exist_ok=True)

    fd = os.open(lock_file, os.O_CREAT | os.O_RDWR)
    acquired = False
    try:
        if timeout == 0:
            # Non-blocking: try once
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
            except (BlockingIOError, OSError):
                raise TimeoutError(
                    "ChromaDB write lock not available (non-blocking)"
                )
        else:
            # Polling with exponential backoff
            deadline = time.monotonic() + timeout
            delay = 0.01  # Start at 10ms
            max_delay = 0.5  # Cap at 500ms

            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired = True
                    break  # Lock acquired
                except (BlockingIOError, OSError):
                    if time.monotonic() >= deadline:
                        raise TimeoutError(
                            f"ChromaDB write lock timeout after {timeout}s"
                        )
                    time.sleep(delay)
                    delay = min(delay * 2, max_delay)

        # Lock acquired — yield to caller
        yield

    finally:
        if acquired:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        os.close(fd)
