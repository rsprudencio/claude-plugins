#!/usr/bin/env python3
"""Live long-input smoke for canonical storage and bounded host inference.

Run from ``/app/jarvis-core`` in a healthy Jarvis container. The probe writes
one temporary memory whose only useful signal is beyond the first inference
window, searches for it with a similarly oversized prompt, verifies that the
canonical document was not truncated, and removes the temporary row.
"""

from __future__ import annotations

import json


MEMORY_MARKER = (
    "Approval record: Project Starling will use the deployment codename "
    "heliotrope zephyr meridian for the lunar archive rollout."
)
QUERY_MARKER = (
    "Find the approved deployment codename heliotrope zephyr meridian "
    "for Project Starling's lunar archive rollout."
)


def main() -> int:
    from tools.content import content_delete, content_read, content_write
    from tools.query import query_vault
    from tools.schema import _get_pool
    from tools.schema_registry import rebuild_registry

    # Distinct boilerplate prevents the test from succeeding on matching heads.
    memory_prefix = "archival northwind boilerplate without retrieval value. " * 3000
    query_prefix = "meeting preamble about unrelated scheduling details. " * 2500
    canonical = memory_prefix + "\nTAIL DECISION: " + MEMORY_MARKER
    oversized_query = query_prefix + "\nQUESTION: locate " + QUERY_MARKER

    rebuild_registry()
    stored = content_write(
        canonical,
        "observation",
        source="long-input-smoke",
        importance_score=0.9,
        skip_secret_scan=True,
    )
    assert stored.get("success"), stored
    doc_id = stored["id"]

    try:
        readback = content_read(doc_id)
        assert readback.get("found"), readback
        assert readback["content"] == canonical, "canonical memory was truncated or changed"

        pool = _get_pool()
        with pool.connection() as conn:
            chunk_count = int(
                conn.execute(
                    "SELECT count(*) FROM local.memory_chunks WHERE parent_id = %s",
                    (doc_id,),
                ).fetchone()[0]
            )
        assert chunk_count > 1, f"expected multiple bounded chunks, got {chunk_count}"

        result = query_vault(oversized_query, n_results=20)
        assert result.get("success"), result
        ids = [item["id"] for item in result.get("results", [])]
        assert doc_id in ids, {"expected": doc_id, "returned": ids}
        query_windows = result.get("query_windows", {})
        assert query_windows.get("input", 1) > 1, query_windows

        print(
            json.dumps(
                {
                    "status": "ok",
                    "canonical_characters": len(canonical),
                    "memory_chunks": chunk_count,
                    "query_characters": len(oversized_query),
                    "query_windows": query_windows,
                    "retrieved_rank": ids.index(doc_id) + 1,
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        removed = content_delete(doc_id, hard=True)
        assert removed.get("deleted"), removed


if __name__ == "__main__":
    raise SystemExit(main())
