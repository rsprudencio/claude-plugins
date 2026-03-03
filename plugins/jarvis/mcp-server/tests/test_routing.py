"""Tests for the multi-remote routing engine (tools/routing.py)."""

import pytest

from tools.routing import (
    MatchCondition,
    RoutingRule,
    RoutingDecision,
    evaluate_routing,
    expand_project_groups,
    match_memory,
    parse_match_condition,
    parse_routing_rule,
)


# ── Fixtures ──────────────────────────────────────────────────────────


def _memory(
    category="observation",
    scope="global",
    project=None,
    importance_score=0.5,
    tags="",
):
    """Create a minimal memory dict for testing."""
    return {
        "category": category,
        "scope": scope,
        "project": project,
        "importance_score": importance_score,
        "metadata": {"tags": tags} if tags else {},
    }


def _rule(name, match=None, action="route-to", destinations=None):
    """Create a RoutingRule for testing."""
    return RoutingRule(
        name=name,
        match=match or MatchCondition(),
        action=action,
        destinations=destinations or [],
    )


# ── MatchCondition tests ─────────────────────────────────────────────


class TestMatchMemory:
    def test_empty_condition_matches_everything(self):
        assert match_memory(_memory(), MatchCondition())

    def test_category_match(self):
        cond = MatchCondition(category=["decision", "learning"])
        assert match_memory(_memory(category="decision"), cond)
        assert match_memory(_memory(category="learning"), cond)
        assert not match_memory(_memory(category="observation"), cond)

    def test_scope_match(self):
        cond = MatchCondition(scope="project")
        assert match_memory(_memory(scope="project", project="test"), cond)
        assert not match_memory(_memory(scope="global"), cond)

    def test_importance_threshold(self):
        cond = MatchCondition(importance_min=0.7)
        assert match_memory(_memory(importance_score=0.8), cond)
        assert match_memory(_memory(importance_score=0.7), cond)
        assert not match_memory(_memory(importance_score=0.6), cond)

    def test_project_glob_exact(self):
        cond = MatchCondition(project=["my-project"])
        assert match_memory(_memory(project="my-project"), cond)
        assert not match_memory(_memory(project="other"), cond)

    def test_project_glob_wildcard(self):
        cond = MatchCondition(project=["work-*"])
        assert match_memory(_memory(project="work-frontend"), cond)
        assert match_memory(_memory(project="work-backend"), cond)
        assert not match_memory(_memory(project="personal"), cond)

    def test_project_glob_no_project(self):
        """Memory without project should not match project conditions."""
        cond = MatchCondition(project=["work-*"])
        assert not match_memory(_memory(project=None), cond)

    def test_project_group_expansion(self):
        cond = MatchCondition(project=["@work"])
        groups = {"@work": ["work-*", "infra-*"]}
        assert match_memory(_memory(project="work-api"), cond, groups)
        assert match_memory(_memory(project="infra-k8s"), cond, groups)
        assert not match_memory(_memory(project="personal"), cond, groups)

    def test_tags_match_all_required(self):
        cond = MatchCondition(tags=["urgent", "reviewed"])
        assert match_memory(_memory(tags="urgent,reviewed,done"), cond)
        assert not match_memory(_memory(tags="urgent"), cond)

    def test_tags_empty_metadata(self):
        cond = MatchCondition(tags=["urgent"])
        assert not match_memory(_memory(tags=""), cond)
        assert not match_memory({"category": "observation", "metadata": {}}, cond)

    def test_combined_conditions_and_logic(self):
        """All conditions must match (AND logic)."""
        cond = MatchCondition(
            category=["decision"],
            scope="project",
            importance_min=0.7,
            project=["work-*"],
        )
        # All match
        assert match_memory(
            _memory(category="decision", scope="project",
                    project="work-api", importance_score=0.8),
            cond,
        )
        # Category mismatch
        assert not match_memory(
            _memory(category="observation", scope="project",
                    project="work-api", importance_score=0.8),
            cond,
        )
        # Importance too low
        assert not match_memory(
            _memory(category="decision", scope="project",
                    project="work-api", importance_score=0.5),
            cond,
        )


# ── expand_project_groups ─────────────────────────────────────────────


class TestExpandProjectGroups:
    def test_no_groups(self):
        assert expand_project_groups(["foo", "bar-*"], {}) == ["foo", "bar-*"]

    def test_group_expansion(self):
        groups = {"@work": ["work-*", "infra-*"]}
        result = expand_project_groups(["@work", "other"], groups)
        assert result == ["work-*", "infra-*", "other"]

    def test_undefined_group_kept_as_literal(self):
        result = expand_project_groups(["@unknown"], {})
        assert result == ["@unknown"]

    def test_empty_patterns(self):
        assert expand_project_groups([], {"@work": ["a"]}) == []


# ── evaluate_routing ──────────────────────────────────────────────────


