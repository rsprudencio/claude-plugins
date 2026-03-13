"""Admin API router for Jarvis Memory Explorer.

CRUD endpoints for sync routing rules, remotes, and project groups.
All write endpoints are gated by jarvis_common.auth (Bearer token or mTLS).
GET endpoints are unauthenticated (read-only, no credentials exposed).

Mounted in app.py as: app.include_router(admin_router)
"""

from __future__ import annotations

import asyncio
import copy
import logging
import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, field_validator

from jarvis_common.auth import authenticate, get_auth_config
from jarvis_common.config import get_config
from jarvis_common.config_writer import read_config_file, update_sync_section
from jarvis_common.routing import VALID_CATEGORIES, evaluate_routing, parse_routing_rule
from jarvis_common.sync_validation import redact_dsn

logger = logging.getLogger("memory-explorer.admin")

# Valid remote name pattern
_REMOTE_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]*$")


# ── Auth dependency ────────────────────────────────────────────────────────

async def require_auth(request: Request) -> str:
    """Auth dependency — reuses jarvis-common auth module.

    When server.auth.enabled=false (default), admin endpoints are open
    (returns "anonymous") — consistent with how MCP servers behave.
    """
    auth_cfg = get_auth_config()
    if auth_cfg is None:
        return "anonymous"
    username, err = authenticate(request.scope)
    if err:
        raise HTTPException(status_code=401, detail=err)
    return username


# ── Request models ─────────────────────────────────────────────────────────

class MatchRequest(BaseModel):
    category: list[str] = []
    project: list[str] = []
    tags: list[str] = []
    importance_min: Optional[float] = None
    scope: Optional[str] = None
    path_prefix: list[str] = []

    @field_validator("category", mode="before")
    @classmethod
    def validate_categories(cls, v):
        if isinstance(v, list):
            for cat in v:
                if cat not in VALID_CATEGORIES:
                    raise ValueError(f"Invalid category '{cat}'. Must be one of: {sorted(VALID_CATEGORIES)}")
        return v


class RuleRequest(BaseModel):
    name: str
    action: Literal["route-to", "deny"]
    destinations: list[str] = []
    match: MatchRequest = MatchRequest()

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        if not v or not v.strip():
            raise ValueError("Rule name cannot be empty")
        return v.strip()


class RemoteRequest(BaseModel):
    name: Optional[str] = None
    host: str = "localhost"
    port: int = 5432
    database: str = "jarvis"
    user: str = "jarvis"
    password: Optional[str] = None
    auth_method: str = "password"
    schema_name: Optional[str] = None
    sslmode: str = "require"
    enabled: bool = True
    description: str = ""

    @field_validator("name")
    @classmethod
    def validate_name(cls, v):
        if v is not None and not _REMOTE_NAME_RE.match(v):
            raise ValueError(
                f"Remote name '{v}' is invalid. "
                "Must match [a-z_][a-z0-9_]* (lowercase, no spaces)"
            )
        return v


class ReorderRequest(BaseModel):
    order: list[str]


class ProjectGroupsRequest(BaseModel):
    groups: dict[str, list[str]]


class RuleTestRequest(BaseModel):
    category: str = "observation"
    project: str = ""
    scope: str = "global"
    importance_score: float = 0.5
    tags: str = ""
    metadata: dict = {}


# ── Helpers ────────────────────────────────────────────────────────────────

def _get_sync_section() -> dict:
    """Get the memory.sync section from config."""
    config = get_config()
    memory = config.get("memory", {})
    if not isinstance(memory, dict):
        return {}
    sync = memory.get("sync", {})
    return sync if isinstance(sync, dict) else {}


def _remote_to_response(name: str, remote: dict) -> dict:
    """Convert a remote config dict to an API response (no credentials)."""
    return {
        "name": name,
        "host": remote.get("host", "localhost"),
        "port": remote.get("port", 5432),
        "database": remote.get("database", "jarvis"),
        "user": remote.get("user", "jarvis"),
        "has_password": bool(remote.get("password")),
        "auth_method": remote.get("auth_method", "password"),
        "schema_name": remote.get("schema", name),
        "sslmode": remote.get("sslmode", "require"),
        "enabled": remote.get("enabled", True),
        "description": remote.get("description", ""),
    }


def _remote_from_request(req: RemoteRequest, name: str) -> dict:
    """Convert a RemoteRequest to a config dict (with URL construction)."""
    import urllib.parse

    remote: dict = {
        "host": req.host,
        "port": req.port,
        "database": req.database,
        "user": req.user,
        "auth_method": req.auth_method,
        "sslmode": req.sslmode,
        "enabled": req.enabled,
        "description": req.description,
    }

    if req.schema_name:
        remote["schema"] = req.schema_name

    # Password handling
    if req.password is not None and req.password != "***":
        remote["password"] = req.password

    # Build URL
    pw_part = ""
    if req.password and req.password != "***":
        if req.password.startswith("$"):
            pw_part = f":{req.password}"
        else:
            pw_part = f":{urllib.parse.quote(req.password, safe='')}"
    elif req.password == "***":
        pw_part = ":***"  # sentinel — will be re-hydrated

    remote["url"] = (
        f"postgresql://{urllib.parse.quote(req.user, safe='')}"
        f"{pw_part}@{req.host}:{req.port}/{req.database}"
        f"?sslmode={req.sslmode}"
    )

    return remote


