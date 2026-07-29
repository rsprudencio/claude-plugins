#!/usr/bin/env python3
"""Generate the per-file contextual summaries that vault retrieval embeds.

THE ONLY summary-generation entry point in the codebase. Indexing, vault writes,
and retrieval read ``obsidian.document_context`` and never write it.

Why out of band
---------------
Generation used to run inline inside ``index_vault`` / ``index_file``, i.e.
inside every vault write. That one choice caused, at once: it could never
succeed in the shipped container (no ``anthropic`` SDK, no ``claude`` CLI, no
key), the per-run spend cap was applied per 10-chunk flush so spend was
unbounded, configured concurrency was scoped to a flush so large vaults
generated serially, and every ``jarvis_store`` write blocked the single-process
MCP event loop on an untimed LLM call (Anthropic SDK defaults: 10 min × 2
retries). Moving generation here makes the cap, the concurrency, and the timeout
all real, and takes the LLM off the request path entirely.

Where to run it
---------------
Wherever an LLM is reachable:

  * HOST (recommended — uses the OAuth ``claude`` CLI, no API key needed)::

        cd plugins/jarvis/mcp-server
        uv run python bin/generate_summaries.py \\
            --vault ~/my-vault \\
            --pg-url 'postgresql://jarvis@localhost:5432/jarvis' \\
            --limit 200 --concurrency 4

  * CONTAINER (needs ANTHROPIC_API_KEY; the image now ships the SDK)::

        docker exec -e ANTHROPIC_API_KEY="$ANTHROPIC_API_KEY" \\
            -w /app/jarvis-core jarvis-jarvis-1 \\
            python bin/generate_summaries.py --limit 200

    The DB URL and vault path resolve from the container's own config exactly as
    the server's do; pass ``--pg-url`` / ``--vault`` if you need to override
    them (e.g. running as a different user against the embedded socket:
    ``--pg-url 'postgresql:///jarvis?host=/var/run/postgresql'``).
    Unlike ``bin/reindex_embeddings.py`` this needs no owner-level table access —
    it only writes ``obsidian.document_context`` — so no ``--user postgres``.

Document text is read from the VAULT, not reassembled from
``obsidian.documents``. The cache key is ``sha256`` of the WHOLE raw file — the
exact hash ``tools/memory.py`` computes when it looks the summary up — so a body
stitched back together from chunks (which have been split, trimmed, and had
headings hoisted) would hash differently and miss forever.

Then re-embed
-------------
Generating a summary does not change any vector. The two-step operator sequence
is::

    bin/generate_summaries.py          # fill the cache
    jarvis_index_vault(force=true)     # embed the summaries

Until step 2 runs, vault vectors stay mechanical (and ``local.meta`` says so).
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time

# Allow imports from the parent mcp-server directory.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logger = logging.getLogger("jarvis-generate-summaries")


def get_connection(pg_url: str):
    import psycopg

    return psycopg.connect(pg_url, autocommit=False)


def list_chunked_files(conn) -> list[str]:
    """Vault files that are genuinely fragmented, i.e. can carry a prefix.

    Whole-document rows already begin with their own title and are never
    augmented, so summarizing them would be pure spend. Reading the file list
    from the INDEX (rather than globbing the vault) also means only files that
    actually made it past secret scanning and skip rules are considered.
    """
    rows = conn.execute(
        """SELECT DISTINCT parent_file FROM obsidian.documents
            WHERE chunk_total > 1 AND parent_file IS NOT NULL AND parent_file <> ''
            ORDER BY parent_file"""
    ).fetchall()
    return [str(row[0]) for row in rows]


def build_requests(vault_path: str, parent_files: list[str], config: dict):
    """Assemble one ``SummaryRequest`` per readable file.

    Returns ``(requests, unreadable)``. A file present in the index but gone
    from disk is reported, not fatal: the next ``index_vault`` will drop its
    rows anyway.
    """
    from tools.context_summary import build_summary_request
    from tools.format_support import detect_format
    from tools.memory import _extract_title_for_file

    requests = []
    unreadable: list[str] = []
    for relative in parent_files:
        absolute = os.path.join(vault_path, relative)
        try:
            with open(absolute, "r", encoding="utf-8") as handle:
                content = handle.read()
        except Exception as exc:
            logger.debug("Skipping %s: %s", relative, exc)
            unreadable.append(relative)
            continue
        if not content.strip():
            unreadable.append(relative)
            continue
        try:
            title = _extract_title_for_file(content, os.path.basename(absolute))
        except Exception:
            title = ""
        requests.append(
            build_summary_request(
                relative,
                content,
                title=title or "",
                fmt=detect_format(relative),
                config=config,
            )
        )
    return requests, unreadable


def resolve_vault_path(explicit: str | None) -> str:
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    from tools.config import get_vault_path

    return get_vault_path()


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--pg-url", help="Override POSTGRES_URL/config")
    parser.add_argument(
        "--vault", help="Vault root (default: JARVIS_VAULT_PATH/config)"
    )
    parser.add_argument(
        "--limit", type=int, default=None,
        help="Hard ceiling on LLM calls for THIS run, applied once to the whole "
             "run (default: memory.chunking.contextual_summaries."
             "max_generations_per_run). Files beyond it keep mechanical "
             "augmentation and are picked up by the next run.",
    )
    parser.add_argument(
        "--concurrency", type=int, default=None,
        help="Parallel generation calls across the WHOLE run "
             "(default: config concurrency)",
    )
    parser.add_argument(
        "--timeout", type=int, default=None,
        help="Per-call timeout in seconds (default: config timeout_seconds)",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Regenerate even for files whose cached content_hash still matches",
    )
    parser.add_argument(
        "--only", action="append", default=None, metavar="PATH",
        help="Restrict to this vault-relative path (repeatable)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what WOULD be generated; make no LLM call and no write",
    )
    parser.add_argument("--verbose", "-v", action="store_true")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 0:
        parser.error("--limit must be >= 0")
    if args.concurrency is not None and args.concurrency < 1:
        parser.error("--concurrency must be >= 1")
    if args.timeout is not None and args.timeout < 1:
        parser.error("--timeout must be >= 1")

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    from tools import conflict, context_summary
    from tools.config import (
        get_contextual_augmentation_mode, get_contextual_summaries_config,
        get_postgres_config,
    )

    config = get_contextual_summaries_config()
    mode = get_contextual_augmentation_mode()
    vault_path = resolve_vault_path(args.vault)
    pg_url = (
        args.pg_url or os.environ.get("POSTGRES_URL")
        or get_postgres_config()["url"]
    )

    if not os.path.isdir(vault_path):
        logger.error("Vault path not found: %s", vault_path)
        return 2
    if mode != "summary":
        # Not an error: an operator may pre-warm the cache before flipping the
        # switch. But it must be visible, because nothing will USE the results.
        logger.warning(
            "Augmentation mode is '%s', not 'summary' — generated summaries will "
            "be cached but not embedded or reranked until "
            "memory.chunking.contextual_summaries.enabled is true (and "
            "contextual_embeddings is on).", mode,
        )

    started = time.perf_counter()
    conn = get_connection(pg_url)
    try:
        parent_files = list_chunked_files(conn)
        conn.commit()
        if args.only:
            wanted = {str(path) for path in args.only}
            parent_files = [f for f in parent_files if f in wanted]
        requests, unreadable = build_requests(vault_path, parent_files, config)

        if args.dry_run:
            cached = context_summary.fetch_summary_rows(
                [request.parent_file for request in requests], conn=conn
            )
            conn.commit()
            valid = [
                request.parent_file for request in requests
                if cached.get(request.parent_file, ("", ""))[1]
                == request.content_hash
                and cached.get(request.parent_file, ("", ""))[0]
            ]
            pending = [
                request.parent_file for request in requests
                if request.parent_file not in set(valid)
            ]
            cap = args.limit if args.limit is not None else int(
                config.get("max_generations_per_run", 500) or 0
            )
            report = {
                "dry_run": True,
                "vault": vault_path,
                "augmentation_mode": mode,
                "llm_available": conflict.haiku_available(),
                "chunked_files": len(parent_files),
                "readable": len(requests),
                "unreadable": len(unreadable),
                "with_valid_summary": 0 if args.force else len(valid),
                "would_generate": min(
                    len(parent_files) if args.force else len(pending),
                    cap if cap > 0 else len(pending),
                ),
                "would_skip_over_limit": max(
                    0,
                    (len(parent_files) if args.force else len(pending))
                    - (cap if cap > 0 else 10**9),
                ),
            }
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0

        if not conflict.haiku_available():
            logger.error(
                "No LLM backend reachable. Provide ANTHROPIC_API_KEY with the "
                "'anthropic' package importable, or a 'claude' binary on PATH. "
                "In Docker: docker exec -e ANTHROPIC_API_KEY=... -w "
                "/app/jarvis-core <container> python bin/generate_summaries.py"
            )
            return 3

        context_summary.reset_unavailable_warning()
        report = context_summary.generate_missing_summaries(
            conn,
            requests,
            config=config,
            limit=args.limit,
            concurrency=args.concurrency,
            timeout=args.timeout,
            force=args.force,
        )
        conn.commit()

        # Coverage over the WHOLE chunked corpus, recomputed from the cache, so
        # the number is what a subsequent reindex will actually achieve rather
        # than an accumulation of this run's optimism.
        cached = context_summary.fetch_summary_rows(
            [request.parent_file for request in requests], conn=conn
        )
        conn.commit()
        with_valid = sum(
            1 for request in requests
            if cached.get(request.parent_file, ("", ""))[0]
            and cached.get(request.parent_file, ("", ""))[1] == request.content_hash
        )
        result = {
            "success": True,
            "vault": vault_path,
            "augmentation_mode": mode,
            "chunked_files": len(parent_files),
            "unreadable_files": len(unreadable),
            "with_valid_summary": with_valid,
            "coverage": (
                round(with_valid / len(requests), 4) if requests else 1.0
            ),
            "duration_seconds": round(time.perf_counter() - started, 3),
            **report.as_dict(),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        if with_valid < len(requests):
            logger.warning(
                "%d of %d chunked files still have no valid summary. Re-run to "
                "continue (a run cap defers the rest); persistent failures are "
                "logged per file above.",
                len(requests) - with_valid, len(requests),
            )
        logger.info(
            "Next step — embed the summaries: jarvis_index_vault(force=true). "
            "Until then vault vectors keep the mechanical prefix."
        )
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
