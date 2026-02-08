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


@pytest.fixture(autouse=True)
def cleanup_chroma_client():
    """Clean up ChromaDB client after each test to prevent file handle leaks."""
    yield
    # After test completes, reset the global client
    import tools.memory as memory_module
    if hasattr(memory_module, '_chroma_client') and memory_module._chroma_client is not None:
        try:
            # Close any open connections
            del memory_module._chroma_client
        except Exception:
            pass
        finally:
            memory_module._chroma_client = None


@pytest.fixture
def git_repo(temp_vault: Path) -> Path:
    """Vault with initialized git repo and sample commits."""
    # Git repo already initialized in temp_vault
    # Add git config for tests
    os.system(f'cd {temp_vault} && git config user.email "test@example.com"')
    os.system(f'cd {temp_vault} && git config user.name "Test User"')

    # Create initial commit
    test_file = temp_vault / "test.txt"
    test_file.write_text("Initial content")
    os.system(f'cd {temp_vault} && git add test.txt && git commit -q -m "Initial commit"')

    return temp_vault


@pytest.fixture
def git_repo_with_jarvis_commits(git_repo: Path) -> Path:
    """Git repo with sample JARVIS protocol commits."""
    # Create a journal entry
    journal_file = git_repo / "journal" / "2026" / "01" / "20260123153045-test-entry.md"
    journal_file.write_text("# Test Entry\n\nTest content")

    # Commit with JARVIS protocol tag
    os.system(f'cd {git_repo} && git add {journal_file}')
    commit_msg = 'Jarvis CREATE: Test journal entry\n\n[JARVIS:Cc:20260123153045]'
    os.system(f'cd {git_repo} && git commit -q -m "{commit_msg}"')

    # Create another commit
    note_file = git_repo / "notes" / "test-note.md"
    note_file.write_text("# Test Note")
    os.system(f'cd {git_repo} && git add {note_file}')
    edit_msg = 'Jarvis EDIT: Update test note\n\n[JARVIS:Ea]'
    os.system(f'cd {git_repo} && git commit -q -m "{edit_msg}"')

    return git_repo


@pytest.fixture
def mock_subprocess(monkeypatch):
    """Mock subprocess.run for testing command failures."""
    import subprocess
    from unittest.mock import Mock

    class SubprocessMock:
        """Helper to mock subprocess.run with custom behaviors."""
        def __init__(self):
            self.call_count = 0
            self.mock_return = None
            self.mock_side_effect = None

        def set_return(self, returncode=0, stdout="", stderr=""):
            """Set what subprocess.run should return."""
            result = Mock()
            result.returncode = returncode
            result.stdout = stdout
            result.stderr = stderr
            self.mock_return = result

        def set_side_effect(self, side_effect):
            """Set a side effect (e.g., exception)."""
            self.mock_side_effect = side_effect

        def __call__(self, *args, **kwargs):
            """Mock implementation of subprocess.run."""
            self.call_count += 1
            if self.mock_side_effect:
                if isinstance(self.mock_side_effect, Exception):
                    raise self.mock_side_effect
                return self.mock_side_effect(*args, **kwargs)
            return self.mock_return if self.mock_return else Mock(returncode=0, stdout="", stderr="")

    mock = SubprocessMock()
    monkeypatch.setattr(subprocess, "run", mock)
    return mock
