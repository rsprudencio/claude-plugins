"""Keep Claude and Codex plugin packaging definitions in sync."""

from __future__ import annotations

import json
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]
PLUGIN_NAMES = (
    "jarvis",
    "jarvis-todoist",
    "jarvis-strategic",
    "jarvis-toolbelt",
    "jarvis-obsidian",
)
CODEX_PLUGIN_NAMES = tuple(
    name for name in PLUGIN_NAMES if name != "jarvis-toolbelt"
)
MCP_PLUGINS = {
    "jarvis": "core",
    "jarvis-todoist": "api",
    "jarvis-obsidian": "vault",
}


def _load_json(relative_path: str) -> dict:
    path = REPO_ROOT / relative_path
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(payload, dict), f"{relative_path} must contain a JSON object"
    return payload


def _base_version(version: str) -> str:
    """Ignore the Codex-only local-development cachebuster metadata."""
    return version.split("+codex.", 1)[0]


def test_codex_manifests_match_claude_identity_and_mcp_definitions() -> None:
    for plugin_name in CODEX_PLUGIN_NAMES:
        plugin_root = REPO_ROOT / "plugins" / plugin_name
        claude = _load_json(f"plugins/{plugin_name}/.claude-plugin/plugin.json")
        codex = _load_json(f"plugins/{plugin_name}/.codex-plugin/plugin.json")

        assert codex["name"] == claude["name"] == plugin_root.name
        assert _base_version(codex["version"]) == claude["version"]
        assert "hooks" not in codex

        mcp_path = plugin_root / ".mcp.json"
        if plugin_name in MCP_PLUGINS:
            claude_mcp = json.loads(mcp_path.read_text(encoding="utf-8"))
            assert codex["mcpServers"] == claude_mcp
            assert set(codex["mcpServers"]) == {MCP_PLUGINS[plugin_name]}
        else:
            assert not mcp_path.exists()
            assert "mcpServers" not in codex


def test_marketplaces_contain_every_plugin_without_version_drift() -> None:
    claude_marketplace = _load_json(".claude-plugin/marketplace.json")
    codex_marketplace = _load_json(".agents/plugins/marketplace.json")

    assert claude_marketplace["name"] == codex_marketplace["name"]

    claude_entries = {
        entry["name"]: entry for entry in claude_marketplace["plugins"]
    }
    codex_entries = {entry["name"]: entry for entry in codex_marketplace["plugins"]}
    assert tuple(claude_entries) == PLUGIN_NAMES
    assert tuple(codex_entries) == CODEX_PLUGIN_NAMES

    for plugin_name in CODEX_PLUGIN_NAMES:
        manifest = _load_json(f"plugins/{plugin_name}/.codex-plugin/plugin.json")
        assert claude_entries[plugin_name]["version"] == _base_version(manifest["version"])
        assert claude_entries[plugin_name]["source"] == f"./plugins/{plugin_name}"

        codex_entry = codex_entries[plugin_name]
        assert codex_entry["source"] == {
            "source": "local",
            "path": f"./plugins/{plugin_name}",
        }
        assert codex_entry["policy"] == {
            "installation": "AVAILABLE",
            "authentication": "ON_INSTALL",
        }
        assert isinstance(codex_entry["category"], str)
        assert codex_entry["category"]

    assert "jarvis-toolbelt" in claude_entries
    assert "jarvis-toolbelt" not in codex_entries
    assert not (
        REPO_ROOT / "plugins/jarvis-toolbelt/.codex-plugin/plugin.json"
    ).exists()
