"""ChromaDB telemetry: instrumented collection wrapper with JSONL logging.

Wraps chromadb.Collection to transparently log timing, errors, and
operation metadata for every ChromaDB call.  Designed to detect SQLite
corruption, compaction failures, and lock contention early — before they
cascade into container health-check failures.

All telemetry is best-effort: logging failures never propagate to callers.
"""

import asyncio
import json
import logging
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger("jarvis-core")

# ── Module-level health state (read by /health endpoint) ─────────────────────

chromadb_health: dict = {
    "status": "unknown",
    "last_write_ok": None,
    "last_error_class": None,
    "doc_count": None,
    "integrity_check": None,
}


def _get_telemetry_dir() -> str:
    """Resolve telemetry directory, co-located with JARVIS_HOME."""
    from .config import _resolve_jarvis_home

    return str(_resolve_jarvis_home() / "telemetry")


def _get_telemetry_path() -> str:
    """Resolve the JSONL log file path."""
    return os.path.join(_get_telemetry_dir(), "chromadb.jsonl")


# ── Error classification ─────────────────────────────────────────────────────


def classify_error(exc: Exception) -> str:
    """Classify a ChromaDB/SQLite exception into a category.

    Categories:
        sqlite_corrupt  — database disk image is malformed (code 11)
        sqlite_busy     — database is locked (code 5)
        compaction_failure — ChromaDB compaction/metadata segment error
        timeout         — operation timed out
        unknown         — anything else
    """
    msg = str(exc).lower()
    if "database disk image is malformed" in msg or "code: 11" in msg:
        return "sqlite_corrupt"
    if "database is locked" in msg or "code: 5" in msg:
        return "sqlite_busy"
    if "compaction" in msg or "metadata segment" in msg:
        return "compaction_failure"
    if "timeout" in msg:
        return "timeout"
    return "unknown"


# ── JSONL logging ─────────────────────────────────────────────────────────────


