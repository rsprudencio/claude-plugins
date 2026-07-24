#!/usr/bin/env python3
"""Black-box smoke test for every service in the production Jarvis image."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request


def _json_request(url: str, payload: dict | None = None, timeout: float = 10) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json, text/event-stream"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        assert response.status == 200, f"{url} returned {response.status}"
        return json.loads(response.read())


def _wait_for_health(urls: list[str], timeout: float = 45) -> None:
    deadline = time.monotonic() + timeout
    pending = set(urls)
    while pending and time.monotonic() < deadline:
        for url in list(pending):
            try:
                payload = _json_request(url, timeout=2)
                if payload.get("status") == "ok":
                    pending.remove(url)
            except (OSError, urllib.error.URLError, json.JSONDecodeError):
                pass
        if pending:
            time.sleep(1)
    assert not pending, f"services failed health checks: {sorted(pending)}"


def _mcp(url: str, method: str, params: dict | None = None, request_id: int = 1) -> dict:
    response = _json_request(
        url,
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params or {},
        },
    )
    assert response.get("jsonrpc") == "2.0", response
    assert "error" not in response, response
    return response["result"]


def _verify_mcp(url: str, expected_tools: set[str], minimum_count: int) -> None:
    initialized = _mcp(
        url,
        "initialize",
        {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "jarvis-image-smoke", "version": "1"},
        },
    )
    assert "serverInfo" in initialized, initialized
    tools = _mcp(url, "tools/list", request_id=2).get("tools", [])
    names = {tool["name"] for tool in tools}
    assert len(names) >= minimum_count, f"{url}: only {len(names)} tools: {sorted(names)}"
    missing = expected_tools - names
    assert not missing, f"{url}: missing tools: {sorted(missing)}"


def _verify_retrieval_round_trip(core_mcp: str) -> None:
    marker = "jarvis smoke quasar dependency drift contract"
    stored = _mcp(
        core_mcp,
        "tools/call",
        {
            "name": "jarvis_store",
            "arguments": {
                "type": "observation",
                "content": marker,
                "source": "docker-smoke",
                "importance": 0.7,
            },
        },
        request_id=3,
    )
    assert not stored.get("isError", False), stored

    distractor = _mcp(
        core_mcp,
        "tools/call",
        {
            "name": "jarvis_store",
            "arguments": {
                "type": "observation",
                "content": "unrelated cooking recipe and garden notes",
                "source": "docker-smoke",
                "importance": 0.5,
            },
        },
        request_id=4,
    )
    assert not distractor.get("isError", False), distractor

    retrieved = _mcp(
        core_mcp,
        "tools/call",
        {
            "name": "jarvis_retrieve",
            "arguments": {"query": "quasar dependency drift", "n_results": 3},
        },
        request_id=5,
    )
    assert not retrieved.get("isError", False), retrieved
    assert marker in json.dumps(retrieved), retrieved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--core-port", type=int, default=8741)
    parser.add_argument("--todoist-port", type=int, default=8742)
    parser.add_argument("--obsidian-port", type=int, default=8744)
    parser.add_argument("--explorer-port", type=int, default=8750)
    parser.add_argument("--embedding-stub-port", type=int, default=8751)
    parser.add_argument("--reranking-stub-port", type=int, default=8752)
    args = parser.parse_args()

    base = f"http://{args.host}"
    core = f"{base}:{args.core_port}"
    todoist = f"{base}:{args.todoist_port}"
    obsidian = f"{base}:{args.obsidian_port}"
    explorer = f"{base}:{args.explorer_port}"
    _wait_for_health(
        [
            f"{core}/health",
            f"{todoist}/health",
            f"{obsidian}/health",
            f"{explorer}/health",
        ]
    )

    _verify_mcp(
        f"{core}/mcp",
        {"jarvis_store", "jarvis_retrieve", "jarvis_collection_stats"},
        9,
    )
    _verify_mcp(
        f"{todoist}/mcp",
        {"find_tasks", "add_tasks", "complete_tasks", "update_tasks"},
        9,
    )
    _verify_mcp(
        f"{obsidian}/mcp",
        {"obsidian_commit", "obsidian_status", "obsidian_push"},
        9,
    )
    _verify_retrieval_round_trip(f"{core}/mcp")

    embedding_metrics = _json_request(
        f"{base}:{args.embedding_stub_port}/metrics"
    )
    reranking_metrics = _json_request(
        f"{base}:{args.reranking_stub_port}/metrics"
    )
    assert embedding_metrics["embeddings"] >= 3, embedding_metrics
    assert reranking_metrics["rerank"] >= 1, reranking_metrics
    print(
        json.dumps(
            {
                "status": "ok",
                "services": ["core", "todoist", "obsidian", "memory-explorer"],
                "embedding_metrics": embedding_metrics,
                "reranking_metrics": reranking_metrics,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
