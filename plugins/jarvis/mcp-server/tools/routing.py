"""Multi-remote routing engine for memory sync.

Evaluates an ordered list of routing rules against a memory dict
to determine which remote destinations should receive a copy.

Two-phase evaluation:
1. Deny pass — any matching deny rule vetoes destinations globally
2. Allow pass — first-match or all-match strategy selects destinations
3. Denied destinations are subtracted from the allow set

Rule format (from config):
    {
        "name": "work-only",
        "match": {"project": ["work-*"], "category": ["code", "decision"]},
        "action": "route-to",
        "destinations": ["work-remote"]
    }
"""

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger("jarvis-core")

# Valid categories (mirrors schema CHECK constraint)
VALID_CATEGORIES = frozenset({
    "observation", "pattern", "learning", "decision",
    "summary", "code", "relationship", "hint", "plan",
    "worklog", "memory",
})


@dataclass(frozen=True)
class MatchCondition:
    """Conditions for matching a memory against a routing rule."""
    project: list[str] = field(default_factory=list)
    category: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    importance_min: Optional[float] = None
    scope: Optional[str] = None
    path_prefix: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RoutingRule:
    """A single routing rule with match conditions and action."""
    name: str
    match: MatchCondition
    action: str  # "route-to" or "deny"
    destinations: list[str] = field(default_factory=list)


@dataclass
class RoutingDecision:
    """Result of routing evaluation."""
    destinations: list[str]
    matched_rules: list[str]
    denied_destinations: list[str] = field(default_factory=list)


def match_memory(memory: dict, condition: MatchCondition,
                 project_groups: dict[str, list[str]] | None = None) -> bool:
    """Check if a memory matches a rule's conditions.

    All specified conditions must match (AND logic).
    Empty condition lists are treated as "match any".

    Args:
        memory: Dict with keys: category, scope, project, importance_score,
                and optionally metadata with tags.
        condition: Match conditions to evaluate.
        project_groups: Mapping of @group names to project glob lists.
    """
    # Category match
    if condition.category:
        mem_category = memory.get("category", "observation")
        if mem_category not in condition.category:
            return False

    # Scope match
    if condition.scope:
        mem_scope = memory.get("scope", "global")
        if mem_scope != condition.scope:
            return False

    # Importance threshold
    if condition.importance_min is not None:
        mem_importance = memory.get("importance_score", 0.5)
        if mem_importance < condition.importance_min:
            return False

    # Project match (glob patterns with group expansion)
    if condition.project:
        mem_project = memory.get("project") or ""
        patterns = expand_project_groups(condition.project, project_groups or {})
        if not any(fnmatch.fnmatch(mem_project, p) for p in patterns):
            return False

    # Path prefix match (any prefix must match project_path)
    if condition.path_prefix:
        mem_path = ""
        metadata = memory.get("metadata", {})
        if isinstance(metadata, dict):
            raw_path = metadata.get("project_path", "")
            if isinstance(raw_path, str):
                mem_path = raw_path
        if not mem_path:
            return False
        mem_path = os.path.normpath(mem_path)
        normalized = [
            os.path.normpath(os.path.expanduser(p)) + "/"
            for p in condition.path_prefix
        ]
        # Append / to mem_path too so exact-directory matches work
        mem_check = mem_path if mem_path.endswith("/") else mem_path + "/"
        if not any(mem_check.startswith(pfx) for pfx in normalized):
            return False

    # Tag match (all specified tags must be present)
    if condition.tags:
        mem_tags_str = ""
        metadata = memory.get("metadata", {})
        if isinstance(metadata, dict):
            mem_tags_str = metadata.get("tags", "")
        mem_tags = set(t.strip() for t in mem_tags_str.split(",") if t.strip())
        if not all(t in mem_tags for t in condition.tags):
            return False

    return True


def expand_project_groups(patterns: list[str],
                          groups: dict[str, list[str]]) -> list[str]:
    """Expand @group references in project patterns.

    Example:
        patterns=["@work"], groups={"@work": ["work-*", "infra-*"]}
        → ["work-*", "infra-*"]
    """
    result = []
    for p in patterns:
        if p.startswith("@") and p in groups:
            result.extend(groups[p])
        else:
            result.append(p)
    return result


def evaluate_routing(
    memory: dict,
    rules: list[RoutingRule],
    strategy: str = "first-match",
    project_groups: dict[str, list[str]] | None = None,
) -> RoutingDecision:
    """Evaluate routing rules against a memory to determine destinations.

    Two-phase evaluation:
    1. Deny pass: collect all denied destinations from matching deny rules
    2. Allow pass: collect allowed destinations using the chosen strategy
    3. Subtract denied from allowed

    Args:
        memory: Memory dict with category, scope, project, importance_score, metadata.
        rules: Ordered list of routing rules.
        strategy: "first-match" (stop at first allow hit) or "all-match" (union all).
        project_groups: Group name → glob list mapping.

    Returns:
        RoutingDecision with final destinations and matched rule names.
    """
    groups = project_groups or {}

    # Phase 1: Deny pass — collect all denied destinations
    denied: set[str] = set()
    deny_rules: list[str] = []
    for rule in rules:
        if rule.action != "deny":
            continue
        if match_memory(memory, rule.match, groups):
            denied.update(rule.destinations)
            deny_rules.append(rule.name)

    # Phase 2: Allow pass — collect allowed destinations
    allowed: set[str] = set()
    allow_rules: list[str] = []
    for rule in rules:
        if rule.action != "route-to":
            continue
        if match_memory(memory, rule.match, groups):
            allowed.update(rule.destinations)
            allow_rules.append(rule.name)
            if strategy == "first-match":
                break

    # Phase 3: Subtract denied from allowed
    final = sorted(allowed - denied)

    return RoutingDecision(
        destinations=final,
        matched_rules=allow_rules + deny_rules,
        denied_destinations=sorted(denied),
    )


def parse_match_condition(raw: dict) -> MatchCondition:
    """Parse a raw match dict from config into a MatchCondition."""
    path_prefix = raw.get("path_prefix", [])
    if not isinstance(path_prefix, list):
        logger.warning("path_prefix must be a list, got %s — ignoring", type(path_prefix).__name__)
        path_prefix = []
    return MatchCondition(
        project=raw.get("project", []),
        category=raw.get("category", []),
        tags=raw.get("tags", []),
        importance_min=raw.get("importance_min"),
        scope=raw.get("scope"),
        path_prefix=path_prefix,
    )


def parse_routing_rule(raw: dict) -> RoutingRule:
    """Parse a raw rule dict from config into a RoutingRule."""
    return RoutingRule(
        name=raw.get("name", "unnamed"),
        match=parse_match_condition(raw.get("match", {})),
        action=raw.get("action", "route-to"),
        destinations=raw.get("destinations", []),
    )
