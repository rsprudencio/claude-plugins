#!/usr/bin/env python3
"""Manage the isolated native llama.cpp inference proof for Jarvis."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent
DEFAULT_MANIFEST = ROOT / "models.json"
DEFAULT_MODEL_DIR = Path.home() / ".jarvis" / "models" / "llama.cpp"
DEFAULT_LLAMA_SERVER = "llama-server"
DOWNLOAD_CHUNK_SIZE = 1024 * 1024


class HostModelError(RuntimeError):
    """Raised when the host-model proof cannot satisfy its contract."""


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, dict[str, Any]]:
    """Load and minimally validate the pinned model manifest."""
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise HostModelError("unsupported model manifest schema")
    models = payload.get("models")
    if not isinstance(models, dict) or not models:
        raise HostModelError("model manifest contains no models")
    required = {
        "role", "model_id", "source_repo", "source_revision", "filename",
        "quantization", "sha256", "size", "port",
    }
    for name, model in models.items():
        missing = required.difference(model)
        if missing:
            raise HostModelError(
                f"model {name!r} is missing: {', '.join(sorted(missing))}"
            )
    return models


def model_url(model: dict[str, Any]) -> str:
    """Return the immutable Hugging Face URL for a manifest model."""
    return (
        f"https://huggingface.co/{model['source_repo']}/resolve/"
        f"{model['source_revision']}/{model['filename']}"
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(DOWNLOAD_CHUNK_SIZE):
            digest.update(chunk)
    return digest.hexdigest()


def verify_model(path: Path, model: dict[str, Any]) -> tuple[bool, str]:
    """Verify file size and SHA-256 without trusting its filename."""
    if not path.is_file():
        return False, "missing"
    actual_size = path.stat().st_size
    if actual_size != model["size"]:
        return False, f"size {actual_size}, expected {model['size']}"
    actual_hash = sha256_file(path)
    if actual_hash != model["sha256"]:
        return False, f"sha256 {actual_hash}, expected {model['sha256']}"
    return True, "verified"


def fetch_model(model_dir: Path, name: str, model: dict[str, Any]) -> Path:
    """Download one pinned model atomically and verify it before use."""
    destination = model_dir / model["filename"]
    valid, _ = verify_model(destination, model)
    if valid:
        print(f"verified {name}: {destination}")
        return destination

    model_dir.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    temporary.unlink(missing_ok=True)
    request = urllib.request.Request(
        model_url(model),
        headers={"User-Agent": "jarvis-host-inference-proof/1"},
    )
    print(
        f"fetching {name} ({model['quantization']}, "
        f"{model['size'] / 1024 / 1024:.1f} MiB)"
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            with temporary.open("wb") as handle:
                while chunk := response.read(DOWNLOAD_CHUNK_SIZE):
                    handle.write(chunk)
        valid, reason = verify_model(temporary, model)
        if not valid:
            raise HostModelError(f"download verification failed for {name}: {reason}")
        temporary.replace(destination)
    except (OSError, urllib.error.URLError) as exc:
        raise HostModelError(f"could not fetch {name}: {exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    print(f"installed {name}: {destination}")
    return destination


def build_server_command(
    name: str,
    model: dict[str, Any],
    model_dir: Path,
    llama_server: str = DEFAULT_LLAMA_SERVER,
) -> list[str]:
    """Build a foreground llama-server command for one model role."""
    command = [
        llama_server,
        "--model", str(model_dir / model["filename"]),
        "--alias", model["model_id"],
        "--host", "127.0.0.1",
        "--port", str(model["port"]),
        "--ctx-size", "8192",
        "--n-gpu-layers", "99",
        "--parallel", "1",
        # Vault chunks routinely exceed 512 Granite tokens (1,338 observed),
        # one code-heavy chunk reached 4,116, and BGE's tokenizer can expand
        # the same text further. llama.cpp
        # embedding mode requires n_batch == n_ubatch and otherwise silently
        # collapses both to the smaller value, so keep both above the chunker
        # ceiling. One oversized item cancels its entire multi-input request.
        "--batch-size", "8192",
        "--ubatch-size", "8192",
        "--cors-origins", "localhost",
        "--metrics",
        "--embedding",
    ]
    if model["role"] == "embedding":
        command.extend(["--pooling", "cls", "--embd-normalize", "2"])
    elif model["role"] == "reranker":
        command.extend(["--pooling", "rank", "--reranking"])
    else:
        raise HostModelError(f"unsupported model role for {name}: {model['role']}")
    return command


def json_request(url: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    data = None if body is None else json.dumps(body).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=data,
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read())
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise HostModelError(f"request failed for {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise HostModelError(f"expected a JSON object from {url}")
    return payload


def wait_for_health(base_url: str, timeout: float = 60.0) -> None:
    deadline = time.monotonic() + timeout
    last_error = "not ready"
    while time.monotonic() < deadline:
        try:
            payload = json_request(f"{base_url}/health")
            if payload.get("status") == "ok":
                return
            last_error = str(payload)
        except HostModelError as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise HostModelError(f"model server at {base_url} did not become ready: {last_error}")


def smoke_embedding(model: dict[str, Any], base_url: str) -> dict[str, Any]:
    started = time.perf_counter()
    payload = json_request(
        f"{base_url}/v1/embeddings",
        {
            "model": model["model_id"],
            "input": ["Jarvis stores durable memories for semantic retrieval."],
            "encoding_format": "float",
        },
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1:
        raise HostModelError("embedding server returned an invalid data array")
    embedding = data[0].get("embedding") if isinstance(data[0], dict) else None
    dimensions = model.get("dimensions")
    if not isinstance(embedding, list) or len(embedding) != dimensions:
        raise HostModelError(
            f"embedding dimensions were {len(embedding) if isinstance(embedding, list) else None}; "
            f"expected {dimensions}"
        )
    if not all(isinstance(value, (int, float)) and math.isfinite(value)
               for value in embedding):
        raise HostModelError("embedding contains non-finite values")
    norm = math.sqrt(sum(float(value) ** 2 for value in embedding))
    if not 0.99 <= norm <= 1.01:
        raise HostModelError(f"embedding is not L2-normalized: norm={norm}")
    return {"dimensions": dimensions, "norm": norm, "elapsed_ms": elapsed_ms}


def smoke_reranker(model: dict[str, Any], base_url: str) -> dict[str, Any]:
    documents = [
        "The office kitchen has a new coffee machine.",
        "Jarvis uses pgvector to retrieve semantically related memories.",
        "Stockholm is the capital of Sweden.",
    ]
    started = time.perf_counter()
    payload = json_request(
        f"{base_url}/v1/rerank",
        {
            "model": model["model_id"],
            "query": "How does Jarvis find related memories?",
            "documents": documents,
            "top_n": len(documents),
        },
    )
    elapsed_ms = (time.perf_counter() - started) * 1000
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != len(documents):
        raise HostModelError("reranker returned an invalid result set")
    top = results[0]
    if not isinstance(top, dict) or top.get("index") != 1:
        raise HostModelError(f"reranker selected the wrong top document: {top!r}")
    return {
        "top_index": top["index"],
        "top_score": top.get("relevance_score", top.get("score")),
        "elapsed_ms": elapsed_ms,
    }


def resolve_server(binary: str) -> str | None:
    if os.sep in binary:
        path = Path(binary).expanduser()
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which(binary)


def command_fetch(args: argparse.Namespace, models: dict[str, dict[str, Any]]) -> None:
    names = models.keys() if args.model == "all" else [args.model]
    for name in names:
        fetch_model(args.model_dir, name, models[name])


def command_doctor(args: argparse.Namespace, models: dict[str, dict[str, Any]]) -> None:
    failed = False
    is_macos_arm = platform.system() == "Darwin" and platform.machine() == "arm64"
    print(f"platform: {'ok' if is_macos_arm else 'unsupported'} ({platform.system()} {platform.machine()})")
    failed |= not is_macos_arm

    server = resolve_server(args.llama_server)
    print(f"llama-server: {server or 'missing'}")
    failed |= server is None
    if server:
        completed = subprocess.run(
            [server, "--version"], capture_output=True, text=True, check=False,
        )
        version = (completed.stdout or completed.stderr).strip().splitlines()
        if version:
            print(f"llama-server version: {version[0]}")

    for name, model in models.items():
        path = args.model_dir / model["filename"]
        valid, reason = verify_model(path, model)
        print(f"{name}: {'ok' if valid else 'not ready'} ({reason})")
        failed |= not valid
    if failed:
        raise HostModelError("host inference prerequisites are incomplete")


def command_serve(args: argparse.Namespace, models: dict[str, dict[str, Any]]) -> None:
    model = models[args.model]
    server = resolve_server(args.llama_server) or args.llama_server
    command = build_server_command(args.model, model, args.model_dir, server)
    if args.print_command:
        import shlex
        print(shlex.join(command))
        return

    path = args.model_dir / model["filename"]
    valid, reason = verify_model(path, model)
    if not valid:
        raise HostModelError(f"model {args.model} is not ready: {reason}; run fetch first")
    resolved_server = resolve_server(args.llama_server)
    if not resolved_server:
        raise HostModelError(f"llama-server executable not found: {args.llama_server}")
    command[0] = resolved_server
    os.execv(resolved_server, command)


def command_smoke(args: argparse.Namespace, models: dict[str, dict[str, Any]]) -> None:
    embedding_url = args.embedding_url.rstrip("/")
    reranker_url = args.reranker_url.rstrip("/")
    wait_for_health(embedding_url, args.health_timeout)
    wait_for_health(reranker_url, args.health_timeout)
    results = {
        "embedding": smoke_embedding(models["granite_embedding"], embedding_url),
        "reranker": smoke_reranker(models["bge_reranker"], reranker_url),
    }
    print(json.dumps(results, indent=2, sort_keys=True))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument(
        "--model-dir",
        type=Path,
        default=Path(os.environ.get("JARVIS_HOST_MODEL_DIR", DEFAULT_MODEL_DIR)),
    )
    parser.add_argument(
        "--llama-server",
        default=os.environ.get("LLAMA_SERVER", DEFAULT_LLAMA_SERVER),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch = subparsers.add_parser("fetch", help="download and verify pinned GGUF files")
    fetch.add_argument(
        "--model", choices=["all", "granite_embedding", "bge_reranker"], default="all"
    )

    subparsers.add_parser("doctor", help="check host prerequisites without changing them")

    serve = subparsers.add_parser("serve", help="run one llama-server in the foreground")
    serve.add_argument("model", choices=["granite_embedding", "bge_reranker"])
    serve.add_argument("--print-command", action="store_true")

    smoke = subparsers.add_parser("smoke", help="validate both running model endpoints")
    smoke.add_argument("--embedding-url", default="http://127.0.0.1:8751")
    smoke.add_argument("--reranker-url", default="http://127.0.0.1:8752")
    smoke.add_argument("--health-timeout", type=float, default=60.0)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    args.model_dir = args.model_dir.expanduser().resolve()
    try:
        models = load_manifest(args.manifest)
        commands = {
            "fetch": command_fetch,
            "doctor": command_doctor,
            "serve": command_serve,
            "smoke": command_smoke,
        }
        commands[args.command](args, models)
    except HostModelError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
