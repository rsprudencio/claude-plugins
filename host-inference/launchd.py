#!/usr/bin/env python3
"""Install and manage Jarvis host inference as macOS LaunchAgents."""

from __future__ import annotations

import argparse
import os
import plistlib
import platform
import subprocess
import sys
import time
from pathlib import Path

import host_models


LABELS = {
    "granite_embedding": "com.jarvis.model-host.embedding",
    "bge_reranker": "com.jarvis.model-host.reranker",
}
DEFAULT_AGENT_DIR = Path.home() / "Library" / "LaunchAgents"
DEFAULT_LOG_DIR = Path.home() / ".jarvis" / "logs" / "model-host"


class LaunchdError(RuntimeError):
    """Raised when LaunchAgent management fails."""


def launch_domain() -> str:
    return f"gui/{os.getuid()}"


def plist_path(agent_dir: Path, name: str) -> Path:
    return agent_dir / f"{LABELS[name]}.plist"


def build_plist(
    name: str,
    model: dict,
    model_dir: Path,
    llama_server: str,
    log_dir: Path,
) -> dict:
    """Build a self-contained LaunchAgent using the native server directly."""
    label = LABELS[name]
    command = host_models.build_server_command(
        name, model, model_dir, llama_server=llama_server
    )
    return {
        "Label": label,
        "ProgramArguments": command,
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 5,
        "StandardOutPath": str(log_dir / f"{name}.log"),
        "StandardErrorPath": str(log_dir / f"{name}.error.log"),
        "ProcessType": "Interactive",
    }


def run_launchctl(*arguments: str, check: bool = True) -> subprocess.CompletedProcess:
    completed = subprocess.run(
        ["launchctl", *arguments], capture_output=True, text=True, check=False
    )
    if check and completed.returncode != 0:
        detail = (completed.stderr or completed.stdout).strip()
        raise LaunchdError(f"launchctl {' '.join(arguments)} failed: {detail}")
    return completed


def bootstrap_with_retry(domain: str, path: Path, attempts: int = 20) -> None:
    """Load a just-replaced agent after launchd finishes tearing down its predecessor."""
    last_detail = "unknown launchctl error"
    for attempt in range(attempts):
        completed = run_launchctl("bootstrap", domain, str(path), check=False)
        if completed.returncode == 0:
            return
        last_detail = (completed.stderr or completed.stdout).strip() or last_detail
        if attempt + 1 < attempts:
            time.sleep(0.25)
    raise LaunchdError(f"launchctl bootstrap {domain} {path} failed: {last_detail}")


def validate_prerequisites(args: argparse.Namespace, models: dict) -> str:
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise LaunchdError("host inference LaunchAgents require arm64 macOS")
    server = host_models.resolve_server(args.llama_server)
    if not server:
        raise LaunchdError(f"llama-server executable not found: {args.llama_server}")
    for name, model in models.items():
        valid, reason = host_models.verify_model(args.model_dir / model["filename"], model)
        if not valid:
            raise LaunchdError(f"model {name} is not ready: {reason}")
    return server


def install(args: argparse.Namespace, models: dict) -> None:
    server = validate_prerequisites(args, models)
    args.agent_dir.mkdir(parents=True, exist_ok=True)
    args.log_dir.mkdir(parents=True, exist_ok=True)
    domain = launch_domain()

    for name, model in models.items():
        path = plist_path(args.agent_dir, name)
        temporary = path.with_suffix(".plist.tmp")
        payload = build_plist(name, model, args.model_dir, server, args.log_dir)
        with temporary.open("wb") as handle:
            plistlib.dump(payload, handle, sort_keys=True)
        temporary.chmod(0o644)
        temporary.replace(path)

        run_launchctl("bootout", f"{domain}/{LABELS[name]}", check=False)
        bootstrap_with_retry(domain, path)
        run_launchctl("enable", f"{domain}/{LABELS[name]}")
        run_launchctl("kickstart", "-k", f"{domain}/{LABELS[name]}")
        print(f"installed {LABELS[name]}: {path}")


def uninstall(args: argparse.Namespace, models: dict) -> None:
    domain = launch_domain()
    for name in models:
        run_launchctl("bootout", f"{domain}/{LABELS[name]}", check=False)
        path = plist_path(args.agent_dir, name)
        path.unlink(missing_ok=True)
        print(f"removed {LABELS[name]}")


def status(args: argparse.Namespace, models: dict) -> None:
    domain = launch_domain()
    failed = False
    for name, model in models.items():
        completed = run_launchctl(
            "print", f"{domain}/{LABELS[name]}", check=False
        )
        loaded = completed.returncode == 0
        base_url = f"http://127.0.0.1:{model['port']}"
        healthy = False
        if loaded:
            try:
                healthy = host_models.json_request(f"{base_url}/health").get("status") == "ok"
            except host_models.HostModelError:
                pass
        print(
            f"{name}: loaded={'yes' if loaded else 'no'} "
            f"healthy={'yes' if healthy else 'no'} url={base_url}"
        )
        failed |= not loaded or not healthy
    if failed:
        raise LaunchdError("one or more host inference services are not ready")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["install", "status", "uninstall"])
    parser.add_argument("--manifest", type=Path, default=host_models.DEFAULT_MANIFEST)
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path(os.environ.get("JARVIS_HOST_MODEL_DIR", host_models.DEFAULT_MODEL_DIR)),
    )
    parser.add_argument(
        "--llama-server",
        default=os.environ.get("LLAMA_SERVER", host_models.DEFAULT_LLAMA_SERVER),
    )
    parser.add_argument("--agent-dir", type=Path, default=DEFAULT_AGENT_DIR)
    parser.add_argument("--log-dir", type=Path, default=DEFAULT_LOG_DIR)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    for attribute in ("model_dir", "agent_dir", "log_dir"):
        setattr(args, attribute, getattr(args, attribute).expanduser().resolve())
    try:
        models = host_models.load_manifest(args.manifest)
        {"install": install, "status": status, "uninstall": uninstall}[
            args.command
        ](args, models)
    except (LaunchdError, host_models.HostModelError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