def _rule_to_dict(rule: dict) -> dict:
    """Normalize a rule dict for API response."""
    return {
        "name": rule.get("name", "unnamed"),
        "action": rule.get("action", "route-to"),
        "destinations": rule.get("destinations", []),
        "match": rule.get("match", {}),
    }


# ── Router ─────────────────────────────────────────────────────────────────

admin_router = APIRouter(prefix="/api/admin")


# ── Rules endpoints ────────────────────────────────────────────────────────

@admin_router.get("/rules")
async def get_rules():
    """Return all routing rules with strategy and default action."""
    sync = _get_sync_section()
    rules = [_rule_to_dict(r) for r in sync.get("rules", [])]
    return {
        "rules": rules,
        "strategy": sync.get("strategy", "first-match"),
        "default_action": sync.get("default_action", "local-only"),
    }


@admin_router.post("/rules", dependencies=[Depends(require_auth)])
async def create_rule(req: RuleRequest):
    """Add a new routing rule."""
    def updater(sync: dict) -> dict:
        rules = sync.get("rules", [])
        # Check name uniqueness
        for r in rules:
            if r.get("name") == req.name:
                raise ValueError(f"Rule '{req.name}' already exists")
        rules.append({
            "name": req.name,
            "action": req.action,
            "destinations": req.destinations,
            "match": req.match.model_dump(exclude_none=True, exclude_defaults=True),
        })
        sync["rules"] = rules
        return sync

    loop = asyncio.get_event_loop()
    try:
        new_sync, errors = await loop.run_in_executor(None, update_sync_section, updater)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    return {"ok": True, "rules": [_rule_to_dict(r) for r in new_sync.get("rules", [])]}


@admin_router.put("/rules/{name}", dependencies=[Depends(require_auth)])
async def update_rule(name: str, req: RuleRequest):
    """Update an existing routing rule by name."""
    def updater(sync: dict) -> dict:
        rules = sync.get("rules", [])
        found = False
        for i, r in enumerate(rules):
            if r.get("name") == name:
                rules[i] = {
                    "name": req.name,
                    "action": req.action,
                    "destinations": req.destinations,
                    "match": req.match.model_dump(exclude_none=True, exclude_defaults=True),
                }
                found = True
                break
        if not found:
            raise ValueError(f"Rule '{name}' not found")
        # If name changed, check for conflicts
        if req.name != name:
            names = [r.get("name") for r in rules]
            if names.count(req.name) > 1:
                raise ValueError(f"Rule '{req.name}' already exists")
        sync["rules"] = rules
        return sync

    loop = asyncio.get_event_loop()
    try:
        new_sync, errors = await loop.run_in_executor(None, update_sync_section, updater)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    return {"ok": True, "rules": [_rule_to_dict(r) for r in new_sync.get("rules", [])]}


@admin_router.delete("/rules/{name}", dependencies=[Depends(require_auth)])
async def delete_rule(name: str):
    """Delete a routing rule by name."""
    def updater(sync: dict) -> dict:
        rules = sync.get("rules", [])
        new_rules = [r for r in rules if r.get("name") != name]
        if len(new_rules) == len(rules):
            raise ValueError(f"Rule '{name}' not found")
        sync["rules"] = new_rules
        return sync

    loop = asyncio.get_event_loop()
    try:
        new_sync, errors = await loop.run_in_executor(None, update_sync_section, updater)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    return {"ok": True, "rules": [_rule_to_dict(r) for r in new_sync.get("rules", [])]}


@admin_router.post("/rules/reorder", dependencies=[Depends(require_auth)])
async def reorder_rules(req: ReorderRequest):
    """Reorder routing rules by name list."""
    def updater(sync: dict) -> dict:
        rules = sync.get("rules", [])
        by_name = {r.get("name"): r for r in rules}
        # Validate all names exist
        for name in req.order:
            if name not in by_name:
                raise ValueError(f"Rule '{name}' not found")
        # Validate no names missing
        existing_names = set(by_name.keys())
        order_names = set(req.order)
        missing = existing_names - order_names
        if missing:
            raise ValueError(f"Missing rules in order: {sorted(missing)}")
        sync["rules"] = [by_name[n] for n in req.order]
        return sync

    loop = asyncio.get_event_loop()
    try:
        new_sync, errors = await loop.run_in_executor(None, update_sync_section, updater)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    return {"ok": True, "rules": [_rule_to_dict(r) for r in new_sync.get("rules", [])]}


