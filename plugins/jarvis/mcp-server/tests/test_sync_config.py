"""Tests for sync configuration validation and helpers (tools/sync_config.py)."""

import os
from unittest.mock import patch

import pytest

from tools.sync_config import (
    load_routing_rules,
    redact_dsn,
    resolve_env_vars,
    validate_sync_config,
)


class TestResolveEnvVars:
    def test_no_env_vars(self):
        assert resolve_env_vars("postgresql://host:5432/db") == "postgresql://host:5432/db"

    def test_single_env_var(self, monkeypatch):
        monkeypatch.setenv("PG_HOST", "my-server")
        assert resolve_env_vars("postgresql://$PG_HOST:5432/db") == "postgresql://my-server:5432/db"

    def test_multiple_env_vars(self, monkeypatch):
        monkeypatch.setenv("PG_HOST", "my-server")
        monkeypatch.setenv("PG_PORT", "5433")
        result = resolve_env_vars("postgresql://$PG_HOST:$PG_PORT/db")
        assert result == "postgresql://my-server:5433/db"

    def test_missing_env_var_raises(self):
        with pytest.raises(ValueError, match="NONEXISTENT_VAR"):
            resolve_env_vars("postgresql://$NONEXISTENT_VAR:5432/db")

    def test_env_var_with_underscores(self, monkeypatch):
        monkeypatch.setenv("MY_PG_URL_123", "host")
        assert resolve_env_vars("$MY_PG_URL_123") == "host"


class TestRedactDsn:
    def test_redacts_password(self):
        assert redact_dsn("postgresql://user:secret@host:5432/db") == \
               "postgresql://user:***@host:5432/db"

    def test_no_credentials_unchanged(self):
        assert redact_dsn("postgresql://host:5432/db") == "postgresql://host:5432/db"

    def test_complex_password(self):
        result = redact_dsn("postgresql://admin:p@ss/w0rd!@host:5432/db")
        # The regex matches up to the first @
        assert "p@ss" not in result or "***" in result

    def test_empty_string(self):
        assert redact_dsn("") == ""


class TestValidateSyncConfig:
    def test_valid_minimal_config(self):
        config = {
            "enabled": True,
            "strategy": "first-match",
            "default_action": "local-only",
            "remotes": {},
            "rules": [],
            "project_groups": {},
        }
        assert validate_sync_config(config) == []

    def test_invalid_strategy(self):
        config = {"strategy": "round-robin"}
        errors = validate_sync_config(config)
        assert any("strategy" in e for e in errors)

    def test_invalid_default_action(self):
        config = {"default_action": "sync-all"}
        errors = validate_sync_config(config)
        assert any("default_action" in e for e in errors)

    def test_remote_missing_url(self):
        config = {"remotes": {"work": {}}, "rules": []}
        errors = validate_sync_config(config)
        assert any("no URL" in e for e in errors)

    def test_remote_unresolvable_env_var(self):
        config = {
            "remotes": {"work": {"url": "postgresql://$MISSING_VAR:5432/db"}},
            "rules": [],
        }
        errors = validate_sync_config(config)
        assert any("MISSING_VAR" in e for e in errors)

    def test_remote_resolvable_env_var(self, monkeypatch):
        monkeypatch.setenv("PG_URL", "postgresql://host:5432/db")
        config = {
            "remotes": {"work": {"url": "$PG_URL"}},
            "rules": [],
        }
        assert validate_sync_config(config) == []

    def test_rule_invalid_action(self):
        config = {
            "remotes": {},
            "rules": [{"name": "bad", "action": "forward"}],
        }
        errors = validate_sync_config(config)
        assert any("invalid action" in e for e in errors)

    def test_route_to_missing_destinations(self):
        config = {
            "remotes": {},
            "rules": [{"name": "empty", "action": "route-to", "destinations": []}],
        }
        errors = validate_sync_config(config)
        assert any("requires at least one destination" in e for e in errors)

    def test_destination_not_in_remotes(self):
        config = {
            "remotes": {"work": {"url": "postgresql://h:5432/db"}},
            "rules": [
                {"name": "bad-dest", "action": "route-to",
                 "destinations": ["nonexistent"]},
            ],
        }
        errors = validate_sync_config(config)
        assert any("nonexistent" in e and "not found" in e for e in errors)

    def test_undefined_project_group(self):
        config = {
            "remotes": {},
            "rules": [
                {"name": "bad-group", "action": "deny",
                 "destinations": [],
                 "match": {"project": ["@undefined"]}},
            ],
            "project_groups": {},
        }
        errors = validate_sync_config(config)
        assert any("@undefined" in e for e in errors)

    def test_valid_full_config(self, monkeypatch):
        monkeypatch.setenv("WORK_PG", "postgresql://h:5432/db")
        config = {
            "strategy": "all-match",
            "default_action": "local-only",
            "remotes": {
                "work": {"url": "$WORK_PG"},
                "backup": {"url": "postgresql://backup:5432/db"},
            },
            "rules": [
                {"name": "work-route", "action": "route-to",
                 "destinations": ["work"],
                 "match": {"project": ["@work"]}},
                {"name": "deny-sensitive", "action": "deny",
                 "destinations": ["backup"],
                 "match": {"category": ["relationship"]}},
            ],
            "project_groups": {"@work": ["work-*"]},
        }
        assert validate_sync_config(config) == []


