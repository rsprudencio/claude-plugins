"""Tests for the admin API endpoints (app_admin.py).

Uses FastAPI TestClient with mocked config file I/O.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure app_admin is importable
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app_admin import admin_router, require_auth

# ── Test app setup ──────────────────────────────────────────────────────────

_test_app = FastAPI()
_test_app.include_router(admin_router)


# ── Fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """Isolate config to temp directory."""
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path))
    from jarvis_common.config import clear_config_cache
    clear_config_cache()
    yield
    clear_config_cache()


def _write_config(tmp_path, config):
    (tmp_path / "config.json").write_text(json.dumps(config, indent=2))


def _base_config(rules=None, remotes=None):
    return {
        "memory": {
            "sync": {
                "enabled": True,
                "strategy": "first-match",
                "default_action": "local-only",
                "remotes": remotes or {},
                "rules": rules or [],
                "project_groups": {},
            }
        }
    }


@pytest.fixture
def client():
    return TestClient(_test_app)


@pytest.fixture
def config_with_remote(tmp_path):
    config = _base_config(
        remotes={
            "aurora": {
                "url": "postgresql://u:p@host:5432/db?sslmode=require",
                "host": "host",
                "port": 5432,
                "database": "db",
                "user": "u",
                "password": "secret",
                "auth_method": "password",
                "schema": "aurora",
                "sslmode": "require",
                "enabled": True,
                "description": "Test remote",
            }
        },
        rules=[
            {
                "name": "route-all",
                "action": "route-to",
                "destinations": ["aurora"],
                "match": {"category": ["observation"]},
            }
        ],
    )
    _write_config(tmp_path, config)
    return config


# ── Auth tests ──────────────────────────────────────────────────────────────

class TestAuth:
    def test_no_auth_configured_allows_access(self, tmp_path, client):
        """When auth is disabled (default), write endpoints are open."""
        _write_config(tmp_path, _base_config())
        r = client.post("/api/admin/rules", json={
            "name": "test", "action": "route-to", "destinations": [],
        })
        # Should not be 401
        assert r.status_code != 401

    def test_auth_enabled_rejects_without_token(self, tmp_path, client):
        token = "test-secret"
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        config = _base_config()
        config["server"] = {"auth": {"enabled": True, "tokens": {token_hash: "admin"}}}
        _write_config(tmp_path, config)
        r = client.post("/api/admin/rules", json={
            "name": "test", "action": "route-to", "destinations": [],
        })
        assert r.status_code == 401

    def test_auth_enabled_accepts_valid_token(self, tmp_path, client):
        token = "test-secret"
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        config = _base_config()
        config["server"] = {"auth": {"enabled": True, "tokens": {token_hash: "admin"}}}
        _write_config(tmp_path, config)
        r = client.get("/api/admin/rules", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200

    def test_auth_enabled_rejects_wrong_token(self, tmp_path, client):
        token_hash = hashlib.sha256(b"right-token").hexdigest()
        config = _base_config()
        config["server"] = {"auth": {"enabled": True, "tokens": {token_hash: "admin"}}}
        _write_config(tmp_path, config)
        r = client.post("/api/admin/rules", json={
            "name": "test", "action": "route-to", "destinations": [],
        }, headers={"Authorization": "Bearer wrong-token"})
        assert r.status_code == 401


# ── Rules CRUD tests ───────────────────────────────────────────────────────

class TestRulesCRUD:
    def test_get_empty_rules(self, tmp_path, client):
        _write_config(tmp_path, _base_config())
        r = client.get("/api/admin/rules")
        assert r.status_code == 200
        data = r.json()
        assert data["rules"] == []
        assert data["strategy"] == "first-match"

    def test_create_rule(self, tmp_path, client):
        _write_config(tmp_path, _base_config(remotes={
            "prod": {"url": "postgresql://u:p@h/d", "schema": "prod"},
        }))
        r = client.post("/api/admin/rules", json={
            "name": "test-rule",
            "action": "route-to",
            "destinations": ["prod"],
            "match": {"category": ["observation"]},
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True
        assert len(r.json()["rules"]) == 1
        assert r.json()["rules"][0]["name"] == "test-rule"

    def test_create_duplicate_name_fails(self, tmp_path, client, config_with_remote):
        r = client.post("/api/admin/rules", json={
            "name": "route-all",
            "action": "route-to",
            "destinations": ["aurora"],
        })
        assert r.status_code == 400
        assert "already exists" in str(r.json()["detail"])

    def test_update_rule(self, tmp_path, client, config_with_remote):
        r = client.put("/api/admin/rules/route-all", json={
            "name": "route-all",
            "action": "deny",
            "destinations": ["aurora"],
            "match": {},
        })
        assert r.status_code == 200
        rules = r.json()["rules"]
        assert rules[0]["action"] == "deny"

    def test_update_nonexistent_rule(self, tmp_path, client):
        _write_config(tmp_path, _base_config())
        r = client.put("/api/admin/rules/nope", json={
            "name": "nope", "action": "route-to", "destinations": [],
        })
        assert r.status_code == 400

    def test_delete_rule(self, tmp_path, client, config_with_remote):
        r = client.delete("/api/admin/rules/route-all")
        assert r.status_code == 200
        assert len(r.json()["rules"]) == 0

    def test_delete_nonexistent_rule(self, tmp_path, client):
        _write_config(tmp_path, _base_config())
        r = client.delete("/api/admin/rules/nope")
        assert r.status_code == 404

    def test_reorder_rules(self, tmp_path, client):
        _write_config(tmp_path, _base_config(
            remotes={"r": {"url": "postgresql://u:p@h/d", "schema": "r"}},
            rules=[
                {"name": "a", "action": "route-to", "destinations": ["r"], "match": {}},
                {"name": "b", "action": "route-to", "destinations": ["r"], "match": {}},
                {"name": "c", "action": "route-to", "destinations": ["r"], "match": {}},
            ],
        ))
        r = client.post("/api/admin/rules/reorder", json={"order": ["c", "a", "b"]})
        assert r.status_code == 200
        names = [rule["name"] for rule in r.json()["rules"]]
        assert names == ["c", "a", "b"]

    def test_reorder_missing_rule(self, tmp_path, client, config_with_remote):
        r = client.post("/api/admin/rules/reorder", json={"order": ["nope"]})
        assert r.status_code == 400

    def test_invalid_action_type(self, tmp_path, client):
        _write_config(tmp_path, _base_config())
        r = client.post("/api/admin/rules", json={
            "name": "bad", "action": "invalid-action", "destinations": [],
        })
        assert r.status_code == 422  # Pydantic validation

    def test_invalid_category(self, tmp_path, client):
        _write_config(tmp_path, _base_config())
        r = client.post("/api/admin/rules", json={
            "name": "bad", "action": "route-to", "destinations": [],
            "match": {"category": ["not-a-category"]},
        })
        assert r.status_code == 422


# ── Rule tester ────────────────────────────────────────────────────────────

class TestRuleTester:
    def test_test_routing(self, tmp_path, client, config_with_remote):
        r = client.post("/api/admin/rules/test", json={
            "category": "observation",
            "project": "test",
            "scope": "global",
            "importance_score": 0.8,
        })
        assert r.status_code == 200
        data = r.json()
        assert "aurora" in data["destinations"]
        assert "route-all" in data["matched_rules"]

    def test_test_no_match(self, tmp_path, client, config_with_remote):
        r = client.post("/api/admin/rules/test", json={
            "category": "code",  # rule only matches "observation"
        })
        assert r.status_code == 200
        assert r.json()["destinations"] == []


# ── Remotes CRUD ───────────────────────────────────────────────────────────

class TestRemotesCRUD:
    def test_get_remotes(self, tmp_path, client, config_with_remote):
        r = client.get("/api/admin/remotes")
        assert r.status_code == 200
        remotes = r.json()["remotes"]
        assert len(remotes) == 1
        assert remotes[0]["name"] == "aurora"
        assert remotes[0]["has_password"] is True
        # Password should NOT be in response
        assert "password" not in remotes[0] or remotes[0].get("password") is None

    def test_create_remote(self, tmp_path, client):
        _write_config(tmp_path, _base_config())
        r = client.post("/api/admin/remotes", json={
            "name": "new_remote",
            "host": "db.example.com",
            "port": 5432,
            "database": "jarvis",
            "user": "jarvis",
            "password": "secret123",
            "sslmode": "require",
        })
        assert r.status_code == 200
        assert r.json()["ok"] is True
        # Verify on disk
        on_disk = json.loads((tmp_path / "config.json").read_text())
        remote = on_disk["memory"]["sync"]["remotes"]["new_remote"]
        assert "db.example.com" in remote["url"]

    def test_create_duplicate_remote(self, tmp_path, client, config_with_remote):
        r = client.post("/api/admin/remotes", json={
            "name": "aurora", "host": "h",
        })
        assert r.status_code == 400
        assert "already exists" in str(r.json()["detail"])

    def test_create_invalid_name(self, tmp_path, client):
        _write_config(tmp_path, _base_config())
        r = client.post("/api/admin/remotes", json={
            "name": "Invalid Name!", "host": "h",
        })
        assert r.status_code == 422

    def test_update_remote(self, tmp_path, client, config_with_remote):
        r = client.put("/api/admin/remotes/aurora", json={
            "host": "new-host.example.com",
            "port": 5432,
            "database": "jarvis",
            "user": "jarvis",
            "password": "***",  # Keep existing
            "sslmode": "require",
        })
        assert r.status_code == 200

    def test_delete_remote_with_dependent_rules(self, tmp_path, client, config_with_remote):
        r = client.delete("/api/admin/remotes/aurora")
        assert r.status_code == 409
        data = r.json()
        assert "route-all" in data["detail"]["affected_rules"]

    def test_delete_remote_no_deps(self, tmp_path, client):
        _write_config(tmp_path, _base_config(remotes={
            "orphan": {"url": "postgresql://u:p@h/d", "schema": "orphan"},
        }))
        r = client.delete("/api/admin/remotes/orphan")
        assert r.status_code == 200

    def test_delete_nonexistent_remote(self, tmp_path, client):
        _write_config(tmp_path, _base_config())
        r = client.delete("/api/admin/remotes/nope")
        assert r.status_code == 404

    def test_env_var_password_preserved(self, tmp_path, client):
        _write_config(tmp_path, _base_config(remotes={
            "env_remote": {
                "url": "postgresql://u:$MY_PW@h/d",
                "password": "$MY_PW",
                "schema": "env_remote",
            },
        }))
        r = client.put("/api/admin/remotes/env_remote", json={
            "host": "new-host",
            "port": 5432,
            "database": "d",
            "user": "u",
            "password": "***",
            "sslmode": "require",
        })
        assert r.status_code == 200
        on_disk = json.loads((tmp_path / "config.json").read_text())
        assert on_disk["memory"]["sync"]["remotes"]["env_remote"]["password"] == "$MY_PW"

    def test_templates_hidden(self, tmp_path, client):
        """Remotes starting with _ should not appear in GET."""
        _write_config(tmp_path, _base_config(remotes={
            "_template": {"url": "postgresql://u:p@h/d", "schema": "tpl"},
            "real": {"url": "postgresql://u:p@h/d", "schema": "real"},
        }))
        r = client.get("/api/admin/remotes")
        names = [rem["name"] for rem in r.json()["remotes"]]
        assert "_template" not in names
        assert "real" in names


# ── Project groups ─────────────────────────────────────────────────────────

class TestProjectGroups:
    def test_get_empty_groups(self, tmp_path, client):
        _write_config(tmp_path, _base_config())
        r = client.get("/api/admin/project-groups")
        assert r.status_code == 200
        assert r.json()["groups"] == {}

    def test_update_groups(self, tmp_path, client):
        _write_config(tmp_path, _base_config())
        r = client.put("/api/admin/project-groups", json={
            "groups": {"@work": ["work-*", "infra-*"]},
        })
        assert r.status_code == 200
        # Verify persisted
        r2 = client.get("/api/admin/project-groups")
        assert r2.json()["groups"]["@work"] == ["work-*", "infra-*"]


# ── Connection test ────────────────────────────────────────────────────────

class TestConnectionTest:
    def test_nonexistent_remote(self, tmp_path, client):
        _write_config(tmp_path, _base_config())
        r = client.post("/api/admin/remotes/nope/test")
        assert r.status_code == 404

    def test_connection_failure(self, tmp_path, client, config_with_remote):
        """Real connection will fail (no actual DB), but endpoint should handle gracefully."""
        r = client.post("/api/admin/remotes/aurora/test")
        assert r.status_code == 200
        data = r.json()
        assert data["connected"] is False
        assert "error" in data