@admin_router.post("/rules/test")
async def test_routing(req: RuleTestRequest):
    """Test routing rules against a sample memory."""
    sync = _get_sync_section()
    rules = [parse_routing_rule(r) for r in sync.get("rules", [])]
    strategy = sync.get("strategy", "first-match")
    project_groups = sync.get("project_groups", {})

    memory = {
        "category": req.category,
        "project": req.project,
        "scope": req.scope,
        "importance_score": req.importance_score,
        "metadata": {
            "tags": req.tags,
            **req.metadata,
        },
    }

    decision = evaluate_routing(memory, rules, strategy, project_groups)
    return {
        "destinations": decision.destinations,
        "matched_rules": decision.matched_rules,
        "denied": decision.denied_destinations,
    }


# ── Remotes endpoints ─────────────────────────────────────────────────────

@admin_router.get("/remotes")
async def get_remotes():
    """Return all remotes (passwords redacted)."""
    sync = _get_sync_section()
    remotes = []
    for name, rcfg in sync.get("remotes", {}).items():
        if name.startswith("_"):
            continue
        remotes.append(_remote_to_response(name, rcfg))
    return {"remotes": remotes}


@admin_router.post("/remotes", dependencies=[Depends(require_auth)])
async def create_remote(req: RemoteRequest):
    """Add a new remote."""
    if not req.name:
        raise HTTPException(status_code=400, detail="Remote name is required")

    def updater(sync: dict) -> dict:
        remotes = sync.get("remotes", {})
        if req.name in remotes:
            raise ValueError(f"Remote '{req.name}' already exists")
        remotes[req.name] = _remote_from_request(req, req.name)
        sync["remotes"] = remotes
        return sync

    loop = asyncio.get_event_loop()
    try:
        new_sync, errors = await loop.run_in_executor(None, update_sync_section, updater)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    return {"ok": True}


@admin_router.put("/remotes/{name}", dependencies=[Depends(require_auth)])
async def update_remote(name: str, req: RemoteRequest):
    """Update an existing remote."""
    def updater(sync: dict) -> dict:
        remotes = sync.get("remotes", {})
        if name not in remotes:
            raise ValueError(f"Remote '{name}' not found")
        remotes[name] = _remote_from_request(req, name)
        sync["remotes"] = remotes
        return sync

    loop = asyncio.get_event_loop()
    try:
        new_sync, errors = await loop.run_in_executor(None, update_sync_section, updater)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    return {"ok": True}


@admin_router.delete("/remotes/{name}", dependencies=[Depends(require_auth)])
async def delete_remote(name: str):
    """Delete a remote. Returns 409 if rules reference it."""
    sync = _get_sync_section()
    # Check for dependent rules
    dependent_rules = []
    for rule in sync.get("rules", []):
        if name in rule.get("destinations", []):
            dependent_rules.append(rule.get("name", "unnamed"))
    if dependent_rules:
        raise HTTPException(
            status_code=409,
            detail={
                "error": f"Remote '{name}' is referenced by rules: {dependent_rules}",
                "affected_rules": dependent_rules,
            },
        )

    def updater(sync: dict) -> dict:
        remotes = sync.get("remotes", {})
        if name not in remotes:
            raise ValueError(f"Remote '{name}' not found")
        del remotes[name]
        sync["remotes"] = remotes
        return sync

    loop = asyncio.get_event_loop()
    try:
        new_sync, errors = await loop.run_in_executor(None, update_sync_section, updater)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    return {"ok": True}


@admin_router.post("/remotes/{name}/test", dependencies=[Depends(require_auth)])
async def test_remote(name: str):
    """Test connectivity to a remote (5s connect timeout, 10s total)."""
    sync = _get_sync_section()
    remotes = sync.get("remotes", {})
    if name not in remotes:
        raise HTTPException(status_code=404, detail=f"Remote '{name}' not found")

    rcfg = remotes[name]
    url = rcfg.get("url", "")
    if not url:
        return {"connected": False, "error": "No URL configured"}

    async def _test_connect():
        try:
            from jarvis_common.sync_validation import resolve_env_vars
            resolved_url = resolve_env_vars(url)
        except ValueError as e:
            return {"connected": False, "error": str(e)}

        try:
            import psycopg
            conn = await asyncio.wait_for(
                asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: psycopg.connect(resolved_url, connect_timeout=5),
                ),
                timeout=10,
            )
            conn.close()
            return {"connected": True}
        except asyncio.TimeoutError:
            return {"connected": False, "error": "Connection timed out (10s)"}
        except Exception as e:
            return {"connected": False, "error": str(e)[:200]}

    return await _test_connect()


# ── Project groups endpoints ───────────────────────────────────────────────

@admin_router.get("/project-groups")
async def get_project_groups():
    """Return all project groups."""
    sync = _get_sync_section()
    return {"groups": sync.get("project_groups", {})}


@admin_router.put("/project-groups", dependencies=[Depends(require_auth)])
async def update_project_groups(req: ProjectGroupsRequest):
    """Replace all project groups."""
    def updater(sync: dict) -> dict:
        sync["project_groups"] = req.groups
        return sync

    loop = asyncio.get_event_loop()
    new_sync, errors = await loop.run_in_executor(None, update_sync_section, updater)
    if errors:
        raise HTTPException(status_code=400, detail={"errors": errors})
    return {"ok": True}
