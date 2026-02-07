"""Tests for configurable path resolution (tools/paths.py)."""
import os
from pathlib import Path

import pytest

from tools.paths import (
    get_path,
    get_relative_path,
    is_sensitive_path,
    list_all_paths,
    validate_paths_config,
    PathNotConfiguredError,
    _VAULT_RELATIVE_DEFAULTS,
    _ABSOLUTE_DEFAULTS,
    SENSITIVE_PATHS,
)


class TestGetPathDefaults:
    """get_path() returns correct defaults when no paths section in config."""

    def test_journal_jarvis(self, mock_config):
        path = get_path("journal_jarvis")
        assert path == os.path.join(str(mock_config.vault_path), "journal/jarvis")

    def test_notes(self, mock_config):
        path = get_path("notes")
        assert path == os.path.join(str(mock_config.vault_path), "notes")

    def test_inbox(self, mock_config):
        path = get_path("inbox")
        assert path == os.path.join(str(mock_config.vault_path), "inbox")

    def test_inbox_todoist(self, mock_config):
        path = get_path("inbox_todoist")
        assert path == os.path.join(str(mock_config.vault_path), "inbox/todoist")

    def test_strategic(self, mock_config):
        path = get_path("strategic")
        assert path == os.path.join(str(mock_config.vault_path), ".jarvis/strategic")

    def test_all_vault_relative_defaults_resolve(self, mock_config):
        """Every vault-relative default resolves without error."""
        for name in _VAULT_RELATIVE_DEFAULTS:
            path = get_path(name)
            assert path.startswith(str(mock_config.vault_path))


class TestGetPathAbsolute:
    """Absolute paths (memory section) resolve without vault prefix."""

    def test_db_path_default(self, mock_config):
        path = get_path("db_path")
        expected = os.path.expanduser("~/.jarvis/memory_db")
        assert path == expected

    def test_project_memories_path_default(self, mock_config):
        path = get_path("project_memories_path")
        expected = os.path.expanduser("~/.jarvis/memories")
        assert path == expected

    def test_absolute_not_prefixed_with_vault(self, mock_config):
        path = get_path("db_path")
        assert not path.startswith(str(mock_config.vault_path))


class TestGetPathConfigOverrides:
    """Config values override defaults."""

    def test_vault_relative_override(self, mock_config):
        mock_config.set(paths={"journal_jarvis": "logs/ai"})
        path = get_path("journal_jarvis")
        assert path == os.path.join(str(mock_config.vault_path), "logs/ai")

    def test_absolute_override(self, mock_config):
        mock_config.set(memory={"db_path": "/custom/chroma"})
        path = get_path("db_path")
        assert path == "/custom/chroma"

    def test_override_does_not_affect_other_paths(self, mock_config):
        mock_config.set(paths={"journal_jarvis": "logs/ai"})
        # notes should still use default
        path = get_path("notes")
        assert path == os.path.join(str(mock_config.vault_path), "notes")

    def test_tilde_expansion_in_absolute(self, mock_config):
        mock_config.set(memory={"db_path": "~/custom/db"})
        path = get_path("db_path")
        assert path == os.path.join(str(Path.home()), "custom/db")


class TestGetPathTemplateSubstitution:
    """Template variables are replaced when substitutions provided."""

    def test_yyyy_substitution(self, mock_config):
        path = get_path("journal_summaries", {"YYYY": "2026"})
        assert "2026" in path
        assert "{YYYY}" not in path

    def test_multiple_substitutions(self, mock_config):
        # Use a custom path with multiple templates
        mock_config.set(paths={"journal_summaries": "journal/{YYYY}/{MM}/summaries"})
        path = get_path("journal_summaries", {"YYYY": "2026", "MM": "02"})
        assert "2026" in path
        assert "02" in path
        assert "{YYYY}" not in path
        assert "{MM}" not in path

    def test_no_substitution_without_dict(self, mock_config):
        path = get_path("journal_summaries")
        assert "{YYYY}" in path

    def test_partial_substitution(self, mock_config):
        mock_config.set(paths={"journal_summaries": "journal/{YYYY}/{MM}/summaries"})
        path = get_path("journal_summaries", {"YYYY": "2026"})
        assert "2026" in path
        assert "{MM}" in path


class TestGetPathEnsureExists:
    """ensure_exists=True creates the directory."""

    def test_creates_missing_directory(self, mock_config):
        path = get_path("inbox_todoist", ensure_exists=True)
        assert os.path.isdir(path)

    def test_idempotent_on_existing(self, mock_config):
        path = get_path("notes", ensure_exists=True)
        assert os.path.isdir(path)
        # Call again — no error
        path2 = get_path("notes", ensure_exists=True)
        assert path == path2