class TestSchemaValidation:
    """Tests for remote schema field validation."""

    def test_valid_schema_names_pass(self):
        """Valid PG identifiers pass validation."""
        for schema in ("personio", "work_context", "aurora", "a", "_private"):
            config = {
                "remotes": {
                    "r": {"url": "postgresql://h:5432/db", "schema": schema},
                },
                "rules": [],
            }
            errors = validate_sync_config(config)
            schema_errors = [e for e in errors if "schema" in e.lower()]
            assert schema_errors == [], f"schema '{schema}' should be valid: {schema_errors}"

    def test_remote_name_as_default_schema_valid(self):
        """When schema omitted, remote name is validated as the schema."""
        config = {
            "remotes": {
                "aurora": {"url": "postgresql://h:5432/db"},
            },
            "rules": [],
        }
        errors = validate_sync_config(config)
        schema_errors = [e for e in errors if "schema" in e.lower()]
        assert schema_errors == []

    def test_remote_name_as_default_schema_invalid(self):
        """Remote name that is not a valid PG identifier fails when no explicit schema."""
        config = {
            "remotes": {
                "0x88": {"url": "postgresql://h:5432/db"},
            },
            "rules": [],
        }
        errors = validate_sync_config(config)
        assert any("schema" in e.lower() and "0x88" in e for e in errors)

    def test_reserved_schema_pg_catalog(self):
        config = {
            "remotes": {
                "r": {"url": "postgresql://h:5432/db", "schema": "pg_catalog"},
            },
            "rules": [],
        }
        errors = validate_sync_config(config)
        assert any("reserved" in e for e in errors)

    def test_reserved_schema_information_schema(self):
        config = {
            "remotes": {
                "r": {"url": "postgresql://h:5432/db", "schema": "information_schema"},
            },
            "rules": [],
        }
        errors = validate_sync_config(config)
        assert any("reserved" in e for e in errors)

    def test_reserved_schema_public(self):
        config = {
            "remotes": {
                "r": {"url": "postgresql://h:5432/db", "schema": "public"},
            },
            "rules": [],
        }
        errors = validate_sync_config(config)
        assert any("reserved" in e for e in errors)

    def test_reserved_schema_local(self):
        """'local' is reserved — must never push to another host's local schema."""
        config = {
            "remotes": {
                "r": {"url": "postgresql://h:5432/db", "schema": "local"},
            },
            "rules": [],
        }
        errors = validate_sync_config(config)
        assert any("reserved" in e for e in errors)

    def test_invalid_schema_starts_with_digit(self):
        config = {
            "remotes": {
                "r": {"url": "postgresql://h:5432/db", "schema": "123bad"},
            },
            "rules": [],
        }
        errors = validate_sync_config(config)
        assert any("PostgreSQL identifier" in e for e in errors)

    def test_invalid_schema_has_spaces(self):
        config = {
            "remotes": {
                "r": {"url": "postgresql://h:5432/db", "schema": "has spaces"},
            },
            "rules": [],
        }
        errors = validate_sync_config(config)
        assert any("PostgreSQL identifier" in e for e in errors)

    def test_invalid_schema_sql_injection(self):
        config = {
            "remotes": {
                "r": {"url": "postgresql://h:5432/db", "schema": "DROP TABLE"},
            },
            "rules": [],
        }
        errors = validate_sync_config(config)
        assert any("PostgreSQL identifier" in e for e in errors)

    def test_invalid_schema_uppercase(self):
        """PG identifiers in our validation are lowercase-only."""
        config = {
            "remotes": {
                "r": {"url": "postgresql://h:5432/db", "schema": "MySchema"},
            },
            "rules": [],
        }
        errors = validate_sync_config(config)
        assert any("PostgreSQL identifier" in e for e in errors)

    def test_missing_schema_no_validation_error(self):
        """When schema is omitted and remote name is valid, no error."""
        config = {
            "remotes": {
                "work": {"url": "postgresql://h:5432/db"},
            },
            "rules": [],
        }
        errors = validate_sync_config(config)
        schema_errors = [e for e in errors if "schema" in e.lower()]
        assert schema_errors == []

    def test_explicit_schema_overrides_remote_name_validation(self):
        """Invalid remote name is OK if explicit schema is valid."""
        config = {
            "remotes": {
                "0x88": {
                    "url": "postgresql://h:5432/db",
                    "schema": "x0x88",
                },
            },
            "rules": [],
        }
        errors = validate_sync_config(config)
        schema_errors = [e for e in errors if "schema" in e.lower()]
        assert schema_errors == []


class TestLoadRoutingRules:
    def test_empty_rules(self):
        assert load_routing_rules({"rules": []}) == []

    def test_parses_rules_in_order(self):
        config = {
            "rules": [
                {"name": "first", "action": "deny", "destinations": ["a"]},
                {"name": "second", "action": "route-to", "destinations": ["b"]},
            ]
        }
        rules = load_routing_rules(config)
        assert len(rules) == 2
        assert rules[0].name == "first"
        assert rules[0].action == "deny"
        assert rules[1].name == "second"
        assert rules[1].action == "route-to"

    def test_no_rules_key(self):
        assert load_routing_rules({}) == []
