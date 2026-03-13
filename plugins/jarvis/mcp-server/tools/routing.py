"""Multi-remote routing engine for memory sync.

Thin re-export from jarvis_common.routing — all logic lives in the
shared library so it can be used by both the MCP server and the
Memory Explorer without sys.path hacks.
"""

from jarvis_common.routing import (  # noqa: F401
    VALID_CATEGORIES,
    MatchCondition,
    RoutingDecision,
    RoutingRule,
    evaluate_routing,
    expand_project_groups,
    match_memory,
    parse_match_condition,
    parse_routing_rule,
)
