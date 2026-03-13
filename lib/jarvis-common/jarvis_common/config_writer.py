"""Atomic config.json read-modify-write with file-based locking.

Provides safe concurrent writes to ~/.jarvis/config.json using
O_CREAT|O_EXCL lock files (macOS APFS-safe). Handles:
- Atomic writes via mkstemp + fsync + os.replace
- File permission preservation
- Password sentinel re-hydration ("***" → original value)
- Validation before write
- Cache invalidation after write
"""

from __future__ import annotations

import json
import logging
import os
import stat
import tempfile
import time
from pathlib import Path
from typing import Callable

from .config import _resolve_jarvis_home, clear_config_cache
from .sync_validation import validate_sync_config

logger = logging.getLogger(__name__)


def _config_path() -> Path:
    """Return the path to config.json."""
    return _resolve_jarvis_home() / "config.json"


def _lock_path() -> Path:
    """Return the path to the config lock file."""
    return _resolve_jarvis_home() / ".config.json.lock"


def read_config_file() -> dict:
    """Read config.json from disk, bypassing cache."""
    path = _config_path()
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as e:
        raise ValueError(f"Malformed config.json: {e}") from e


def write_config_file(config: dict) -> None:
    """Atomic write: mkstemp + fchmod(original mode) + fsync + os.replace.

    Preserves the original file's permissions. If the file doesn't exist
    yet, uses 0o600 (owner read/write only).
    """
    path = _config_path()

    # Capture original file mode
    try:
        original_mode = stat.S_IMODE(os.stat(path).st_mode)
    except FileNotFoundError:
        original_mode = 0o600

    # Write to temp file in same directory (required for os.replace)
    dir_path = path.parent
    dir_path.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(dir_path), suffix=".tmp")
    try:
        data = json.dumps(config, indent=2, ensure_ascii=False) + "\n"
        os.write(fd, data.encode("utf-8"))
        os.fchmod(fd, original_mode)
        os.fsync(fd)
        os.close(fd)
        fd = -1  # Mark as closed
        os.replace(tmp_path, str(path))
    except BaseException:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _acquire_config_lock(timeout: float = 10.0) -> None:
    """Acquire exclusive lock via atomic file creation (O_CREAT|O_EXCL)."""
    lock = str(_lock_path())
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            return
        except FileExistsError:
            if time.monotonic() > deadline:
                # Check for stale lock (>60s old)
                try:
                    if time.time() - os.path.getmtime(lock) > 60:
                        os.unlink(lock)
                        continue
                except OSError:
                    pass
                raise TimeoutError("Could not acquire config lock")
            time.sleep(0.05)


def _release_config_lock() -> None:
    """Release the config lock file."""
    try:
        os.unlink(str(_lock_path()))
    except OSError:
        pass


def _rehydrate_passwords(
    new_remotes: dict, original_remotes: dict
) -> dict:
    """Re-hydrate '***' sentinel passwords from original config.

    When a client sends password='***', it means "keep the existing
    password". This merges the original password (which may be a
    literal or $ENV_VAR reference) back into the new config.
    """
    for name, remote in new_remotes.items():
        if not isinstance(remote, dict):
            continue
        pw = remote.get("password")
        if pw == "***" or pw is None:
            # Restore from original
            orig = original_remotes.get(name, {})
            if isinstance(orig, dict) and "password" in orig:
                remote["password"] = orig["password"]
            elif pw == "***":
                # Sentinel but no original — remove it
                remote.pop("password", None)
    return new_remotes


def update_sync_section(
    updater_fn: Callable[[dict], dict],
) -> tuple[dict, list[str]]:
    """Read-modify-write the memory.sync section with locking and validation.

    Steps:
    1. Acquire O_CREAT|O_EXCL lock
    2. Read config from disk (fresh)
    3. Apply updater_fn to sync section
    4. Re-hydrate password sentinels from original
    5. Validate via validate_sync_config()
    6. Atomic write if valid
    7. Clear config cache
    8. Release lock

    Args:
        updater_fn: Function that receives the current sync section dict
                    and returns the modified sync section dict.

    Returns:
        Tuple of (new_sync_section, errors).
        If errors is non-empty, the write was aborted.
    """
    _acquire_config_lock()
    try:
        original_config = read_config_file()
        memory = original_config.get("memory", {})
        if not isinstance(memory, dict):
            memory = {}
        original_sync = memory.get("sync", {})
        if not isinstance(original_sync, dict):
            original_sync = {}

        # Deep copy original remotes for re-hydration
        import copy
        original_remotes = copy.deepcopy(original_sync.get("remotes", {}))

        # Apply the updater
        new_sync = updater_fn(copy.deepcopy(original_sync))

        # Re-hydrate password sentinels
        new_remotes = new_sync.get("remotes", {})
        if isinstance(new_remotes, dict):
            _rehydrate_passwords(new_remotes, original_remotes)

        # Validate
        errors = validate_sync_config(new_sync)
        if errors:
            return new_sync, errors

        # Write back
        new_config = dict(original_config)
        new_memory = dict(memory)
        new_memory["sync"] = new_sync
        new_config["memory"] = new_memory

        write_config_file(new_config)
        clear_config_cache()

        return new_sync, []
    finally:
        _release_config_lock()
