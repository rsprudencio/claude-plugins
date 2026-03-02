"""Tests for namespace ID generation and parsing."""

import pytest
from tools.namespaces import (
    vault_id,
    global_memory_id,
    project_memory_id,
    memory_namespace,
    observation_id,
    pattern_id,
    summary_id,
    code_id,
    learning_id,
    decision_id,
    worklog_id,
    parse_id,
    ParsedId,
    _slugify,
    ContentType,
    NAMESPACE_VAULT,
    NAMESPACE_MEMORY_GLOBAL,
    NAMESPACE_OBS,
    NAMESPACE_PATTERN,
    NAMESPACE_SUMMARY,
    NAMESPACE_CODE,
    NAMESPACE_LEARNING,
    NAMESPACE_DECISION,
    NAMESPACE_WORKLOG,
    ALL_TYPES,
    CONTENT_TYPES,
    TIER2_TYPES,
    VALID_CATEGORIES,
    SCHEMA_CORE,
    SCHEMA_VAULT,
    schema_for_id,
)


class TestSlugify:
    """Tests for the _slugify helper."""

    def test_lowercase(self):
        assert _slugify("Hello World") == "hello-world"

    def test_special_chars_removed(self):
        assert _slugify("hello_world (test)") == "helloworld-test"

    def test_multiple_hyphens_collapsed(self):
        assert _slugify("hello---world") == "hello-world"

    def test_strips_leading_trailing_hyphens(self):
        assert _slugify("-hello-") == "hello"

    def test_already_slugified(self):
        assert _slugify("jarvis-trajectory") == "jarvis-trajectory"

    def test_empty_string(self):
        assert _slugify("") == ""


class TestVaultId:
    """Tests for vault ID generation."""

    def test_simple_path(self):
        assert vault_id("notes/Containers.md") == "vault::notes/Containers.md"

    def test_nested_path(self):
        assert (
            vault_id("journal/jarvis/2026/01/entry.md")
            == "vault::journal/jarvis/2026/01/entry.md"
        )

    def test_with_chunk(self):
        assert (
            vault_id("notes/Containers.md", 2) == "vault::notes/Containers.md#chunk-2"
        )

    def test_chunk_zero(self):
        assert (
            vault_id("notes/Containers.md", 0) == "vault::notes/Containers.md#chunk-0"
        )

    def test_no_chunk(self):
        result = vault_id("notes/test.md")
        assert "#chunk-" not in result


class TestGlobalMemoryId:
    """Tests for global strategic memory ID generation."""

    def test_basic(self):
        assert (
            global_memory_id("jarvis-trajectory") == "memory::global::jarvis-trajectory"
        )

    def test_with_spaces(self):
        assert (
            global_memory_id("Jarvis Trajectory") == "memory::global::jarvis-trajectory"
        )

    def test_with_special_chars(self):
        assert global_memory_id("focus_areas (Q1)") == "memory::global::focusareas-q1"


class TestProjectMemoryId:
    """Tests for project-scoped memory ID generation."""

    def test_basic(self):
        assert (
            project_memory_id("jarvis-plugin", "dev-worklog")
            == "memory::jarvis-plugin::dev-worklog"
        )

    def test_with_spaces(self):
        assert (
            project_memory_id("Home Infra", "Network Map")
            == "memory::home-infra::network-map"
        )


class TestMemoryNamespace:
    """Tests for memory namespace prefix generation."""

    def test_global(self):
        assert memory_namespace() == "memory::global::"

    def test_project(self):
        assert memory_namespace("jarvis-plugin") == "memory::jarvis-plugin::"


class TestObservationId:
    """Tests for observation ID generation."""

    def test_explicit_timestamp(self):
        assert observation_id(1738857000000) == "obs::1738857000000"

    def test_auto_timestamp(self):
        oid = observation_id()
        assert oid.startswith("obs::")
        ts = int(oid.split("::")[1])
        assert ts > 0


class TestPatternId:
    """Tests for pattern ID generation."""

    def test_basic(self):
        assert pattern_id("nil-handling-oversight") == "pattern::nil-handling-oversight"

    def test_with_spaces(self):
        assert pattern_id("Nil Handling Oversight") == "pattern::nil-handling-oversight"

    def test_special_chars(self):
        assert (
            pattern_id("context_window (exhaustion)")
            == "pattern::contextwindow-exhaustion"
        )


class TestSummaryId:
    """Tests for session summary ID generation."""

    def test_explicit_session(self):
        assert summary_id("session-abc123") == "summary::session-abc123"

    def test_auto_session(self):
        sid = summary_id()
        assert sid.startswith("summary::session-")


class TestCodeId:
    """Tests for code chunk ID generation."""

    def test_with_symbol(self):
        assert (
            code_id("tools/memory.py", "index_vault")
            == "code::tools/memory.py::index_vault"
        )

    def test_module_default(self):
        assert code_id("tools/memory.py") == "code::tools/memory.py::__module__"