class TestGetPathErrors:
    """Error cases for get_path()."""

    def test_unknown_path_raises(self, mock_config):
        with pytest.raises(PathNotConfiguredError, match="Unknown path name"):
            get_path("nonexistent_path")

    def test_no_vault_config_raises(self, no_config):
        with pytest.raises(ValueError, match="Cannot resolve"):
            get_path("journal_jarvis")

    def test_unconfirmed_vault_raises(self, unconfirmed_config):
        with pytest.raises(ValueError, match="Cannot resolve"):
            get_path("notes")

    def test_absolute_path_works_without_vault(self, no_config):
        """Absolute paths don't need vault config."""
        path = get_path("db_path")
        assert path == os.path.expanduser("~/.jarvis/memory_db")


class TestGetPathNormalization:
    """Path normalization removes redundant separators and ./ components."""

    def test_trailing_slash_stripped(self, mock_config):
        mock_config.set(paths={"inbox": "inbox/"})
        path = get_path("inbox")
        assert not path.endswith("/")

    def test_double_slash_normalized(self, mock_config):
        mock_config.set(paths={"inbox": "inbox//todoist"})
        path = get_path("inbox")
        assert "//" not in path


class TestGetRelativePath:
    """get_relative_path() returns raw relative string."""

    def test_returns_default(self, mock_config):
        rel = get_relative_path("journal_jarvis")
        assert rel == "journal/jarvis"

    def test_returns_config_override(self, mock_config):
        mock_config.set(paths={"journal_jarvis": "logs/ai"})
        rel = get_relative_path("journal_jarvis")
        assert rel == "logs/ai"

    def test_raises_for_absolute(self, mock_config):
        with pytest.raises(ValueError, match="absolute path"):
            get_relative_path("db_path")

    def test_raises_for_unknown(self, mock_config):
        with pytest.raises(PathNotConfiguredError):
            get_relative_path("nonexistent")


class TestIsSensitivePath:
    """is_sensitive_path() checks sensitivity classification."""

    def test_people_is_sensitive(self):
        assert is_sensitive_path("people") is True

    def test_documents_is_sensitive(self):
        assert is_sensitive_path("documents") is True

    def test_notes_is_not_sensitive(self):
        assert is_sensitive_path("notes") is False

    def test_journal_is_not_sensitive(self):
        assert is_sensitive_path("journal_jarvis") is False


class TestValidatePathsConfig:
    """validate_paths_config() catches config issues."""

    def test_clean_config_no_warnings(self, mock_config):
        warnings = validate_paths_config()
        assert warnings == []

    def test_unknown_path_key(self, mock_config):
        mock_config.set(paths={"typo_path": "foo"})
        warnings = validate_paths_config()
        assert any("Unknown path key" in w for w in warnings)

    def test_absolute_in_vault_relative(self, mock_config):
        mock_config.set(paths={"notes": "/absolute/notes"})
        warnings = validate_paths_config()
        assert any("should be relative" in w for w in warnings)

    def test_path_traversal(self, mock_config):
        mock_config.set(paths={"notes": "../outside"})
        warnings = validate_paths_config()
        assert any("traversal" in w for w in warnings)

    def test_unknown_memory_key(self, mock_config):
        mock_config.set(memory={"unknown_key": "value"})
        warnings = validate_paths_config()
        assert any("Unknown memory key" in w for w in warnings)

    def test_known_memory_keys_no_warning(self, mock_config):
        mock_config.set(memory={"secret_detection": True, "db_path": "~/.jarvis/db"})
        warnings = validate_paths_config()
        assert warnings == []


class TestListAllPaths:
    """list_all_paths() returns complete diagnostic output."""

    def test_returns_both_sections(self, mock_config):
        result = list_all_paths()
        assert "vault_relative" in result
        assert "absolute" in result

    def test_includes_all_vault_relative(self, mock_config):
        result = list_all_paths()
        for name in _VAULT_RELATIVE_DEFAULTS:
            assert name in result["vault_relative"]

    def test_includes_all_absolute(self, mock_config):
        result = list_all_paths()
        for name in _ABSOLUTE_DEFAULTS:
            assert name in result["absolute"]

    def test_shows_resolved_paths(self, mock_config):
        result = list_all_paths()
        entry = result["vault_relative"]["journal_jarvis"]
        assert entry["resolved"] is not None
        assert entry["default"] == "journal/jarvis"
        assert entry["configured"] is None

    def test_shows_configured_override(self, mock_config):
        mock_config.set(paths={"notes": "my-notes"})
        result = list_all_paths()
        entry = result["vault_relative"]["notes"]
        assert entry["configured"] == "my-notes"
        assert entry["default"] == "notes"

    def test_handles_missing_vault(self, no_config):
        result = list_all_paths()
        entry = result["vault_relative"]["journal_jarvis"]
        assert entry["resolved"] is None
        assert "error" in entry
        # Absolute paths should still work
        abs_entry = result["absolute"]["db_path"]
        assert abs_entry["resolved"] is not None