class TestEvaluateRouting:
    def test_no_rules_empty_destinations(self):
        decision = evaluate_routing(_memory(), [])
        assert decision.destinations == []
        assert decision.matched_rules == []

    def test_single_allow_rule(self):
        rules = [
            _rule("backup", destinations=["remote-a"]),
        ]
        decision = evaluate_routing(_memory(), rules)
        assert decision.destinations == ["remote-a"]
        assert "backup" in decision.matched_rules

    def test_first_match_stops_at_first(self):
        rules = [
            _rule("first", match=MatchCondition(category=["observation"]),
                  destinations=["remote-a"]),
            _rule("second", destinations=["remote-b"]),
        ]
        decision = evaluate_routing(
            _memory(category="observation"), rules, strategy="first-match"
        )
        assert decision.destinations == ["remote-a"]
        assert "second" not in decision.matched_rules

    def test_all_match_unions_destinations(self):
        rules = [
            _rule("first", match=MatchCondition(category=["observation"]),
                  destinations=["remote-a"]),
            _rule("second", destinations=["remote-b"]),
        ]
        decision = evaluate_routing(
            _memory(category="observation"), rules, strategy="all-match"
        )
        assert sorted(decision.destinations) == ["remote-a", "remote-b"]

    def test_deny_vetoes_destination(self):
        rules = [
            _rule("allow-all", destinations=["remote-a", "remote-b"]),
            _rule("deny-a", match=MatchCondition(category=["observation"]),
                  action="deny", destinations=["remote-a"]),
        ]
        decision = evaluate_routing(
            _memory(category="observation"), rules, strategy="all-match"
        )
        assert decision.destinations == ["remote-b"]
        assert "remote-a" in decision.denied_destinations

    def test_deny_evaluated_first(self):
        """Deny rules are evaluated in a separate pass before allow rules."""
        rules = [
            _rule("deny-sensitive", match=MatchCondition(category=["relationship"]),
                  action="deny", destinations=["remote-a"]),
            _rule("allow-all", destinations=["remote-a", "remote-b"]),
        ]
        decision = evaluate_routing(
            _memory(category="relationship"), rules, strategy="all-match"
        )
        assert decision.destinations == ["remote-b"]

    def test_deny_all_destinations_results_in_empty(self):
        rules = [
            _rule("deny-all", action="deny", destinations=["remote-a"]),
            _rule("allow-all", destinations=["remote-a"]),
        ]
        decision = evaluate_routing(_memory(), rules)
        assert decision.destinations == []

    def test_no_matching_allow_rules(self):
        rules = [
            _rule("work-only", match=MatchCondition(project=["work-*"]),
                  destinations=["remote-a"]),
        ]
        decision = evaluate_routing(_memory(project="personal"), rules)
        assert decision.destinations == []

    def test_project_groups_in_routing(self):
        rules = [
            _rule("work-route", match=MatchCondition(project=["@work"]),
                  destinations=["work-remote"]),
        ]
        groups = {"@work": ["work-*", "infra-*"]}
        decision = evaluate_routing(
            _memory(project="work-api"), rules,
            project_groups=groups,
        )
        assert decision.destinations == ["work-remote"]

    def test_destinations_sorted(self):
        rules = [
            _rule("multi", destinations=["z-remote", "a-remote", "m-remote"]),
        ]
        decision = evaluate_routing(_memory(), rules)
        assert decision.destinations == ["a-remote", "m-remote", "z-remote"]

    def test_deny_without_allow_is_empty(self):
        rules = [
            _rule("deny-only", action="deny", destinations=["remote-a"]),
        ]
        decision = evaluate_routing(_memory(), rules)
        assert decision.destinations == []


# ── Parsing ───────────────────────────────────────────────────────────


class TestParsing:
    def test_parse_match_condition(self):
        raw = {
            "project": ["work-*"],
            "category": ["decision"],
            "importance_min": 0.7,
            "scope": "project",
        }
        cond = parse_match_condition(raw)
        assert cond.project == ["work-*"]
        assert cond.category == ["decision"]
        assert cond.importance_min == 0.7
        assert cond.scope == "project"

    def test_parse_match_condition_defaults(self):
        cond = parse_match_condition({})
        assert cond.project == []
        assert cond.category == []
        assert cond.tags == []
        assert cond.importance_min is None
        assert cond.scope is None

    def test_parse_routing_rule(self):
        raw = {
            "name": "test-rule",
            "match": {"category": ["code"]},
            "action": "deny",
            "destinations": ["work"],
        }
        rule = parse_routing_rule(raw)
        assert rule.name == "test-rule"
        assert rule.action == "deny"
        assert rule.destinations == ["work"]
        assert rule.match.category == ["code"]

    def test_parse_routing_rule_defaults(self):
        rule = parse_routing_rule({})
        assert rule.name == "unnamed"
        assert rule.action == "route-to"
        assert rule.destinations == []