class TestParseId:
    """Tests for ID parsing across all namespaces."""

    def test_parse_vault(self):
        p = parse_id("vault::notes/Containers.md")
        assert p.namespace == "vault"
        assert p.full_prefix == "vault::"
        assert p.content_id == "notes/Containers.md"
        assert p.chunk is None

    def test_parse_vault_chunked(self):
        p = parse_id("vault::notes/Containers.md#chunk-2")
        assert p.namespace == "vault"
        assert p.content_id == "notes/Containers.md"
        assert p.chunk == 2

    def test_parse_global_memory(self):
        p = parse_id("memory::global::jarvis-trajectory")
        assert p.namespace == "memory"
        assert p.full_prefix == "memory::global::"
        assert p.content_id == "jarvis-trajectory"

    def test_parse_project_memory(self):
        p = parse_id("memory::jarvis-plugin::dev-worklog")
        assert p.namespace == "memory"
        assert p.full_prefix == "memory::jarvis-plugin::"
        assert p.content_id == "dev-worklog"

    def test_parse_observation(self):
        p = parse_id("obs::1738857000000")
        assert p.namespace == "obs"
        assert p.full_prefix == "obs::"
        assert p.content_id == "1738857000000"

    def test_parse_pattern(self):
        p = parse_id("pattern::nil-handling-oversight")
        assert p.namespace == "pattern"
        assert p.full_prefix == "pattern::"
        assert p.content_id == "nil-handling-oversight"

    def test_parse_summary(self):
        p = parse_id("summary::session-abc123")
        assert p.namespace == "summary"
        assert p.full_prefix == "summary::"
        assert p.content_id == "session-abc123"

    def test_parse_code(self):
        p = parse_id("code::tools/memory.py::index_vault")
        assert p.namespace == "code"
        assert p.full_prefix == "code::"
        assert p.content_id == "tools/memory.py::index_vault"

    def test_parse_bare_id(self):
        """Bare IDs (no prefix) should be treated as vault."""
        p = parse_id("notes/Containers.md")
        assert p.namespace == "vault"
        assert p.full_prefix == "vault::"
        assert p.content_id == "notes/Containers.md"


class TestRoundTrip:
    """Test that generate -> parse produces consistent results."""

    def test_vault_roundtrip(self):
        doc_id = vault_id("notes/test.md")
        parsed = parse_id(doc_id)
        assert parsed.namespace == "vault"
        assert parsed.content_id == "notes/test.md"
        assert parsed.chunk is None

    def test_vault_chunk_roundtrip(self):
        doc_id = vault_id("notes/test.md", 3)
        parsed = parse_id(doc_id)
        assert parsed.content_id == "notes/test.md"
        assert parsed.chunk == 3

    def test_global_memory_roundtrip(self):
        doc_id = global_memory_id("jarvis-trajectory")
        parsed = parse_id(doc_id)
        assert parsed.namespace == "memory"
        assert parsed.content_id == "jarvis-trajectory"

    def test_project_memory_roundtrip(self):
        doc_id = project_memory_id("jarvis-plugin", "dev-worklog")
        parsed = parse_id(doc_id)
        assert parsed.namespace == "memory"
        assert parsed.content_id == "dev-worklog"

    def test_observation_roundtrip(self):
        doc_id = observation_id(1738857000000)
        parsed = parse_id(doc_id)
        assert parsed.content_id == "1738857000000"

    def test_pattern_roundtrip(self):
        doc_id = pattern_id("nil-handling")
        parsed = parse_id(doc_id)
        assert parsed.content_id == "nil-handling"

    def test_summary_roundtrip(self):
        doc_id = summary_id("session-abc")
        parsed = parse_id(doc_id)
        assert parsed.content_id == "session-abc"

    def test_code_roundtrip(self):
        doc_id = code_id("tools/memory.py", "index_vault")
        parsed = parse_id(doc_id)
        assert parsed.content_id == "tools/memory.py::index_vault"

    def test_learning_roundtrip(self):
        doc_id = learning_id(1738857000000)
        parsed = parse_id(doc_id)
        assert parsed.namespace == "learning"
        assert parsed.content_id == "1738857000000"

    def test_decision_roundtrip(self):
        doc_id = decision_id("use-python")
        parsed = parse_id(doc_id)
        assert parsed.namespace == "decision"
        assert parsed.content_id == "use-python"


class TestLearningId:
    """Tests for learning ID generation."""

    def test_explicit_timestamp(self):
        assert learning_id(1738857000000) == "learning::1738857000000"

    def test_auto_timestamp(self):
        lid = learning_id()
        assert lid.startswith("learning::")
        ts = int(lid.split("::")[1])
        assert ts > 0


