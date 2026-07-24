#!/usr/bin/env python3
"""Small llama.cpp-compatible model-host stub for Docker smoke tests.

It intentionally implements only Jarvis' retrieval contract.  CI uses it to
prove that the production image starts with host-only inference and that a
real store/retrieve round trip reaches both the embedding and reranking APIs.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import signal
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


EMBEDDING_DIMENSIONS = 384


def _words(value: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", value.lower()))


class _Handler(BaseHTTPRequestHandler):
    server_version = "JarvisModelStub/1.0"

    def log_message(self, format: str, *args) -> None:
        return

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        if self.path == "/health":
            self._send(200, {"status": "ok", "stub": True})
        elif self.path == "/metrics":
            self._send(200, dict(self.server.counts))
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler contract
        try:
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, json.JSONDecodeError):
            self._send(400, {"error": "invalid JSON"})
            return

        if self.path == "/v1/embeddings":
            self.server.counts["embeddings"] += 1
            self._embeddings(payload)
        elif self.path == "/v1/rerank":
            self.server.counts["rerank"] += 1
            self._rerank(payload)
        elif self.path == "/tokenize":
            self.server.counts["tokenize"] += 1
            self._tokenize(payload)
        else:
            self._send(404, {"error": "not found"})

    def _embeddings(self, payload: dict) -> None:
        values = payload.get("input", [])
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
            self._send(400, {"error": "input must be a string or string array"})
            return

        # A normalized deterministic vector is enough for a one-document
        # end-to-end smoke while still exercising Jarvis' strict validation.
        magnitude = 1.0 / math.sqrt(EMBEDDING_DIMENSIONS)
        vector = [magnitude] * EMBEDDING_DIMENSIONS
        self._send(
            200,
            {
                "model": payload.get("model"),
                "data": [
                    {"object": "embedding", "index": index, "embedding": vector}
                    for index, _ in enumerate(values)
                ],
            },
        )

    def _rerank(self, payload: dict) -> None:
        query = payload.get("query", "")
        documents = payload.get("documents", [])
        if not isinstance(query, str) or not isinstance(documents, list):
            self._send(400, {"error": "invalid rerank request"})
            return
        query_words = _words(query)
        results = []
        for index, document in enumerate(documents):
            overlap = len(query_words & _words(str(document)))
            results.append(
                {"index": index, "relevance_score": 10.0 if overlap else -10.0}
            )
        self._send(
            200,
            {"model": payload.get("model"), "results": results},
        )

    def _tokenize(self, payload: dict) -> None:
        content = payload.get("content", payload.get("text", ""))
        if not isinstance(content, str):
            self._send(400, {"error": "content must be a string"})
            return
        if payload.get("with_pieces"):
            tokens = [
                {"id": index, "piece": char}
                for index, char in enumerate(content)
            ]
        else:
            tokens = list(range(len(content)))
        self._send(200, {"tokens": tokens})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--embedding-port", type=int, default=8751)
    parser.add_argument("--reranking-port", type=int, default=8752)
    args = parser.parse_args()

    servers = [
        ThreadingHTTPServer((args.host, args.embedding_port), _Handler),
        ThreadingHTTPServer((args.host, args.reranking_port), _Handler),
    ]
    for server in servers:
        server.counts = {"embeddings": 0, "rerank": 0, "tokenize": 0}
    threads = [
        threading.Thread(target=server.serve_forever, daemon=True)
        for server in servers
    ]
    for thread in threads:
        thread.start()

    stopped = threading.Event()

    def stop(_signum, _frame) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(
        f"model host stub ready on {args.embedding_port}/{args.reranking_port}",
        flush=True,
    )
    stopped.wait()
    for server in servers:
        server.shutdown()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
