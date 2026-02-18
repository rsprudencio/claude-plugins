"""Tests for tools.routing_utils — shared routing validation helpers."""

from types import SimpleNamespace

from tools.routing_utils import validate_exactly_one, parse_memory_scope


# ── validate_exactly_one ──────────────────────────────────────────────


class TestValidateExactlyOne:
    """Cover zero-set, one-set, multi-set, and falsy-value scenarios."""

    def test_zero_options_returns_error(self):
        result = validate_exactly_one(
            [None, None, None],
            "missing",
            "multiple",
        )
        assert result == {"success": False, "error": "missing"}

    def test_one_option_passes(self):
        assert validate_exactly_one(["a", None, None], "m", "x") is None

    def test_multiple_options_returns_error(self):
        result = validate_exactly_one(
            ["a", "b", None],
            "missing",
            "multiple",
        )
        assert result == {"success": False, "error": "multiple"}

    def test_all_options_set_returns_error(self):
        result = validate_exactly_one(
            ["a", "b", "c"],
            "missing",
            "multiple",
        )
        assert result == {"success": False, "error": "multiple"}

    def test_falsy_values_treated_as_absent(self):
        """Empty strings and 0 are falsy — should count as 'not set'."""
        assert validate_exactly_one(["", 0, None], "m", "x") == {
            "success": False,
            "error": "m",
        }

    def test_falsy_plus_one_truthy_passes(self):
        assert validate_exactly_one(["", "real", None], "m", "x") is None

    def test_two_element_list(self):
        assert validate_exactly_one([None, "b"], "m", "x") is None
        assert validate_exactly_one(["a", "b"], "m", "x") == {
            "success": False,
            "error": "x",
        }


# ── parse_memory_scope ────────────────────────────────────────────────


class TestParseMemoryScope:
    """Cover global and project-scoped parsed IDs."""

    def test_global_scope(self):
        parsed = SimpleNamespace(
            full_prefix="memory::global::",
            content_id="my-memory",
            project=None,
        )
        scope, project = parse_memory_scope(parsed)
        assert scope == "global"
        assert project is None

    def test_project_scope(self):
        parsed = SimpleNamespace(
            full_prefix="memory::project::jarvis::",
            content_id="some-decision",
            project="jarvis",
        )
        scope, project = parse_memory_scope(parsed)
        assert scope == "project"
        assert project == "jarvis"

    def test_project_scope_none_project_returns_none(self):
        """If parsed.project is None but prefix says project, still return None."""
        parsed = SimpleNamespace(
            full_prefix="memory::project::foo::",
            content_id="bar",
            project=None,
        )
        scope, project = parse_memory_scope(parsed)
        assert scope == "project"
        assert project is None