class TestDecisionId:
    """Tests for decision ID generation."""

    def test_basic(self):
        assert decision_id("use-python") == "decision::use-python"

    def test_with_spaces(self):
        assert decision_id("Use Python MCP") == "decision::use-python-mcp"

    def test_special_chars(self):
        assert (
            decision_id("python_over (typescript)") == "decision::pythonover-typescript"
        )


class TestConstants:
    """Test namespace constants and type values."""

    def test_namespace_prefixes(self):
        assert NAMESPACE_VAULT == "vault::"
        assert NAMESPACE_MEMORY_GLOBAL == "memory::global::"
        assert NAMESPACE_OBS == "obs::"
        assert NAMESPACE_PATTERN == "pattern::"
        assert NAMESPACE_SUMMARY == "summary::"
        assert NAMESPACE_CODE == "code::"

    def test_type_values(self):
        assert ContentType.VAULT == "vault"
        assert ContentType.MEMORY == "memory"
        assert ContentType.LEARNING == "learning"
        assert ContentType.DECISION == "decision"

    def test_all_types_count(self):
        assert len(ALL_TYPES) == 12
        assert "vault" in ALL_TYPES
        assert "memory" in ALL_TYPES
        assert "observation" in ALL_TYPES
        assert "pattern" in ALL_TYPES
        assert "summary" in ALL_TYPES
        assert "code" in ALL_TYPES
        assert "relationship" in ALL_TYPES
        assert "hint" in ALL_TYPES
        assert "plan" in ALL_TYPES
        assert "learning" in ALL_TYPES
        assert "decision" in ALL_TYPES

    def test_content_types_excludes_vault(self):
        """CONTENT_TYPES = all types except vault."""
        assert len(CONTENT_TYPES) == 11
        assert "vault" not in CONTENT_TYPES
        assert "memory" in CONTENT_TYPES
        assert "observation" in CONTENT_TYPES
        assert "learning" in CONTENT_TYPES
        assert "decision" in CONTENT_TYPES

    def test_tier2_types_is_alias_for_content_types(self):
        """TIER2_TYPES is a backward compatibility alias."""
        assert TIER2_TYPES is CONTENT_TYPES


class TestNewNamespaceConstants:
    """Tests for new namespace constants (rel, hint, plan)."""

    def test_new_namespace_values(self):
        from tools.namespaces import NAMESPACE_REL, NAMESPACE_HINT, NAMESPACE_PLAN

        assert NAMESPACE_REL == "rel::"
        assert NAMESPACE_HINT == "hint::"
        assert NAMESPACE_PLAN == "plan::"

    def test_new_type_values(self):
        assert ContentType.RELATIONSHIP == "relationship"
        assert ContentType.HINT == "hint"
        assert ContentType.PLAN == "plan"
        assert ContentType.LEARNING == "learning"
        assert ContentType.DECISION == "decision"

    def test_all_types_count(self):
        assert len(ALL_TYPES) == 12
        assert "relationship" in ALL_TYPES
        assert "hint" in ALL_TYPES
        assert "plan" in ALL_TYPES
        assert "learning" in ALL_TYPES
        assert "decision" in ALL_TYPES


class TestSchemaConstants:
    """Tests for schema routing constants and function."""

    def test_schema_constants(self):
        assert SCHEMA_CORE == "core"
        assert SCHEMA_VAULT == "vault"

    def test_schema_for_vault_id(self):
        assert schema_for_id("vault::notes/test.md") == SCHEMA_VAULT

    def test_schema_for_observation_id(self):
        assert schema_for_id("obs::12345") == SCHEMA_CORE

    def test_schema_for_pattern_id(self):
        assert schema_for_id("pattern::test-pattern") == SCHEMA_CORE

    def test_schema_for_memory_id(self):
        assert schema_for_id("memory::global::test") == SCHEMA_CORE

    def test_schema_for_content_ids(self):
        assert schema_for_id("rel::a::b") == SCHEMA_CORE
        assert schema_for_id("hint::topic::0") == SCHEMA_CORE
        assert schema_for_id("plan::test-plan") == SCHEMA_CORE
        assert schema_for_id("learning::1738857000000") == SCHEMA_CORE
        assert schema_for_id("decision::use-python") == SCHEMA_CORE
        assert schema_for_id("worklog::1738857000000") == SCHEMA_CORE

    def test_bare_path_defaults_to_core(self):
        """Bare paths (no prefix) default to SCHEMA_CORE."""
        assert schema_for_id("notes/test.md") == SCHEMA_CORE


