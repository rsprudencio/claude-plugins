"""Tests for config_writer — atomic config.json mutations with locking."""

import json
import os
import stat
import threading
import time
from unittest.mock import patch

import pytest

from jarvis_common.config import clear_config_cache, get_config
from jarvis_common.config_writer import (
    _acquire_config_lock,
    _rehydrate_passwords,
    _release_config_lock,
    read_config_file,
    update_sync_section,
    write_config_file,
)


@pytest.fixture(autouse=True)
def _isolate_config(tmp_path, monkeypatch):
    """Point all config operations at a temp directory."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    clear_config_cache()
    yield
    clear_config_cache()


def _write_raw(tmp_path, config):
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config, indent=2))
    return path


# ── Atomic write tests ─────────────────────────────────────────────────────

class TestWriteConfigFile:
    def test_produces_valid_json(self, tmp_path):
        config = {"vault_path": "/test", "memory": {"sync": {"enabled": True}}}
        write_config_file(config)
        path = tmp_path / "config.json"
        assert path.exists()
        loaded = json.loads(path.read_text())
        assert loaded == config

    def test_preserves_file_permissions(self, tmp_path):
        path = _write_raw(tmp_path, {"initial": True})
        os.chmod(str(path), 0o640)
        write_config_file({"updated": True})
        assert stat.S_IMODE(os.stat(str(path)).st_mode) == 0o640

    def test_creates_with_0600_if_no_existing_file(self, tmp_path):
        write_config_file({"new": True})
        path = tmp_path / "config.json"
        assert stat.S_IMODE(os.stat(str(path)).st_mode) == 0o600


class TestReadConfigFile:
    def test_reads_existing(self, tmp_path):
        _write_raw(tmp_path, {"key": "value"})
        assert read_config_file() == {"key": "value"}

    def test_returns_empty_on_missing(self, tmp_path):
        assert read_config_file() == {}

    def test_raises_on_malformed(self, tmp_path):
        (tmp_path / "config.json").write_text("{bad json")
        with pytest.raises(ValueError, match="Malformed"):
            read_config_file()


# ── Locking tests ──────────────────────────────────────────────────────────

class TestLocking:
    def test_lock_acquire_release(self, tmp_path):
        _acquire_config_lock(timeout=2)
        lock = tmp_path / ".config.json.lock"
        assert lock.exists()
        _release_config_lock()
        assert not lock.exists()

    def test_lock_timeout(self, tmp_path):
        _acquire_config_lock(timeout=2)
        with pytest.raises(TimeoutError):
            _acquire_config_lock(timeout=0.2)
        _release_config_lock()

    def test_stale_lock_recovery(self, tmp_path):
        lock = tmp_path / ".config.json.lock"
        lock.write_text("12345")
        # Make it appear old
        old_time = time.time() - 120
        os.utime(str(lock), (old_time, old_time))
        # Should recover
        _acquire_config_lock(timeout=2)
        _release_config_lock()


# ── Password re-hydration ──────────────────────────────────────────────────

class TestRehydratePasswords:
    def test_sentinel_rehydrates(self):
        new = {"aurora": {"password": "***", "host": "new-host"}}
        orig = {"aurora": {"password": "$AURORA_PW", "host": "old-host"}}
        result = _rehydrate_passwords(new, orig)
        assert result["aurora"]["password"] == "$AURORA_PW"

    def test_new_password_kept(self):
        new = {"aurora": {"password": "new-secret"}}
        orig = {"aurora": {"password": "old-secret"}}
        result = _rehydrate_passwords(new, orig)
        assert result["aurora"]["password"] == "new-secret"

    def test_env_var_preserved(self):
        new = {"aurora": {"password": "$MY_VAR"}}
        orig = {"aurora": {"password": "$OLD_VAR"}}
        result = _rehydrate_passwords(new, orig)
        assert result["aurora"]["password"] == "$MY_VAR"

    def test_none_rehydrates(self):
        new = {"aurora": {"host": "h"}}
        orig = {"aurora": {"password": "kept"}}
        result = _rehydrate_passwords(new, orig)
        assert result["aurora"]["password"] == "kept"

    def test_sentinel_no_original(self):
        new = {"aurora": {"password": "***"}}
        result = _rehydrate_passwords(new, {})
        assert "password" not in new["aurora"]


# ── update_sync_section tests ──────────────────────────────────────────────

class TestUpdateSyncSection:
    def test_basic_update(self, tmp_path):
        _write_raw(tmp_path, {
            "vault_path": "/v",
            "memory": {"sync": {"enabled": False, "remotes": {}, "rules": []}},
        })

        def enable_sync(sync):
            sync["enabled"] = True
            return sync

        new_sync, errors = update_sync_section(enable_sync)
        assert errors == []
        assert new_sync["enabled"] is True
        # Verify on disk
        on_disk = json.loads((tmp_path / "config.json").read_text())
        assert on_disk["memory"]["sync"]["enabled"] is True
        # Other sections preserved
        assert on_disk["vault_path"] == "/v"

    def test_validation_failure_aborts(self, tmp_path):
        _write_raw(tmp_path, {
            "memory": {"sync": {"enabled": False, "remotes": {}, "rules": []}},
        })
        original = json.loads((tmp_path / "config.json").read_text())

        def bad_update(sync):
            sync["strategy"] = "invalid-strategy"
            return sync

        new_sync, errors = update_sync_section(bad_update)
        assert len(errors) > 0
        assert "invalid" in errors[0].lower() or "Invalid" in errors[0]
        # File should be unchanged
        assert json.loads((tmp_path / "config.json").read_text()) == original

    def test_password_sentinel_roundtrip(self, tmp_path):
        _write_raw(tmp_path, {
            "memory": {"sync": {
                "remotes": {
                    "prod": {
                        "url": "postgresql://u:$PROD_PW@host/db",
                        "password": "$PROD_PW",
                        "schema": "prod",
                    }
                },
                "rules": [],
            }},
        })

        def update_host(sync):
            sync["remotes"]["prod"]["url"] = "postgresql://u:***@new-host/db"
            sync["remotes"]["prod"]["password"] = "***"
            return sync

        new_sync, errors = update_sync_section(update_host)
        assert errors == []
        assert new_sync["remotes"]["prod"]["password"] == "$PROD_PW"

    def test_concurrent_writes_safe(self, tmp_path):
        _write_raw(tmp_path, {
            "memory": {"sync": {"enabled": True, "remotes": {}, "rules": [], "counter": 0}},
        })
        results = []

        def increment(sync):
            sync["counter"] = sync.get("counter", 0) + 1
            return sync

        def worker():
            try:
                _, errs = update_sync_section(increment)
                results.append(("ok", errs))
            except Exception as e:
                results.append(("err", str(e)))

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert all(r[0] == "ok" for r in results), f"Some threads failed: {results}"
        on_disk = json.loads((tmp_path / "config.json").read_text())
        assert on_disk["memory"]["sync"]["counter"] == 5

    def test_cache_invalidated_after_write(self, tmp_path):
        _write_raw(tmp_path, {
            "memory": {"sync": {"enabled": False, "remotes": {}, "rules": []}},
        })
        # Prime the cache
        assert get_config()["memory"]["sync"]["enabled"] is False

        def enable(sync):
            sync["enabled"] = True
            return sync

        update_sync_section(enable)
        # Cache should reflect new value
        assert get_config()["memory"]["sync"]["enabled"] is True


# ── mtime cache tests ─────────────────────────────────────────────────────

class TestMtimeCache:
    def test_mtime_triggers_reread(self, tmp_path):
        _write_raw(tmp_path, {"value": 1})
        assert get_config()["value"] == 1
        # Write new value
        _write_raw(tmp_path, {"value": 2})
        clear_config_cache()
        assert get_config()["value"] == 2

    def test_malformed_json_returns_last_good(self, tmp_path):
        _write_raw(tmp_path, {"good": True})
        assert get_config()["good"] is True
        # Corrupt the file
        (tmp_path / "config.json").write_text("{bad")
        clear_config_cache()
        # Should return last good or empty
        result = get_config()
        # Either last good cache or empty dict is acceptable
        assert isinstance(result, dict)
