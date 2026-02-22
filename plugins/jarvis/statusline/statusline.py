#!/usr/bin/env python3
"""Jarvis statusline for Claude Code.

Reads JSON from stdin (piped by Claude Code), enriches with MCP server info
and git status, outputs a two-line ANSI-colored statusline.

Install: copy to ~/.jarvis/statusline.py, then configure in settings.json:
  {"statusLine": {"type": "command", "command": "~/.jarvis/statusline.py"}}

Stdlib-only. No external dependencies.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

CACHE_DIR = Path.home() / ".jarvis" / "cache" / "statusline"
MCP_CACHE_TTL = 120  # seconds
GIT_CACHE_TTL = 10

# ---------------------------------------------------------------------------
# ANSI
# ---------------------------------------------------------------------------

RESET = "\033[0m"
BOLD = "\033[1m"
PURPLE = "\033[35m"
BLUE = "\033[34m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
GRAY = "\033[90m"

# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _ensure_cache_dir():
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass


def _read_cache(name: str, ttl: int):
    try:
        p = CACHE_DIR / name
        if not p.exists():
            return None
        age = time.time() - p.stat().st_mtime
        if age > ttl:
            p.unlink(missing_ok=True)
            return None
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_cache(name: str, data):
    try:
        (CACHE_DIR / name).write_text(json.dumps(data))
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Data collectors
# ---------------------------------------------------------------------------


def _git_info() -> dict:
    cached = _read_cache("git.json", GIT_CACHE_TTL)
    if cached:
        return cached

    env = {**os.environ, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, env=env, timeout=5,
        ).stdout.strip() or "no-git"
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, env=env, timeout=5,
        ).stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        branch, dirty = "no-git", False

    info = {"branch": branch, "dirty": dirty}
    _write_cache("git.json", info)
    return info


def _mcp_info() -> dict:
    cached = _read_cache("mcp.json", MCP_CACHE_TTL)
    if cached:
        return cached

    servers = []
    # Strip CLAUDECODE to avoid "nested session" error
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
    try:
        result = subprocess.run(
            ["claude", "mcp", "list"],
            capture_output=True, text=True, env=env, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().splitlines():
                if line.startswith("Checking") or not line.strip():
                    continue
                match = line.split(":", 1)
                if match:
                    name = match[0].strip()
                    if name:
                        servers.append(name)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    info = {"servers": servers, "count": len(servers)}
    _write_cache("mcp.json", info)
    return info


def _jarvis_health() -> dict:
    """Quick Jarvis MCP server health check (cached)."""
    cached = _read_cache("jarvis.json", MCP_CACHE_TTL)
    if cached:
        return cached

    info = {"ok": False, "version": ""}
    try:
        result = subprocess.run(
            ["curl", "-sf", "--max-time", "2", "http://localhost:8741/health"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            info = {"ok": data.get("status") == "ok", "version": data.get("version", "")}
    except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
        pass

    _write_cache("jarvis.json", info)
    return info


# ---------------------------------------------------------------------------
# Formatters
# ---------------------------------------------------------------------------


def _fmt_model(model) -> str:
    if not model:
        return f"{GRAY}unknown{RESET}"
    if isinstance(model, str):
        name = model
    else:
        name = model.get("display_name") or model.get("id") or str(model)
    lower = name.lower()
    if "opus" in lower:
        return f"{PURPLE}{BOLD}{name}{RESET}"
    if "sonnet" in lower:
        return f"{BLUE}{name}{RESET}"
    if "haiku" in lower:
        return f"{GREEN}{name}{RESET}"
    return f"{CYAN}{name}{RESET}"


def _fmt_cost(cost) -> str:
    if not isinstance(cost, (int, float)) or cost == 0:
        return f"{GRAY}$0.00{RESET}"
    s = f"${cost:.4f}"
    if cost > 10:
        return f"{RED}{BOLD}{s}{RESET}"
    if cost > 1:
        return f"{YELLOW}{s}{RESET}"
    return f"{GREEN}{s}{RESET}"


def _fmt_context(data: dict) -> str:
    pct = 0.0
    ctx = data.get("context_window")
    if isinstance(ctx, dict):
        pct = ctx.get("used_percentage") or 0.0
    if pct == 0:
        return f"{GRAY}0%{RESET}"
    s = f"{pct:.1f}%"
    if pct > 90:
        return f"{RED}{BOLD}{s}{RESET}"
    if pct > 70:
        return f"{YELLOW}{s}{RESET}"
    return f"{GREEN}{s}{RESET}"


def _fmt_duration(ms) -> str:
    if not ms:
        return f"{GRAY}0s{RESET}"
    secs = ms // 1000
    mins = secs // 60
    hrs = mins // 60
    if hrs:
        return f"{CYAN}{hrs}h{mins % 60}m{RESET}"
    if mins:
        return f"{CYAN}{mins}m{secs % 60}s{RESET}"
    return f"{CYAN}{secs}s{RESET}"


def _fmt_lines(added, removed, dirty) -> str:
    parts = []
    if added:
        parts.append(f"{GREEN}+{added}{RESET}")
    if removed:
        parts.append(f"{RED}-{removed}{RESET}")
    if parts:
        return "/".join(parts)
    if dirty:
        return f"{GRAY}~{RESET}"
    return ""


def _fmt_mcp(info: dict) -> str:
    n = info.get("count", 0)
    if n == 0:
        return f"{GRAY}0 MCP{RESET}"
    servers = info.get("servers", [])
    color = YELLOW if n > 3 else GREEN
    if n <= 2:
        names = ", ".join(servers)
    else:
        names = f"{', '.join(servers[:2])} +{n - 2}"
    return f"{color}{n} MCP [{names}]{RESET}"


def _fmt_jarvis(health: dict, full=False) -> str:
    if health.get("ok"):
        v = health.get("version", "")
        if full:
            label = f"\u26a1 JARVIS v{v}" if v else "\u26a1 JARVIS"
            return f"{BOLD}{YELLOW}{label}{RESET}"
        label = f"J:{v}" if v else "J"
        return f"{GREEN}{label}{RESET}"
    if full:
        return ""
    return f"{RED}J:down{RESET}"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def generate(data: dict) -> str:
    _ensure_cache_dir()

    SEP = f" {GRAY}\u2502{RESET} "

    git = _git_info()
    mcp = _mcp_info()
    jarvis = _jarvis_health()

    model = data.get("model")
    cost_obj = data.get("cost", {})
    cost_usd = cost_obj.get("total_cost_usd", 0) if isinstance(cost_obj, dict) else 0
    added = cost_obj.get("total_lines_added", 0) if isinstance(cost_obj, dict) else 0
    removed = cost_obj.get("total_lines_removed", 0) if isinstance(cost_obj, dict) else 0

    cwd = data.get("cwd", "")
    dirname = os.path.basename(cwd) if cwd else "?"

    # Git branch + changes
    dirty_mark = "*" if git.get("dirty") else ""
    branch = f"{BLUE}{git.get('branch', '?')}{dirty_mark}{RESET}"
    lines = _fmt_lines(added, removed, git.get("dirty"))
    git_display = f"{branch} {lines}".rstrip() if lines else branch

    # Build segments
    parts = []

    # Jarvis branding (only when healthy)
    jarvis_label = _fmt_jarvis(jarvis, full=True)
    if jarvis_label:
        parts.append(jarvis_label)

    parts.append(_fmt_model(model))
    parts.append(f"{CYAN}{dirname}{RESET}")
    parts.append(git_display)

    if cost_usd:
        parts.append(_fmt_cost(cost_usd))

    ctx = data.get("context_window")
    ctx_pct = (ctx.get("used_percentage") or 0.0) if isinstance(ctx, dict) else 0.0
    if ctx_pct > 0:
        parts.append(f"ctx {_fmt_context(data)}")

    return SEP.join(parts)


def main():
    try:
        # Demo mode: --demo flag or interactive TTY with no stdin
        if "--demo" in sys.argv or sys.stdin.isatty():
            demo = {
                "model": {"id": "claude-opus-4-6", "display_name": "Opus 4.6"},
                "cwd": os.getcwd(),
                "cost": {
                    "total_cost_usd": 1.23,
                    "total_duration_ms": 95000,
                    "total_lines_added": 42,
                    "total_lines_removed": 7,
                },
                "context_window": {"used_percentage": 34.5},
            }
            print(generate(demo))
            return

        raw = sys.stdin.read()
        data = json.loads(raw) if raw.strip() else {}
        print(generate(data))
    except Exception:
        # Never crash — output something safe
        print(f"{GRAY}jarvis statusline error{RESET}")


if __name__ == "__main__":
    main()