class TestParsedIdSchema:
    """Tests for schema field in ParsedId."""

    def test_vault_id_has_schema_vault(self):
        parsed = parse_id("vault::notes/test.md")
        assert parsed.schema == SCHEMA_VAULT

    def test_observation_id_has_schema_core(self):
        parsed = parse_id("obs::12345")
        assert parsed.schema == SCHEMA_CORE

    def test_memory_id_has_schema_core(self):
        parsed = parse_id("memory::global::test")
        assert parsed.schema == SCHEMA_CORE

    def test_bare_path_has_schema_vault(self):
        parsed = parse_id("notes/test.md")
        assert parsed.schema == SCHEMA_VAULT


class TestValidCategories:
    """Tests for VALID_CATEGORIES tuple."""

    def test_valid_categories_contents(self):
        assert "observation" in VALID_CATEGORIES
        assert "pattern" in VALID_CATEGORIES
        assert "learning" in VALID_CATEGORIES
        assert "decision" in VALID_CATEGORIES
        assert "summary" in VALID_CATEGORIES
        assert "code" in VALID_CATEGORIES
        assert "relationship" in VALID_CATEGORIES
        assert "hint" in VALID_CATEGORIES
        assert "plan" in VALID_CATEGORIES
        assert "worklog" in VALID_CATEGORIES
        assert "memory" in VALID_CATEGORIES
        assert len(VALID_CATEGORIES) == 11

    def test_vault_not_in_valid_categories(self):
        """Vault is not a valid category for core.memories."""
        assert "vault" not in VALID_CATEGORIES


class TestNewIdGenerators:
    """Tests for new ID generators (relationship, hint, plan)."""

    def test_relationship_id(self):
        from tools.namespaces import relationship_id

        # Entities sorted alphabetically
        assert relationship_id("alice", "bob") == "rel::alice::bob"
        assert relationship_id("bob", "alice") == "rel::alice::bob"  # Same result

    def test_relationship_id_slugify(self):
        from tools.namespaces import relationship_id

        result = relationship_id("Alice Smith", "Bob Jones")
        assert result.startswith("rel::")
        assert "alice" in result.lower()
        assert "bob" in result.lower()

    def test_hint_id(self):
        from tools.namespaces import hint_id

        assert hint_id("git-workflow", 0) == "hint::git-workflow::0"
        assert hint_id("git-workflow", 5) == "hint::git-workflow::5"

    def test_hint_id_default_seq(self):
        from tools.namespaces import hint_id

        assert hint_id("test") == "hint::test::0"

    def test_plan_id(self):
        from tools.namespaces import plan_id

        assert plan_id("phase-1-implementation") == "plan::phase-1-implementation"

    def test_plan_id_slugify(self):
        from tools.namespaces import plan_id

        assert plan_id("Phase 1 Implementation") == "plan::phase-1-implementation"


class TestParseIdNewNamespaces:
    """Tests for parsing new namespace IDs."""

    def test_parse_relationship_id(self):
        parsed = parse_id("rel::alice::bob")
        assert parsed.namespace == "rel"
        assert parsed.full_prefix == "rel::"
        assert parsed.content_id == "alice::bob"

    def test_parse_hint_id(self):
        parsed = parse_id("hint::git-workflow::0")
        assert parsed.namespace == "hint"
        assert parsed.full_prefix == "hint::"
        assert parsed.content_id == "git-workflow::0"

    def test_parse_plan_id(self):
        parsed = parse_id("plan::phase-1")
        assert parsed.namespace == "plan"
        assert parsed.full_prefix == "plan::"
        assert parsed.content_id == "phase-1"

    def test_parse_learning_id(self):
        parsed = parse_id("learning::1738857000000")
        assert parsed.namespace == "learning"
        assert parsed.full_prefix == "learning::"
        assert parsed.content_id == "1738857000000"

    def test_parse_decision_id(self):
        parsed = parse_id("decision::use-python")
        assert parsed.namespace == "decision"
        assert parsed.full_prefix == "decision::"
        assert parsed.content_id == "use-python"

    def test_parse_worklog_id(self):
        parsed = parse_id("worklog::1738857000000")
        assert parsed.namespace == "worklog"
        assert parsed.full_prefix == "worklog::"
        assert parsed.content_id == "1738857000000"


class TestWorklogId:
    """Tests for worklog ID generation."""

    def test_explicit_timestamp(self):
        assert worklog_id(1738857000000) == "worklog::1738857000000"

    def test_auto_timestamp(self):
        wid = worklog_id()
        assert wid.startswith("worklog::")
        ts = int(wid.split("::")[1])
        assert ts > 0

    def test_namespace_constant(self):
        assert NAMESPACE_WORKLOG == "worklog::"

    def test_content_type_value(self):
        assert ContentType.WORKLOG == "worklog"

    def test_worklog_in_all_types(self):
        assert "worklog" in ALL_TYPES

    def test_worklog_in_content_types(self):
        assert "worklog" in CONTENT_TYPES

    def test_schema_for_worklog(self):
        assert schema_for_id("worklog::1738857000000") == SCHEMA_CORE
