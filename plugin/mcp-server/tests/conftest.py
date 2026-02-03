"""Pytest fixtures for Jarvis Tools tests."""
import json
import os
import tempfile
from pathlib import Path
from typing import Generator

import pytest


@pytest.fixture
def temp_vault() -> Generator[Path, None, None]:
    """Create a temporary vault directory for testing."""
    with tempfile.TemporaryDirectory(prefix="jarvis_test_vault_") as tmpdir:
        vault_path = Path(tmpdir)
        # Create some structure
        (vault_path / "journal" / "2026" / "01").mkdir(parents=True)
        (vault_path / "notes").mkdir()
        (vault_path / "inbox").mkdir()
        # Initialize as git repo (some tests may need this)
        os.system(f"cd {vault_path} && git init -q")
        yield vault_path


@pytest.fixture
def temp_config_dir() -> Generator[Path, None, None]:
    """Create a temporary config directory."""
    with tempfile.TemporaryDirectory(prefix="jarvis_test_config_") as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def mock_config(temp_vault: Path, temp_config_dir: Path, monkeypatch):
    """Mock the config module to use temporary paths."""
    import tools.config as config_module

    # Clear any cached config
    config_module._config_cache = None

    # Create a valid config
    config_file = temp_config_dir / "config.json"
    config_data = {
        "vault_path": str(temp_vault),
        "vault_confirmed": True,
        "configured_at": "2026-02-02T12:00:00Z",
        "version": "0.2.0"
    }
    config_file.write_text(json.dumps(config_data))

    # Monkey-patch the config path
    def mock_get_config():
        if config_module._config_cache is None:
            if config_file.exists():
                config_module._config_cache = json.loads(config_file.read_text())
            else:
                config_module._config_cache = {}
        return config_module._config_cache

    monkeypatch.setattr(config_module, "get_config", mock_get_config)

    # Return helper to modify config
    class ConfigHelper:
        def __init__(self):
            self.path = config_file
            self.vault_path = temp_vault

        def set(self, **kwargs):
            """Update config values."""
            data = json.loads(self.path.read_text()) if self.path.exists() else {}
            data.update(kwargs)
            self.path.write_text(json.dumps(data))
            config_module._config_cache = None  # Clear cache

        def delete_key(self, key: str):
            """Remove a key from config."""
            data = json.loads(self.path.read_text())
            data.pop(key, None)
            self.path.write_text(json.dumps(data))
            config_module._config_cache = None

        def delete_file(self):
            """Delete the config file entirely."""
            if self.path.exists():
                self.path.unlink()
            config_module._config_cache = None

    return ConfigHelper()


@pytest.fixture
def unconfirmed_config(mock_config):
    """Config without vault_confirmed flag."""
    mock_config.delete_key("vault_confirmed")
    return mock_config


@pytest.fixture
def no_config(mock_config):
    """No config file exists."""
    mock_config.delete_file()
    return mock_config
