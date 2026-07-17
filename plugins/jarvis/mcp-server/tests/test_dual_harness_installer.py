"""Smoke-test the installer against Claude- and Codex-shaped plugin CLIs."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[4]


def _executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def _fake_runtime(tmp_path: Path, harness: str) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    log_path = tmp_path / "harness.log"
    plugin_root = REPO_ROOT / "plugins/jarvis"

    if harness == "codex":
        list_payload = {
            "installed": [
                {
                    "pluginId": "jarvis@jarvis-plugins",
                    "source": {"source": "local", "path": str(plugin_root)},
                }
            ]
        }
    else:
        list_payload = [
            {
                "id": "jarvis@jarvis-plugins",
                "installPath": str(plugin_root),
            }
        ]

    _executable(
        fake_bin / harness,
        """#!/bin/bash
printf '%s\n' "$*" >> "$HARNESS_LOG"
if [ "$1 $2 $3" = "plugin marketplace list" ]; then
    printf '%s\n' 'jarvis-plugins'
elif [ "$1 $2 $3" = "plugin list --json" ]; then
    printf '%s\n' "$PLUGIN_LIST_JSON"
fi
exit 0
""",
    )
    _executable(
        fake_bin / "docker",
        """#!/bin/bash
if [ "$1" = "pull" ]; then
    printf '%s\n' 'pulled'
fi
exit 0
""",
    )
    _executable(
        fake_bin / "curl",
        """#!/bin/bash
printf '%s\n' '{"postgres":{"status":"ok","doc_count":0}}'
exit 0
""",
    )

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path / "home"),
            "JARVIS_HOME": str(tmp_path / "home/.jarvis"),
            "JARVIS_HARNESS": harness,
            "HARNESS_LOG": str(log_path),
            "PLUGIN_LIST_JSON": json.dumps(list_payload),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        }
    )
    env.pop("ANTHROPIC_API_KEY", None)
    return env, log_path


def _run_installer(tmp_path: Path, harness: str) -> tuple[str, str, dict]:
    env, log_path = _fake_runtime(tmp_path, harness)
    result = subprocess.run(
        ["/bin/bash", str(REPO_ROOT / "install.sh")],
        env=env,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    config_path = Path(env["JARVIS_HOME"]) / "config.json"
    return result.stdout, log_path.read_text(encoding="utf-8"), json.loads(
        config_path.read_text(encoding="utf-8")
    )


def test_codex_installer_uses_add_refreshes_snapshot_and_disables_extraction(
    tmp_path: Path,
) -> None:
    stdout, calls, config = _run_installer(tmp_path, "codex")

    assert "plugin marketplace add rsprudencio/jarvis" in calls
    assert "plugin marketplace upgrade jarvis-plugins" in calls
    assert "plugin add jarvis@jarvis-plugins" in calls
    assert "plugin add jarvis-obsidian@jarvis-plugins" in calls
    assert "jarvis-toolbelt" not in calls
    assert "activate Jarvis inside any Claude session" not in stdout
    assert config["memory"]["auto_extract"]["mode"] == "disabled"


def test_claude_installer_keeps_install_protocol_and_default_extraction(
    tmp_path: Path,
) -> None:
    _, calls, config = _run_installer(tmp_path, "claude")

    assert "plugin install jarvis@jarvis-plugins" in calls
    assert "plugin install jarvis-obsidian@jarvis-plugins" in calls
    assert "plugin add" not in calls
    assert "marketplace upgrade" not in calls
    assert config["memory"]["auto_extract"]["mode"] == "background"
