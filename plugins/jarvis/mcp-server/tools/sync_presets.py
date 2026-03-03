"""Sync configuration presets for easy onboarding.

Each preset returns a partial sync config dict that can be merged
with the user's existing config. Presets define rules and
project_groups — the user must still configure remotes.
"""

from __future__ import annotations

PRESETS = {
    "personal-backup": {
        "description": "Sync everything to a single backup remote",
        "strategy": "first-match",
        "rules": [
            {
                "name": "backup-all",
                "match": {},
                "action": "route-to",
                "destinations": ["backup"],
            }
        ],
        "project_groups": {},
    },
    "work-separation": {
        "description": "Route work projects to work remote, everything else to personal",
        "strategy": "all-match",
        "rules": [
            {
                "name": "work-to-work",
                "match": {"project": ["@work"]},
                "action": "route-to",
                "destinations": ["work"],
            },
            {
                "name": "personal-catch-all",
                "match": {},
                "action": "route-to",
                "destinations": ["personal"],
            },
        ],
        "project_groups": {
            "@work": ["work-*", "infra-*", "corp-*"],
        },
    },
    "privacy-first": {
        "description": "Explicit allow-list only — nothing syncs unless matched",
        "strategy": "first-match",
        "rules": [
            {
                "name": "deny-sensitive",
                "match": {"category": ["relationship", "observation"]},
                "action": "deny",
                "destinations": ["backup"],
            },
            {
                "name": "allow-decisions",
                "match": {"category": ["decision", "learning", "pattern"]},
                "action": "route-to",
                "destinations": ["backup"],
            },
        ],
        "project_groups": {},
    },
}


def get_preset(name: str) -> dict | None:
    """Get a sync config preset by name.

    Returns:
        Preset config dict, or None if not found.
    """
    return PRESETS.get(name)


def list_presets() -> list[dict]:
    """List available presets with descriptions.

    Returns:
        List of dicts with name and description.
    """
    return [
        {"name": name, "description": preset["description"]}
        for name, preset in PRESETS.items()
    ]
