#!/usr/bin/env python3
"""Atomically activate Jarvis host inference settings or show their status."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path


DEFAULT_CONFIG = Path.home() / ".jarvis" / "config.json"
BACKUP_SUFFIX = ".pre-host-inference.bak"


class ConfigurationError(RuntimeError):
    """Raised when Jarvis configuration cannot be changed safely."""


def load_config(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError(f"could not read {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ConfigurationError("Jarvis configuration must be a JSON object")
    return payload


def write_config(path: Path, payload: dict) -> None:
    """Write JSON atomically while preserving the original file mode."""
    mode = path.stat().st_mode & 0o777
    temporary = path.with_name(path.name + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, indent=2, sort_keys=False) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_backup(path: Path) -> Path:
    backup = path.with_name(path.name + BACKUP_SUFFIX)
    if not backup.exists():
        shutil.copy2(path, backup)
    return backup


def activate(payload: dict) -> None:
    memory = payload.setdefault("memory", {})
    memory.update(
        {
            "embedding_backend": "host",
            "embedding_model_id": "ibm-granite/granite-embedding-small-english-r2",
            "embedding_host_model": "ibm-granite/granite-embedding-small-english-r2",
            "embedding_host_url": "http://host.docker.internal:8751",
            "embedding_host_timeout_ms": 2000,
        }
    )
    reranking = memory.setdefault("reranking", {})
    reranking.update(
        {
            "enabled": True,
            "backend": "host",
            "model": "BAAI/bge-reranker-v2-m3",
            "candidate_count": 20,
            "alpha": 0.7,
            "max_latency_ms": 1500,
            "host_url": "http://host.docker.internal:8752",
            "host_timeout_ms": 1500,
        }
    )


def selected_settings(payload: dict) -> dict:
    memory = payload.get("memory", {})
    reranking = memory.get("reranking", {})
    return {
        "embedding_backend": memory.get("embedding_backend"),
        "embedding_model_id": memory.get("embedding_model_id"),
        "embedding_host_model": memory.get("embedding_host_model"),
        "embedding_host_url": memory.get("embedding_host_url"),
        "reranking_enabled": reranking.get("enabled"),
        "reranking_backend": reranking.get("backend"),
        "reranking_model": reranking.get("model"),
        "reranking_candidate_count": reranking.get("candidate_count"),
        "reranking_host_url": reranking.get("host_url"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["activate", "status"])
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    path = args.config.expanduser().resolve()
    try:
        payload = load_config(path)
        if args.command != "status":
            backup = ensure_backup(path)
            activate(payload)
            write_config(path, payload)
            print(f"updated {path}; backup: {backup}")
        print(json.dumps(selected_settings(payload), indent=2, sort_keys=True))
    except ConfigurationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
