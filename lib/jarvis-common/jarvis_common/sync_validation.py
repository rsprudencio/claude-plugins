"""Sync configuration validation, env var resolution, and secret redaction.

Provides fail-fast validation at startup and safe logging helpers
to prevent credential leakage in log output.
"""

from __future__ import annotations

import logging
import os
import re

from .routing import RoutingRule, parse_routing_rule

logger = logging.getLogger(__name__)

# Pattern for $ENV_VAR references in DSN strings
_ENV_VAR_PATTERN = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")

# Pattern for credentials in DSN: scheme://user:password@host
_DSN_CREDENTIALS_PATTERN = re.compile(r"(://[^:]+:)([^@]+)(@)")

# Valid PostgreSQL identifier: starts with letter/underscore, alphanumeric+underscore, max 63 chars
_PG_IDENTIFIER_PATTERN = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")

# Schema names that must never be used as remote targets
_RESERVED_SCHEMAS = frozenset({
    "pg_catalog", "information_schema", "public", "local",
})


def resolve_env_vars(url: str) -> str:
    """Resolve $ENV_VAR references in a connection URL.

    Raises:
        ValueError: If an env var is referenced but not set.
    """
    def _replace(match):
        var_name = match.group(1)
        value = os.environ.get(var_name)
        if value is None:
            raise ValueError(
                f"Environment variable ${var_name} referenced in sync config "
                f"but not set"
            )
        return value

    return _ENV_VAR_PATTERN.sub(_replace, url)


def redact_dsn(url: str) -> str:
    """Redact credentials from a PostgreSQL DSN for safe logging.

    Example:
        postgresql://user:secret@host:5432/db → postgresql://user:***@host:5432/db
    """
    return _DSN_CREDENTIALS_PATTERN.sub(r"\1***\3", url)


def validate_sync_config(config: dict) -> list[str]:
    """Validate sync configuration, returning a list of error messages.

    Checks:
    - strategy is valid
    - default_action is valid
    - all rule destinations reference configured remotes
    - all rule actions are valid
    - env vars in remote URLs can be resolved
    - project group references in rules are defined

    Returns:
        List of error strings. Empty list means valid.
    """
    errors = []

    # Validate strategy
    valid_strategies = ("first-match", "all-match")
    strategy = config.get("strategy", "first-match")
    if strategy not in valid_strategies:
        errors.append(
            f"Invalid strategy '{strategy}'. "
            f"Must be one of: {', '.join(valid_strategies)}"
        )

    # Validate default_action
    valid_actions = ("local-only",)
    default_action = config.get("default_action", "local-only")
    if default_action not in valid_actions:
        errors.append(
            f"Invalid default_action '{default_action}'. "
            f"Must be one of: {', '.join(valid_actions)}"
        )

    # Validate remotes
    remotes = config.get("remotes", {})
    for remote_name, remote_cfg in remotes.items():
        url = remote_cfg.get("url", "")
        has_host = bool(remote_cfg.get("host"))
        if not url and not has_host:
            errors.append(f"Remote '{remote_name}' has no URL or host configured")
            continue
        # Try resolving env vars in URL (fail-fast)
        if url:
            try:
                resolve_env_vars(url)
            except ValueError as e:
                errors.append(str(e))

        # Validate schema (explicit field or remote name as fallback)
        schema = remote_cfg.get("schema", remote_name)
        if not _PG_IDENTIFIER_PATTERN.match(schema):
            errors.append(
                f"Remote '{remote_name}': schema '{schema}' is not a valid "
                f"PostgreSQL identifier (must match [a-z_][a-z0-9_]{{0,62}})"
            )
        elif schema in _RESERVED_SCHEMAS:
            errors.append(
                f"Remote '{remote_name}': schema '{schema}' is reserved "
                f"and cannot be used as a remote target"
            )

    # Validate rules
    remote_names = set(remotes.keys())
    project_groups = config.get("project_groups", {})
    rules = config.get("rules", [])

    for i, raw_rule in enumerate(rules):
        rule_name = raw_rule.get("name", f"rule[{i}]")
        action = raw_rule.get("action", "route-to")

        if action not in ("route-to", "deny"):
            errors.append(
                f"Rule '{rule_name}': invalid action '{action}'. "
                f"Must be 'route-to' or 'deny'"
            )

        destinations = raw_rule.get("destinations", [])
        if action == "route-to" and not destinations:
            errors.append(
                f"Rule '{rule_name}': route-to action requires at least one destination"
            )

        for dest in destinations:
            if dest not in remote_names:
                errors.append(
                    f"Rule '{rule_name}': destination '{dest}' "
                    f"not found in configured remotes"
                )

        # Validate project group references
        match = raw_rule.get("match", {})
        for project_pattern in match.get("project", []):
            if project_pattern.startswith("@") and project_pattern not in project_groups:
                errors.append(
                    f"Rule '{rule_name}': project group '{project_pattern}' "
                    f"not defined in project_groups"
                )

    return errors


def load_routing_rules(config: dict) -> list[RoutingRule]:
    """Parse config rules into RoutingRule dataclasses.

    Args:
        config: Full sync config dict (from get_sync_config()).

    Returns:
        Ordered list of RoutingRule objects.
    """
    raw_rules = config.get("rules", [])
    return [parse_routing_rule(r) for r in raw_rules]
