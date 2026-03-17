"""Tests for the schema registry module."""

import pytest

from tools.schema_registry import (
    SchemaKind,
    SchemaEntry,
    is_valid_schema_name,
    get_searchable_schemas,
    get_registry,
    rebuild_registry,
    register_obsidian,
    register_remote,
    unregister,
    _registry,
)
from tools.namespaces import SCHEMA_LOCAL, SCHEMA_OBSIDIAN


@pytest.fixture(autouse=True)
def clean_registry(monkeypatch):
    """Reset registry and stub remote discovery before each test."""
    import tools.schema_registry as mod
    mod._registry = []
    # Stub auto-discovery so unit tests don't hit live PG
    monkeypatch.setattr(mod, "_discover_remote_schemas", lambda: [])
    monkeypatch.setattr(mod, "_get_enabled_remote_names", lambda: set())
    monkeypatch.setattr(mod, "_get_local_embedding_model", lambda: None)
    yield
    mod._registry = []


class TestSchemaKind:
    """Tests for SchemaKind enum."""

    def test_values(self):
        assert SchemaKind.LOCAL == "local"
        assert SchemaKind.OBSIDIAN == "obsidian"
        assert SchemaKind.REMOTE == "remote"

    def test_str_enum(self):
        assert str(SchemaKind.LOCAL) == "SchemaKind.LOCAL"
        assert SchemaKind.LOCAL.value == "local"


class TestSchemaEntry:
    """Tests for SchemaEntry dataclass."""

    def test_defaults(self):
        entry = SchemaEntry(name="test", kind=SchemaKind.LOCAL, table="memories")
        assert entry.searchable is True
        assert entry.writable is True
        assert entry.remote_name is None
        assert entry.metadata == {}

    def test_remote_entry(self):
        entry = SchemaEntry(
            name="remote_work",
            kind=SchemaKind.REMOTE,
            table="memories",
            searchable=True,
            writable=False,
            remote_name="work-server",
            metadata={"url": "postgres://..."},
        )
        assert entry.remote_name == "work-server"
        assert entry.writable is False


class TestIsValidSchemaName:
    """Tests for schema name validation."""

    def test_valid_names(self):
        assert is_valid_schema_name("local") is True
        assert is_valid_schema_name("obsidian") is True
        assert is_valid_schema_name("remote_work") is True
        assert is_valid_schema_name("r123") is True

    def test_invalid_names(self):
        assert is_valid_schema_name("") is False
        assert is_valid_schema_name("123abc") is False  # starts with digit
        assert is_valid_schema_name("my-schema") is False  # hyphens
        assert is_valid_schema_name("My.Schema") is False  # dots, uppercase
        assert is_valid_schema_name("a" * 64) is False  # too long

    def test_pg_reserved(self):
        assert is_valid_schema_name("pg_catalog") is False
        assert is_valid_schema_name("pg_temp") is False


class TestRebuildRegistry:
    """Tests for rebuild_registry()."""

    def test_builds_two_entries(self):
        result = rebuild_registry()
        assert len(result) == 2

    def test_local_entry(self):
        rebuild_registry()
        local = [e for e in get_registry() if e.kind == SchemaKind.LOCAL]
        assert len(local) == 1
        assert local[0].name == SCHEMA_LOCAL
        assert local[0].table == "memories"
        assert local[0].searchable is True

    def test_obsidian_entry(self):
        rebuild_registry()
        obsidian = [e for e in get_registry() if e.kind == SchemaKind.OBSIDIAN]
        assert len(obsidian) == 1
        assert obsidian[0].name == SCHEMA_OBSIDIAN
        assert obsidian[0].table == "documents"

    def test_rebuild_replaces_previous(self):
        rebuild_registry()
        register_remote("remote_1", "r1")
        assert len(get_registry()) == 3
        rebuild_registry()  # Should reset (discovery stubbed to empty)
        assert len(get_registry()) == 2

    def test_rebuild_auto_discovers_remotes(self, monkeypatch):
        """rebuild_registry() includes auto-discovered remote schemas."""
        import tools.schema_registry as mod
        monkeypatch.setattr(mod, "_discover_remote_schemas", lambda: ["remote_personio"])
        monkeypatch.setattr(mod, "_get_enabled_remote_names", lambda: set())  # empty = register all
        monkeypatch.setattr(mod, "_get_local_embedding_model", lambda: "test-model")

        result = rebuild_registry()
        assert len(result) == 3  # local + obsidian + remote_personio
        remote = [e for e in result if e.kind == SchemaKind.REMOTE]
        assert len(remote) == 1
        assert remote[0].name == "remote_personio"
        assert remote[0].remote_name == "personio"
        assert remote[0].metadata == {"embedding_model": "test-model"}
        assert remote[0].searchable is True
        assert remote[0].writable is False

    def test_rebuild_skips_disabled_remotes(self, monkeypatch):
        """rebuild_registry() prunes remotes not in enabled config."""
        import tools.schema_registry as mod
        monkeypatch.setattr(mod, "_discover_remote_schemas", lambda: ["remote_a", "remote_b"])
        monkeypatch.setattr(mod, "_get_enabled_remote_names", lambda: {"a"})  # only 'a' enabled
        monkeypatch.setattr(mod, "_get_local_embedding_model", lambda: None)

        result = rebuild_registry()
        assert len(result) == 3  # local + obsidian + remote_a (remote_b pruned)
        remote_names = [e.name for e in result if e.kind == SchemaKind.REMOTE]
        assert remote_names == ["remote_a"]