def _log_record(
    op: str,
    elapsed_ms: float,
    error: Optional[str],
    error_class: Optional[str],
    n_ids: Optional[int],
) -> None:
    """Append a single JSONL record. Best-effort, never raises."""
    try:
        telemetry_path = _get_telemetry_path()
        os.makedirs(os.path.dirname(telemetry_path), exist_ok=True)

        record: dict = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
            + "Z",
            "op": op,
            "elapsed_ms": round(elapsed_ms, 1),
            "ok": error is None,
        }
        if error is not None:
            record["error_class"] = error_class
            record["error"] = error
        if n_ids is not None:
            record["n_ids"] = n_ids

        with open(telemetry_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception:
        pass  # telemetry must never break the caller


# ── Instrumented Collection wrapper ──────────────────────────────────────────


def _extract_n_ids(kwargs: dict) -> Optional[int]:
    """Extract document count from common ChromaDB kwargs."""
    ids = kwargs.get("ids")
    if ids is not None:
        return len(ids)
    docs = kwargs.get("documents")
    if docs is not None:
        return len(docs)
    return None


class InstrumentedCollection:
    """Transparent wrapper that logs timing/errors for every ChromaDB operation.

    Delegates all attribute access to the underlying collection via __getattr__.
    Instruments write methods (upsert, add, update, delete) and read methods
    (get, query, count, peek) with timing and error classification.
    """

    def __init__(self, collection):
        self._collection = collection

    def __getattr__(self, name):
        return getattr(self._collection, name)

    # ── Write methods ────────────────────────────────────────────────────

    def upsert(self, **kwargs):
        return self._instrumented_call("upsert", self._collection.upsert, kwargs, is_write=True)

    def delete(self, **kwargs):
        return self._instrumented_call("delete", self._collection.delete, kwargs, is_write=True)

    def add(self, **kwargs):
        return self._instrumented_call("add", self._collection.add, kwargs, is_write=True)

    def update(self, **kwargs):
        return self._instrumented_call("update", self._collection.update, kwargs, is_write=True)

    # ── Read methods ─────────────────────────────────────────────────────

    def get(self, **kwargs):
        return self._instrumented_call("get", self._collection.get, kwargs, is_write=False)

    def query(self, **kwargs):
        return self._instrumented_call("query", self._collection.query, kwargs, is_write=False)

    def count(self):
        return self._instrumented_call("count", self._collection.count, {}, is_write=False)

    def peek(self, **kwargs):
        return self._instrumented_call("peek", self._collection.peek, kwargs, is_write=False)

    # ── Instrumentation core ─────────────────────────────────────────────

    def _instrumented_call(self, op: str, fn, kwargs: dict, is_write: bool):
        start = time.monotonic()
        error = None
        error_class = None
        try:
            result = fn(**kwargs) if kwargs else fn()
            if is_write:
                chromadb_health["last_write_ok"] = True
            return result
        except Exception as e:
            error = str(e)
            error_class = classify_error(e)
            if is_write:
                chromadb_health["last_write_ok"] = False
                chromadb_health["last_error_class"] = error_class
                if error_class == "sqlite_corrupt":
                    chromadb_health["status"] = "corrupt"
                elif chromadb_health["status"] != "corrupt":
                    chromadb_health["status"] = "degraded"
            raise
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000
            self._maybe_log(op, elapsed_ms, error, error_class, kwargs, is_write)

    def _maybe_log(
        self,
        op: str,
        elapsed_ms: float,
        error: Optional[str],
        error_class: Optional[str],
        kwargs: dict,
        is_write: bool,
    ) -> None:
        """Log if telemetry config says so. Errors are ALWAYS logged."""
        from .config import get_telemetry_config

        config = get_telemetry_config()
        if not config["enabled"]:
            return

        # Errors are always logged regardless of log_reads/log_writes
        if error is not None:
            _log_record(op, elapsed_ms, error, error_class, _extract_n_ids(kwargs))
            return

        # Successful operations: check config
        if is_write and config["log_writes"]:
            _log_record(op, elapsed_ms, None, None, _extract_n_ids(kwargs))
        elif not is_write and config["log_reads"]:
            _log_record(op, elapsed_ms, None, None, _extract_n_ids(kwargs))


# ── Startup integrity check ──────────────────────────────────────────────────


def check_integrity(db_path: str) -> dict:
    """Run PRAGMA quick_check on ChromaDB's SQLite database.

    Called once on first _get_client() invocation. Logs result to JSONL
    and stderr. Does NOT block startup on corruption — reads may still
    work from cache.

    Returns:
        Dict with 'ok' bool, 'result' string, and optional 'error'.
    """
    result = {"ok": False, "result": "not_checked"}
    sqlite_path = os.path.join(db_path, "chroma.sqlite3")

    if not os.path.exists(sqlite_path):
        result = {"ok": True, "result": "no_database_yet"}
        chromadb_health["integrity_check"] = "no_database_yet"
        _log_record("integrity_check", 0, None, None, None)
        return result

    try:
        conn = sqlite3.connect(sqlite_path)
        try:
            cursor = conn.execute("PRAGMA quick_check")
            rows = cursor.fetchall()
            check_result = rows[0][0] if rows else "unknown"

            if check_result == "ok":
                result = {"ok": True, "result": "ok"}
                chromadb_health["integrity_check"] = "ok"
                logger.info(f"[telemetry] SQLite integrity check: ok ({sqlite_path})")
            else:
                result = {"ok": False, "result": check_result}
                chromadb_health["integrity_check"] = "failed"
                chromadb_health["status"] = "corrupt"
                logger.critical(
                    f"[telemetry] SQLite CORRUPT: {check_result} ({sqlite_path})"
                )
        finally:
            conn.close()
    except Exception as e:
        result = {"ok": False, "result": "error", "error": str(e)}
        chromadb_health["integrity_check"] = "error"
        chromadb_health["status"] = "corrupt"
        logger.error(f"[telemetry] SQLite integrity check failed: {e}")

    # Log to JSONL
    _log_record(
        op="integrity_check",
        elapsed_ms=0,
        error=None if result["ok"] else result.get("error", result["result"]),
        error_class="sqlite_corrupt" if not result["ok"] else None,
        n_ids=None,
    )

    return result


# ── Background health probe ──────────────────────────────────────────────────

_PROBE_STARTUP_DELAY = 30  # seconds before first probe


async def health_probe_loop():
    """Periodic read+write probe to detect ChromaDB corruption early.

    Every probe_interval_seconds (default 300):
    1. count() — read probe
    2. upsert + delete — write probe with a sentinel document

    Results are logged to JSONL and update the module-level chromadb_health
    dict, which the /health endpoint reads.

    Runs as a background asyncio task via get_background_tasks().
    """
    from .config import get_telemetry_config

    await asyncio.sleep(_PROBE_STARTUP_DELAY)

    while True:
        try:
            config = get_telemetry_config()
            if not config["enabled"]:
                await asyncio.sleep(config["probe_interval_seconds"])
                continue

            await asyncio.to_thread(_probe_once)

        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.warning(f"[telemetry] Health probe error: {e}")

        config = get_telemetry_config()
        await asyncio.sleep(config["probe_interval_seconds"])


def _probe_once() -> dict:
    """Execute a single health probe cycle (synchronous).

    Returns probe result dict.
    """
    from .memory import _get_collection
    from .chroma_lock import chroma_write_lock

    result = {"read_ok": False, "write_ok": False}

    # Read probe: count()
    try:
        collection = _get_collection()
        # Access the underlying collection for direct calls (avoid double-logging)
        raw = collection._collection if isinstance(collection, InstrumentedCollection) else collection
        count = raw.count()
        chromadb_health["doc_count"] = count
        result["read_ok"] = True
        result["doc_count"] = count
    except Exception as e:
        error_class = classify_error(e)
        chromadb_health["status"] = "degraded"
        _log_record("probe_read", 0, str(e), error_class, None)
        result["read_error"] = str(e)

    # Write probe: upsert + delete a sentinel
    sentinel_id = "__jarvis_health_probe__"
    try:
        with chroma_write_lock(timeout=5.0):
            raw.upsert(
                ids=[sentinel_id],
                documents=["health probe"],
                metadatas=[{"type": "probe", "namespace": "internal"}],
            )
            raw.delete(ids=[sentinel_id])
        result["write_ok"] = True
        chromadb_health["last_write_ok"] = True
        if chromadb_health["status"] not in ("corrupt",):
            chromadb_health["status"] = "ok"
    except Exception as e:
        error_class = classify_error(e)
        chromadb_health["last_write_ok"] = False
        chromadb_health["last_error_class"] = error_class
        if error_class == "sqlite_corrupt":
            chromadb_health["status"] = "corrupt"
        else:
            chromadb_health["status"] = "degraded"
        _log_record("probe_write", 0, str(e), error_class, None)
        result["write_error"] = str(e)

    # Log successful probe summary
    if result["read_ok"] and result["write_ok"]:
        _log_record("probe_ok", 0, None, None, None)

    return result