class TestGetSearchableSchemas:
    """Tests for get_searchable_schemas()."""

    def test_all_searchable(self):
        rebuild_registry()
        result = get_searchable_schemas()
        assert len(result) == 2

    def test_filter_by_kind(self):
        rebuild_registry()
        local = get_searchable_schemas(kind=SchemaKind.LOCAL)
        assert len(local) == 1
        assert local[0].name == SCHEMA_LOCAL

    def test_non_searchable_excluded(self):
        rebuild_registry()
        register_remote("remote_hidden", "r1", searchable=False)
        result = get_searchable_schemas()
        assert len(result) == 2  # remote_hidden excluded

    def test_searchable_remote_included(self):
        rebuild_registry()
        register_remote("remote_visible", "r2", searchable=True)
        result = get_searchable_schemas()
        assert len(result) == 3


class TestRegisterObsidian:
    """Tests for register_obsidian()."""

    def test_registers_when_empty(self):
        entry = register_obsidian()
        assert entry.kind == SchemaKind.OBSIDIAN
        assert entry.name == SCHEMA_OBSIDIAN

    def test_idempotent(self):
        rebuild_registry()
        existing = [e for e in get_registry() if e.kind == SchemaKind.OBSIDIAN][0]
        result = register_obsidian()
        assert result is existing  # Same object
        assert len([e for e in get_registry() if e.kind == SchemaKind.OBSIDIAN]) == 1


class TestRegisterRemote:
    """Tests for register_remote()."""

    def test_register_remote(self):
        rebuild_registry()
        entry = register_remote("remote_work", "work-server")
        assert entry.kind == SchemaKind.REMOTE
        assert entry.remote_name == "work-server"
        assert entry.writable is False  # default

    def test_register_writable_remote(self):
        rebuild_registry()
        entry = register_remote("remote_shared", "shared", writable=True)
        assert entry.writable is True

    def test_duplicate_returns_existing(self):
        """register_remote is idempotent — duplicate name returns existing entry (D11)."""
        rebuild_registry()
        e1 = register_remote("remote_a", "a")
        e2 = register_remote("remote_a", "b")  # different remote_name, same schema name
        assert e1.name == e2.name
        assert e2.remote_name == "a"  # original entry preserved, not overwritten

    def test_invalid_name_raises(self):
        with pytest.raises(ValueError, match="Invalid schema name"):
            register_remote("my-invalid-name", "r1")

    def test_metadata_stored(self):
        rebuild_registry()
        entry = register_remote("remote_meta", "m1", metadata={"region": "us-east-1"})
        assert entry.metadata["region"] == "us-east-1"


class TestUnregister:
    """Tests for unregister()."""

    def test_unregister_existing(self):
        rebuild_registry()
        register_remote("remote_temp", "t1")
        assert len(get_registry()) == 3
        result = unregister("remote_temp")
        assert result is True
        assert len(get_registry()) == 2

    def test_unregister_nonexistent(self):
        rebuild_registry()
        result = unregister("nonexistent")
        assert result is False
        assert len(get_registry()) == 2
