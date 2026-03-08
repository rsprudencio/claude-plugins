"""Jarvis Admin — read-only web UI for Jarvis memory stores and sync status.

Standalone FastAPI app. Run with:
    cd apps/memory-explorer
    uv run uvicorn app:app --reload --host 127.0.0.1 --port 8750

Safety:
    - Localhost-only bind (enforced by uvicorn --host 127.0.0.1)
    - Read-only sessions (SET TRANSACTION READ ONLY per query)
    - sql.Identifier() for all dynamic schema/table names
    - Source whitelist (only sources from config are accepted)
    - DSN redaction in logs via redact_dsn()
    - XSS prevention: all user content via textContent in SPA
    - CSP header on SPA route
    - statement_timeout = 10s to prevent runaway queries
"""

from __future__ import annotations

import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Optional

import psycopg_pool
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from pgvector.psycopg import register_vector
from psycopg import sql
from pydantic import BaseModel

# ── Import tools from MCP server via sys.path ──────────────────────────────
# In Docker: tools live at /app/jarvis-core. In local dev: relative path.
_CONTAINER_PATH = Path("/app/jarvis-core")
_DEV_PATH = Path(__file__).resolve().parent.parent.parent / "plugins" / "jarvis" / "mcp-server"
_MCP = _CONTAINER_PATH if _CONTAINER_PATH.exists() else _DEV_PATH
if str(_MCP) not in sys.path:
    sys.path.insert(0, str(_MCP))

from tools.config import get_embedding_config, get_postgres_config, get_sync_config  # noqa: E402
from tools.embedding import get_embedding_service  # noqa: E402
from tools.remote_connection import get_remote_pool  # noqa: E402
from tools.sync_config import redact_dsn  # noqa: E402
from tools.routing import parse_routing_rule  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("memory-explorer")

_SNIPPET_LEN = 300
_MAX_PAGE_SIZE = 100

# ── Module state ────────────────────────────────────────────────────────────
_local_pool: Optional[psycopg_pool.ConnectionPool] = None
_sources: dict[str, dict] = {}


# ── Pool helpers ─────────────────────────────────────────────────────────────

def _make_local_pool() -> psycopg_pool.ConnectionPool:
    url = get_postgres_config()["url"]
    logger.info("Connecting to local PG: %s", redact_dsn(url))
    return psycopg_pool.ConnectionPool(
        conninfo=url,
        min_size=1,
        max_size=5,
        open=True,
        kwargs={"connect_timeout": 5},
        configure=lambda conn: register_vector(conn),
    )


def _probe_source(pool_fn, schema: str, table: str) -> bool:
    """Return True if the table exists and is queryable."""
    try:
        with pool_fn() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute(
                sql.SQL("SELECT 1 FROM {}.{} LIMIT 1").format(
                    sql.Identifier(schema), sql.Identifier(table)
                )
            )
        return True
    except Exception as e:
        logger.warning("Probe failed %s.%s: %s", schema, table, e)
        return False


def _probe_semantic(pool_fn, schema: str, table: str, dims: int) -> bool:
    """Return True if a pgvector cosine query succeeds with current dimensions."""
    dummy = "[" + ",".join(["0.0"] * dims) + "]"
    try:
        with pool_fn() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute(
                sql.SQL(
                    "SELECT 1 FROM {}.{} ORDER BY embedding <=> %s::vector LIMIT 1"
                ).format(sql.Identifier(schema), sql.Identifier(table)),
                [dummy],
            )
        return True
    except Exception as e:
        logger.warning("Semantic probe failed %s.%s: %s", schema, table, e)
        return False


def _discover_sources(local_pool: psycopg_pool.ConnectionPool) -> dict[str, dict]:
    sources: dict[str, dict] = {}
    dims = get_embedding_config()["dimensions"]

    def lconn():
        return local_pool.connection()

    # local.memories
    if _probe_source(lconn, "local", "memories"):
        sem = _probe_semantic(lconn, "local", "memories", dims)
        sources["local"] = {
            "id": "local",
            "label": "Local Memories",
            "type": "local",
            "schema": "local",
            "table": "memories",
            "has_retrieval_count": True,
            "capabilities": ["text", "metadata"] + (["semantic"] if sem else []),
            "metadata_filters": ["category", "scope", "project", "source", "status"],
        }

    # obsidian.documents
    if _probe_source(lconn, "obsidian", "documents"):
        sem = _probe_semantic(lconn, "obsidian", "documents", dims)
        sources["obsidian"] = {
            "id": "obsidian",
            "label": "Obsidian Vault",
            "type": "local",
            "schema": "obsidian",
            "table": "documents",
            "has_retrieval_count": False,
            "capabilities": ["text", "metadata"] + (["semantic"] if sem else []),
            "metadata_filters": ["vault_type", "directory"],
        }

    # Remotes from sync config
    sync_cfg = get_sync_config()
    for rname, rcfg in sync_cfg.get("remotes", {}).items():
        src_id = f"remote:{rname}"
        schema = rcfg.get("schema", rname)
        try:
            pool = get_remote_pool(rname)

            def rconn(p=pool):
                return p.connection()

            ok = _probe_source(rconn, schema, "memory_refs")
            sem = ok and _probe_semantic(rconn, schema, "content", dims)
            caps = (["text", "metadata"] + (["semantic"] if sem else [])) if ok else []
        except Exception as e:
            logger.warning("Remote %r unavailable: %s", rname, e)
            ok, caps = False, []

        display_name = rcfg.get("schema", rname)
        sources[src_id] = {
            "id": src_id,
            "label": f"Remote: {display_name}",
            "type": "remote",
            "remote_name": rname,
            "schema": schema,
            "available": ok,
            "capabilities": caps,
            "metadata_filters": ["category", "scope", "project", "source", "status"],
        }

    return sources


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    global _local_pool, _sources
    _local_pool = _make_local_pool()
    _sources = _discover_sources(_local_pool)
    logger.info("Ready. Sources: %s", list(_sources.keys()))
    yield
    _local_pool.close()


app = FastAPI(title="Jarvis Admin", lifespan=lifespan)

_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline'; "
    "style-src 'self' 'unsafe-inline'; "
    "connect-src 'self';"
)


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def spa():
    return HTMLResponse(content=_HTML, headers={"Content-Security-Policy": _CSP})


@app.get("/api/sources")
async def list_sources():
    out = []
    for s in _sources.values():
        item = {k: v for k, v in s.items() if k != "remote_name"}
        item["sort_options"] = _sort_options_for(s)
        out.append(item)
    return out


@app.get("/api/stats")
async def get_stats(source: Optional[str] = Query(default=None)):
    targets = (
        [_sources[source]] if (source and source in _sources)
        else list(_sources.values())
    )
    loop = asyncio.get_event_loop()
    out = {}
    for src in targets:
        try:
            n = await loop.run_in_executor(None, _count_sync, src)
            out[src["id"]] = {"count": n}
        except Exception as e:
            out[src["id"]] = {"count": None, "error": _safe_err(e)}
    return out


def _count_sync(src: dict) -> int:
    if src["type"] == "local":
        with _local_pool.connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            row = conn.execute(
                sql.SQL("SELECT COUNT(*) FROM {}.{}").format(
                    sql.Identifier(src["schema"]), sql.Identifier(src["table"])
                )
            ).fetchone()
            return row[0] if row else 0
    else:
        pool = get_remote_pool(src["remote_name"])
        with pool.connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            row = conn.execute(
                sql.SQL("SELECT COUNT(*) FROM {}.memory_refs").format(
                    sql.Identifier(src["schema"])
                )
            ).fetchone()
            return row[0] if row else 0


class SearchRequest(BaseModel):
    source: str
    mode: str
    query: str = ""
    filters: dict = {}
    page: int = 0
    page_size: int = 20
    sort_by: str = "date_desc"


# Sort definitions per context: local (single table) vs remote (r/c JOIN aliases)
_SORT_OPTIONS = {
    "date_desc":       {"local": "created_at DESC",                       "remote": "r.created_at DESC",                       "label": "Newest first"},
    "date_asc":        {"local": "created_at ASC",                        "remote": "r.created_at ASC",                        "label": "Oldest first"},
    "updated_desc":    {"local": "updated_at DESC NULLS LAST",            "remote": "r.updated_at DESC NULLS LAST",            "label": "Recently updated"},
    "importance_desc": {"local": "importance_score DESC NULLS LAST",      "remote": "r.importance_score DESC NULLS LAST",      "label": "Most important"},
    "importance_asc":  {"local": "importance_score ASC NULLS LAST",       "remote": "r.importance_score ASC NULLS LAST",       "label": "Least important"},
    "size_desc":       {"local": "LENGTH(document) DESC",                 "remote": "LENGTH(c.content) DESC",                  "label": "Largest first"},
    "size_asc":        {"local": "LENGTH(document) ASC",                  "remote": "LENGTH(c.content) ASC",                   "label": "Smallest first"},
    "retrieval_desc":  {"local": "retrieval_count DESC NULLS LAST",       "remote": "r.retrieval_count DESC NULLS LAST",       "label": "Most retrieved",
                        "sources": {"local", "remote"}},
}


def _sort_options_for(src: dict) -> list[dict]:
    """Return sort options available for a given source."""
    src_type = "obsidian" if src.get("schema") == "obsidian" else src["type"]
    result = []
    for key, opt in _SORT_OPTIONS.items():
        allowed = opt.get("sources")
        if allowed and src_type not in allowed:
            continue
        result.append({"value": key, "label": opt["label"]})
    return result


def _order_sql(sort_by: str, remote: bool = False, has_rc: bool = True) -> sql.SQL:
    """Build ORDER BY SQL fragment. Falls back to date_desc."""
    opt = _SORT_OPTIONS.get(sort_by, _SORT_OPTIONS["date_desc"])
    # Fall back if sort references retrieval_count on a table that lacks it
    if not has_rc and "retrieval" in sort_by:
        opt = _SORT_OPTIONS["date_desc"]
    return sql.SQL(opt["remote"] if remote else opt["local"])


def _local_cols(has_rc: bool) -> str:
    """Build SELECT column list for local tables."""
    base = "id, document, created_at, updated_at, importance_score"
    if has_rc:
        base += ", retrieval_count"
    base += ", LENGTH(document) AS doc_size"
    return base


def _local_sem_cols(has_rc: bool) -> str:
    """Build SELECT column list for local semantic queries (with score)."""
    base = "id, document, created_at, 1 - (embedding <=> %s::vector) AS score, updated_at, importance_score"
    if has_rc:
        base += ", retrieval_count"
    base += ", LENGTH(document) AS doc_size"
    return base


@app.post("/api/search")
async def search(req: SearchRequest):
    if req.source not in _sources:
        raise HTTPException(400, f"Unknown source: {req.source!r}")
    src = _sources[req.source]
    if req.mode not in ("text", "semantic", "metadata"):
        raise HTTPException(400, f"Invalid mode: {req.mode!r}")
    if req.mode not in src.get("capabilities", []):
        raise HTTPException(400, f"Source {req.source!r} does not support {req.mode!r}")
    if not (0 < req.page_size <= _MAX_PAGE_SIZE):
        raise HTTPException(400, "page_size must be between 1 and 100")

    loop = asyncio.get_event_loop()
    try:
        result = await loop.run_in_executor(None, _search_sync, src, req)
    except Exception as e:
        logger.exception("Search error")
        raise HTTPException(500, _safe_err(e))
    return {
        "results": result["rows"],
        "total": result["total"],
        "page": req.page,
        "source": req.source,
        "allowed_filters": src.get("metadata_filters", []),
        "sort_options": _sort_options_for(src),
    }


def _count_where(conn, schema: str, table: str, where_sql, params: list) -> int:
    """Count rows matching a WHERE clause."""
    row = conn.execute(
        sql.SQL("SELECT COUNT(*) FROM {}.{} WHERE {}").format(
            sql.Identifier(schema), sql.Identifier(table), where_sql
        ),
        params,
    ).fetchone()
    return row[0] if row else 0


def _search_sync(src: dict, req: SearchRequest) -> dict:
    offset = req.page * req.page_size
    allowed_filters = set(src.get("metadata_filters", []))
    total = 0

    if src["type"] == "local":
        schema, table = src["schema"], src["table"]
        sch = sql.Identifier(schema)
        tbl = sql.Identifier(table)
        has_rc = src.get("has_retrieval_count", False)
        with _local_pool.connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET statement_timeout = '10000'")

            order = _order_sql(req.sort_by, has_rc=has_rc)

            if req.mode == "text":
                q = req.query.strip()
                if not q:
                    where = sql.SQL("TRUE")
                    wparams: list = []
                else:
                    where = sql.SQL("document ILIKE %s")
                    wparams = [f"%{q}%"]
                total = _count_where(conn, schema, table, where, wparams)
                rows = conn.execute(
                    sql.SQL(
                        "SELECT " + _local_cols(has_rc) + " FROM {}.{}"
                        " WHERE {} ORDER BY {} LIMIT %s OFFSET %s"
                    ).format(sch, tbl, where, order),
                    wparams + [req.page_size, offset],
                ).fetchall()

            elif req.mode == "semantic":
                vec = _vec_str(req.query)
                total = _count_where(conn, schema, table, sql.SQL("TRUE"), [])
                use_sim = req.sort_by in ("similarity", "date_desc")
                sem_order = sql.SQL("embedding <=> %s::vector") if use_sim else order
                sem_params = [vec] if use_sim else []
                rows = conn.execute(
                    sql.SQL(
                        "SELECT " + _local_sem_cols(has_rc)
                        + " FROM {}.{}"
                        " ORDER BY {} LIMIT %s OFFSET %s"
                    ).format(sch, tbl, sem_order),
                    [vec] + sem_params + [req.page_size, offset],
                ).fetchall()

            else:  # metadata
                conds, params = _build_conds(req.filters, allowed_filters)
                where_sql = sql.SQL(" AND ").join(conds)
                total = _count_where(conn, schema, table, where_sql, list(params))
                rows = conn.execute(
                    sql.SQL(
                        "SELECT " + _local_cols(has_rc) + " FROM {}.{}"
                        " WHERE {} ORDER BY {} LIMIT %s OFFSET %s"
                    ).format(sch, tbl, where_sql, order),
                    params + [req.page_size, offset],
                ).fetchall()

    else:  # remote — memory_refs JOIN content
        pool = get_remote_pool(src["remote_name"])
        s = sql.Identifier(src["schema"])
        with pool.connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            conn.execute("SET statement_timeout = '10000'")

            order = _order_sql(req.sort_by, remote=True)

            if req.mode == "text":
                q = req.query.strip()
                if not q:
                    where = sql.SQL("TRUE")
                    wparams = []
                else:
                    where = sql.SQL("c.content ILIKE %s")
                    wparams = [f"%{q}%"]
                row = conn.execute(
                    sql.SQL(
                        "SELECT COUNT(*) FROM {0}.memory_refs r"
                        " JOIN {0}.content c ON r.content_hash = c.hash"
                        " WHERE {1}"
                    ).format(s, where),
                    wparams,
                ).fetchone()
                total = row[0] if row else 0
                rows = conn.execute(
                    sql.SQL(
                        "SELECT r.id, c.content, r.created_at,"
                        " r.updated_at, r.importance_score, r.retrieval_count, LENGTH(c.content) AS doc_size"
                        " FROM {0}.memory_refs r JOIN {0}.content c ON r.content_hash = c.hash"
                        " WHERE {1} ORDER BY {2} LIMIT %s OFFSET %s"
                    ).format(s, where, order),
                    wparams + [req.page_size, offset],
                ).fetchall()

            elif req.mode == "semantic":
                row = conn.execute(
                    sql.SQL("SELECT COUNT(*) FROM {0}.memory_refs").format(s)
                ).fetchone()
                total = row[0] if row else 0
                vec = _vec_str(req.query)
                use_sim = req.sort_by in ("similarity", "date_desc")
                sem_order = sql.SQL("c.embedding <=> %s::vector") if use_sim else order
                sem_params = [vec] if use_sim else []
                rows = conn.execute(
                    sql.SQL(
                        "SELECT r.id, c.content, r.created_at,"
                        " 1 - (c.embedding <=> %s::vector) AS score,"
                        " r.updated_at, r.importance_score, r.retrieval_count, LENGTH(c.content) AS doc_size"
                        " FROM {0}.memory_refs r JOIN {0}.content c ON r.content_hash = c.hash"
                        " ORDER BY {1} LIMIT %s OFFSET %s"
                    ).format(s, sem_order),
                    [vec] + sem_params + [req.page_size, offset],
                ).fetchall()

            else:  # metadata
                conds, params = _build_remote_conds(req.filters, allowed_filters)
                where_sql = sql.SQL(" AND ").join(conds)
                row = conn.execute(
                    sql.SQL(
                        "SELECT COUNT(*) FROM {0}.memory_refs r"
                        " JOIN {0}.content c ON r.content_hash = c.hash"
                        " WHERE {1}"
                    ).format(s, where_sql),
                    list(params),
                ).fetchone()
                total = row[0] if row else 0
                rows = conn.execute(
                    sql.SQL(
                        "SELECT r.id, c.content, r.created_at,"
                        " r.updated_at, r.importance_score, r.retrieval_count, LENGTH(c.content) AS doc_size"
                        " FROM {0}.memory_refs r JOIN {0}.content c ON r.content_hash = c.hash"
                        " WHERE {1} ORDER BY {2} LIMIT %s OFFSET %s"
                    ).format(s, where_sql, order),
                    params + [req.page_size, offset],
                ).fetchall()

    has_rc = src.get("has_retrieval_count", src["type"] == "remote")
    return {"rows": _rows_to_dicts(rows, req.mode, has_rc=has_rc), "total": total}


@app.get("/api/content")
async def get_content(source: str = Query(...), id: str = Query(...)):
    if source not in _sources:
        raise HTTPException(404, "Source not found")
    src = _sources[source]
    loop = asyncio.get_event_loop()
    try:
        item = await loop.run_in_executor(None, _fetch_content_sync, src, id)
    except Exception as e:
        logger.exception("Content fetch error")
        raise HTTPException(500, _safe_err(e))
    if item is None:
        raise HTTPException(404, "Item not found")
    return item


def _fetch_content_sync(src: dict, item_id: str) -> Optional[dict]:
    if src["type"] == "local":
        schema, table = src["schema"], src["table"]
        with _local_pool.connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            cur = conn.execute(
                sql.SQL(
                    "SELECT * FROM {}.{} WHERE id = %s"
                ).format(sql.Identifier(schema), sql.Identifier(table)),
                [item_id],
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [desc.name for desc in cur.description]
            return _row_to_detail(cols, row)
    else:
        pool = get_remote_pool(src["remote_name"])
        s = sql.Identifier(src["schema"])
        with pool.connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            cur = conn.execute(
                sql.SQL(
                    "SELECT r.*, c.content"
                    " FROM {0}.memory_refs r JOIN {0}.content c ON r.content_hash = c.hash"
                    " WHERE r.id = %s"
                ).format(s),
                [item_id],
            )
            row = cur.fetchone()
            if not row:
                return None
            cols = [desc.name for desc in cur.description]
            return _row_to_detail(cols, row)


def _row_to_detail(cols: list[str], row) -> dict:
    """Convert a full row into a detail response with content + all metadata."""
    data = dict(zip(cols, row))
    # Extract primary fields
    content = data.pop("document", None) or data.pop("content", None) or ""
    item_id = data.pop("id", "")
    # Remove binary/large fields not useful in UI
    data.pop("embedding", None)
    # Format timestamps
    created = data.pop("created_at", None)
    updated = data.pop("updated_at", None)
    # Merge JSONB metadata into top-level for flat display
    jsonb_meta = data.pop("metadata", None) or {}
    if isinstance(jsonb_meta, dict):
        for k, v in jsonb_meta.items():
            if k not in data:
                data[k] = v
    # Build clean metadata dict (skip None values)
    metadata = {}
    for k, v in sorted(data.items()):
        if v is not None and v != "" and v != []:
            if hasattr(v, "isoformat"):
                metadata[k] = v.isoformat()
            elif isinstance(v, (list, dict)):
                metadata[k] = v
            else:
                metadata[k] = v
    return {
        "id": item_id,
        "content": content,
        "metadata": metadata,
        "created_at": created.isoformat() if hasattr(created, "isoformat") else created,
        "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else updated,
    }


# ── Helpers ───────────────────────────────────────────────────────────────────

def _vec_str(query: str) -> str:
    """Encode a query string to a pgvector-compatible string literal."""
    vec = get_embedding_service().encode(query)
    return "[" + ",".join(str(x) for x in vec) + "]"


def _build_conds(filters: dict, allowed: set) -> tuple[list, list]:
    """Build WHERE conditions for local tables (bare column names)."""
    conds = [sql.SQL("TRUE")]
    params: list = []
    for col, val in filters.items():
        if col in allowed and isinstance(val, str):
            conds.append(sql.SQL("{} = %s").format(sql.Identifier(col)))
            params.append(val)
    return conds, params


def _build_remote_conds(filters: dict, allowed: set) -> tuple[list, list]:
    """Build WHERE conditions for remote tables (prefixed with r.)."""
    conds = [sql.SQL("TRUE")]
    params: list = []
    for col, val in filters.items():
        if col in allowed and isinstance(val, str):
            conds.append(sql.SQL("r.{} = %s").format(sql.Identifier(col)))
            params.append(val)
    return conds, params


def _rows_to_dicts(rows: list, mode: str, has_rc: bool = True) -> list[dict]:
    out = []
    for row in rows:
        doc = row[1] or ""
        entry: dict = {
            "id": row[0],
            "snippet": doc[:_SNIPPET_LEN] + ("…" if len(doc) > _SNIPPET_LEN else ""),
            "created_at": row[2].isoformat() if row[2] else None,
        }
        if mode == "semantic":
            if len(row) > 3:
                entry["score"] = round(float(row[3]), 4)
            idx = 4
        else:
            idx = 3
        # Common columns: updated_at, importance_score
        if len(row) > idx:
            entry["updated_at"] = row[idx].isoformat() if row[idx] else None
        if len(row) > idx + 1:
            entry["importance_score"] = float(row[idx + 1]) if row[idx + 1] is not None else None
        # retrieval_count only present on some tables
        if has_rc:
            if len(row) > idx + 2:
                entry["retrieval_count"] = float(row[idx + 2]) if row[idx + 2] is not None else None
            size_idx = idx + 3
        else:
            size_idx = idx + 2
        if len(row) > size_idx:
            entry["doc_size"] = int(row[size_idx]) if row[size_idx] is not None else 0
        out.append(entry)
    return out


def _safe_err(e: Exception) -> str:
    """Return a sanitized error message safe for API responses."""
    msg = str(e)
    # Strip any potential DSN/credential leakage
    if "@" in msg and "//" in msg:
        return "Database error (credentials redacted)"
    return msg[:200]


# ── Admin API ────────────────────────────────────────────────────────────────

@app.get("/api/admin")
async def admin_data():
    """Return sync system status for the admin dashboard."""
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, _admin_sync)
    except Exception as e:
        logger.exception("Admin data error")
        raise HTTPException(500, _safe_err(e))
    return data


def _admin_sync() -> dict:
    sync_cfg = get_sync_config()
    enabled = sync_cfg.get("enabled", False)

    # Remotes (no URLs or credentials)
    remotes = []
    for rname, rcfg in sync_cfg.get("remotes", {}).items():
        remote_ok = False
        try:
            pool = get_remote_pool(rname)
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            remote_ok = True
        except Exception:
            pass
        remotes.append({
            "name": rname,
            "schema": rcfg.get("schema", rname),
            "auth_method": rcfg.get("auth_method", "password"),
            "enabled": rcfg.get("enabled", True),
            "connected": remote_ok,
        })

    # Queue stats + DLQ + recent activity from sync_queue
    queue_stats = {}
    dlq_entries = []
    recent_syncs = []
    last_push = None
    last_fail = None

    try:
        with _local_pool.connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")

            # Per-destination status counts
            rows = conn.execute(
                "SELECT destination, status, count(*) FROM local.sync_queue "
                "GROUP BY destination, status ORDER BY destination, status"
            ).fetchall()
            for dest, status, cnt in rows:
                if dest not in queue_stats:
                    queue_stats[dest] = {}
                queue_stats[dest][status] = cnt

            # DLQ entries (limit 50)
            dlq_rows = conn.execute(
                "SELECT id, memory_id, destination, attempts, error, "
                "created_at, last_attempt "
                "FROM local.sync_queue WHERE status = 'dlq' "
                "ORDER BY last_attempt DESC NULLS LAST LIMIT 50"
            ).fetchall()
            for row in dlq_rows:
                dlq_entries.append({
                    "id": row[0],
                    "memory_id": row[1],
                    "destination": row[2],
                    "attempts": row[3],
                    "error": str(row[4])[:200] if row[4] else None,
                    "created_at": row[5].isoformat() if row[5] else None,
                    "last_attempt": row[6].isoformat() if row[6] else None,
                })

            # Last successful sync (most recent 'done' entry)
            done_row = conn.execute(
                "SELECT last_attempt, destination FROM local.sync_queue "
                "WHERE status = 'done' ORDER BY last_attempt DESC NULLS LAST LIMIT 1"
            ).fetchone()
            if done_row and done_row[0]:
                last_push = {
                    "at": done_row[0].isoformat(),
                    "destination": done_row[1],
                }

            # Last failed sync
            fail_row = conn.execute(
                "SELECT last_attempt, destination, error FROM local.sync_queue "
                "WHERE status IN ('dlq', 'pending') AND error IS NOT NULL "
                "ORDER BY last_attempt DESC NULLS LAST LIMIT 1"
            ).fetchone()
            if fail_row and fail_row[0]:
                last_fail = {
                    "at": fail_row[0].isoformat(),
                    "destination": fail_row[1],
                    "error": str(fail_row[2])[:200] if fail_row[2] else None,
                }

            # Recent sync activity (last 20 completed)
            recent_rows = conn.execute(
                "SELECT id, memory_id, destination, status, last_attempt "
                "FROM local.sync_queue ORDER BY last_attempt DESC NULLS LAST LIMIT 20"
            ).fetchall()
            for row in recent_rows:
                recent_syncs.append({
                    "id": row[0],
                    "memory_id": row[1],
                    "destination": row[2],
                    "status": row[3],
                    "at": row[4].isoformat() if row[4] else None,
                })
    except Exception as e:
        logger.warning("Failed to read sync_queue: %s", e)

    # Routing rules
    rules = []
    for raw in sync_cfg.get("rules", []):
        rules.append({
            "name": raw.get("name", "unnamed"),
            "action": raw.get("action", "route-to"),
            "destinations": raw.get("destinations", []),
            "match": raw.get("match", {}),
        })

    # Memory stats
    mem_stats = {}
    try:
        with _local_pool.connection() as conn:
            conn.execute("SET TRANSACTION READ ONLY")
            # Total across all statuses (matches sidebar count)
            total_row = conn.execute(
                "SELECT count(*) FROM local.memories"
            ).fetchone()
            mem_stats["total"] = total_row[0] if total_row else 0
            # By status
            status_rows = conn.execute(
                "SELECT status, count(*) FROM local.memories "
                "GROUP BY status ORDER BY count(*) DESC"
            ).fetchall()
            mem_stats["by_status"] = {r[0]: r[1] for r in status_rows}
            # Active breakdown by category
            rows = conn.execute(
                "SELECT category, count(*) FROM local.memories "
                "WHERE status = 'active' GROUP BY category ORDER BY count(*) DESC"
            ).fetchall()
            mem_stats["by_category"] = {r[0]: r[1] for r in rows}
            mem_stats["total_active"] = sum(r[1] for r in rows)
    except Exception as e:
        logger.warning("Failed to read memory stats: %s", e)

    return {
        "sync": {
            "enabled": enabled,
            "strategy": sync_cfg.get("strategy", "first-match"),
            "worker_interval": sync_cfg.get("worker_interval_seconds", 30),
            "pull_interval": sync_cfg.get("pull_interval_seconds", 300),
        },
        "remotes": remotes,
        "queue": queue_stats,
        "dlq": dlq_entries,
        "last_push": last_push,
        "last_fail": last_fail,
        "recent_activity": recent_syncs,
        "rules": rules,
        "memory_stats": mem_stats,
    }


# ── Inline SPA ────────────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Jarvis Admin</title>
<style>
:root {
  --bg: #0d1117; --surface: #161b22; --surface2: #21262d;
  --border: #30363d; --text: #e6edf3; --muted: #8b949e;
  --accent: #58a6ff; --badge: #1f6feb; --green: #238636; --orange: #f0883e;
  --red: #da3633;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { background: var(--bg); color: var(--text); font: 13px/1.5 ui-monospace, "SF Mono", Consolas, monospace; display: flex; flex-direction: column; height: 100vh; overflow: hidden; }

/* Top tab bar */
#tabbar { display: flex; background: var(--surface); border-bottom: 1px solid var(--border); padding: 0 14px; gap: 0; flex-shrink: 0; align-items: stretch; }
#tabbar .tab-brand { font-size: 12px; font-weight: 700; color: var(--accent); padding: 9px 14px 9px 4px; letter-spacing: 0.5px; display: flex; align-items: center; }
.tab { padding: 9px 16px; font-size: 12px; color: var(--muted); cursor: pointer; border-bottom: 2px solid transparent; transition: all .15s; }
.tab:hover { color: var(--text); }
.tab.active { color: var(--text); border-bottom-color: var(--accent); font-weight: 600; }

/* Main content area below tabs */
#main { flex: 1; display: flex; overflow: hidden; }
#tab-memories { display: flex; flex: 1; overflow: hidden; }
#tab-admin { display: none; flex: 1; overflow-y: auto; padding: 20px 24px; }
#tab-admin.active { display: block; }

/* Left sidebar */
#sidebar { width: 210px; min-width: 210px; background: var(--surface); border-right: 1px solid var(--border); display: flex; flex-direction: column; overflow: hidden; }
#sidebar-header { padding: 14px 12px 8px; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); }
#source-list { flex: 1; overflow-y: auto; padding: 0 6px 8px; }
.src { padding: 7px 8px; border-radius: 6px; cursor: pointer; display: flex; align-items: center; gap: 8px; color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.src:hover { background: var(--surface2); color: var(--text); }
.src.active { background: var(--surface2); color: var(--text); font-weight: 600; }
.dot { width: 7px; height: 7px; min-width: 7px; border-radius: 50%; background: var(--green); }
.dot.remote { background: var(--orange); }
.dot.down { background: var(--muted); }
#stats-section { border-top: 1px solid var(--border); padding: 10px 12px; font-size: 11px; color: var(--muted); }
#stats-section strong { color: var(--text); }

/* Center: search + results */
#center { flex: 1; display: flex; flex-direction: column; min-width: 0; overflow: hidden; }
#topbar { padding: 10px 14px; background: var(--surface); border-bottom: 1px solid var(--border); display: flex; gap: 6px; align-items: center; }
#q { flex: 1; background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 7px 11px; color: var(--text); font: inherit; outline: none; min-width: 0; }
#q:focus { border-color: var(--accent); }
.mbtn { padding: 6px 10px; border-radius: 6px; border: 1px solid var(--border); background: var(--bg); color: var(--muted); cursor: pointer; font: 11px inherit; }
.mbtn:hover { color: var(--text); }
.mbtn.on { background: var(--badge); border-color: var(--badge); color: #fff; }
.mbtn:disabled { opacity: 0.3; cursor: default; }
#go { padding: 7px 14px; background: var(--green); border: none; border-radius: 6px; color: #fff; cursor: pointer; font: 600 12px inherit; }
#go:hover { background: #2ea043; }
#go:disabled { opacity: 0.3; cursor: default; background: var(--muted); }
#sortbar { padding: 6px 14px; background: var(--surface); border-bottom: 1px solid var(--border); display: none; gap: 10px; align-items: center; }
#sortbar.show { display: flex; }
#sortbar label { font-size: 11px; color: var(--muted); }
#sortbar select { background: var(--bg); border: 1px solid var(--border); border-radius: 4px; color: var(--text); padding: 3px 7px; font: 11px inherit; }
#sort-dir { padding: 3px 10px; border-radius: 4px; border: 1px solid var(--border); background: var(--bg); color: var(--accent); cursor: pointer; font: 600 11px inherit; min-width: 50px; }
#sort-dir:hover { border-color: var(--accent); }
#filterbar { padding: 6px 14px; background: var(--surface); border-bottom: 1px solid var(--border); display: none; gap: 10px; align-items: center; flex-wrap: wrap; }
#filterbar.show { display: flex; }
#filterbar label { font-size: 11px; color: var(--muted); }
#filterbar select { background: var(--bg); border: 1px solid var(--border); border-radius: 4px; color: var(--text); padding: 3px 7px; font: 11px inherit; }
#results { flex: 1; overflow-y: auto; padding: 12px 14px; display: flex; flex-direction: column; gap: 7px; }
.card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 11px 14px; cursor: pointer; transition: border-color .15s; }
.card:hover { border-color: #58a6ff44; }
.card.selected { border-color: var(--accent); background: var(--surface2); }
.cid { font-size: 10px; color: var(--muted); margin-bottom: 5px; font-family: inherit; word-break: break-all; }
.csnip { color: var(--text); font-size: 12px; line-height: 1.6; white-space: pre-wrap; word-break: break-word; }
.cmeta { display: flex; gap: 6px; margin-top: 7px; flex-wrap: wrap; }
.badge { font-size: 10px; padding: 2px 6px; border-radius: 4px; background: var(--surface2); color: var(--muted); }
.sbadge { background: #1c2d3f; color: var(--accent); }
.empty { color: var(--muted); text-align: center; padding: 60px 20px; font-size: 13px; }
#statusbar { padding: 5px 14px; border-top: 1px solid var(--border); font-size: 11px; color: var(--muted); min-height: 24px; }
#more-btn { display: block; margin: 4px auto 8px; padding: 7px 20px; background: var(--surface); border: 1px solid var(--border); border-radius: 6px; color: var(--text); cursor: pointer; font: 12px inherit; }
#more-btn:hover { border-color: var(--accent); }

/* Right detail panel */
#detail { width: 380px; min-width: 380px; background: var(--surface); border-left: 1px solid var(--border); display: flex; flex-direction: column; overflow: hidden; }
#detail-header { padding: 12px 14px 8px; font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: var(--muted); display: flex; justify-content: space-between; align-items: center; }
#detail-close { background: none; border: none; color: var(--muted); cursor: pointer; font-size: 16px; padding: 0 4px; }
#detail-close:hover { color: var(--text); }
#detail-body { flex: 1; overflow-y: auto; padding: 0 14px 14px; }
#detail.empty-state #detail-body { display: flex; align-items: center; justify-content: center; }
.detail-section { margin-bottom: 14px; }
.detail-section-title { font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); margin-bottom: 6px; padding-bottom: 4px; border-bottom: 1px solid var(--border); }
.detail-content { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 12px; font-size: 12px; line-height: 1.7; white-space: pre-wrap; word-break: break-word; max-height: 50vh; overflow-y: auto; }
.meta-table { width: 100%; border-collapse: collapse; }
.meta-table tr { border-bottom: 1px solid var(--border); }
.meta-table tr:last-child { border-bottom: none; }
.meta-table td { padding: 5px 0; font-size: 12px; vertical-align: top; }
.meta-key { color: var(--accent); width: 120px; font-weight: 600; padding-right: 10px; }
.meta-val { color: var(--text); word-break: break-all; }
.meta-val.nested { font-size: 11px; color: var(--muted); white-space: pre-wrap; }

/* Admin panel */
.admin-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr)); gap: 16px; margin-bottom: 20px; }
.admin-card { background: var(--surface); border: 1px solid var(--border); border-radius: 8px; padding: 16px; }
.admin-card h3 { font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); margin-bottom: 12px; padding-bottom: 6px; border-bottom: 1px solid var(--border); }
.admin-card .val { font-size: 22px; font-weight: 700; color: var(--text); }
.admin-card .lbl { font-size: 11px; color: var(--muted); margin-top: 2px; }
.kv { display: flex; justify-content: space-between; align-items: center; padding: 5px 0; border-bottom: 1px solid var(--border); font-size: 12px; }
.kv:last-child { border-bottom: none; }
.kv .k { color: var(--muted); }
.kv .v { color: var(--text); font-weight: 600; }
.pill { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 10px; font-weight: 600; }
.pill-green { background: #23863622; color: var(--green); }
.pill-red { background: #da363322; color: var(--red); }
.pill-orange { background: #f0883e22; color: var(--orange); }
.pill-blue { background: #1f6feb22; color: var(--accent); }
.pill-muted { background: var(--surface2); color: var(--muted); }
.admin-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.admin-table th { text-align: left; color: var(--muted); font-size: 10px; text-transform: uppercase; letter-spacing: 0.5px; padding: 6px 8px; border-bottom: 1px solid var(--border); }
.admin-table td { padding: 6px 8px; border-bottom: 1px solid var(--border); color: var(--text); vertical-align: top; }
.admin-table tr:last-child td { border-bottom: none; }
.admin-table .err { font-size: 11px; color: var(--red); max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.admin-section { margin-bottom: 20px; }
.admin-section h2 { font-size: 13px; color: var(--accent); margin-bottom: 10px; padding-bottom: 6px; border-bottom: 1px solid var(--border); }
.stat-row { display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 12px; }
.stat-box { background: var(--bg); border: 1px solid var(--border); border-radius: 6px; padding: 10px 14px; min-width: 100px; text-align: center; }
.stat-box .num { font-size: 20px; font-weight: 700; }
.stat-box .slbl { font-size: 10px; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; }
.match-chip { display: inline-block; background: var(--surface2); border-radius: 4px; padding: 1px 6px; margin: 1px 2px; font-size: 11px; color: var(--muted); }
.ts { font-size: 11px; color: var(--muted); }
</style>
</head>
<body>
<div id="tabbar">
  <div class="tab-brand">JARVIS</div>
  <div class="tab active" data-tab="memories" onclick="switchTab('memories')">Memories</div>
  <div class="tab" data-tab="admin" onclick="switchTab('admin')">Admin</div>
</div>
<div id="main">
<div id="tab-memories">
<div id="sidebar">
  <div id="sidebar-header">Sources</div>
  <div id="source-list"><div class="empty" style="padding:20px 8px">Loading...</div></div>
  <div id="stats-section"></div>
</div>
<div id="center">
  <div id="topbar">
    <input id="q" type="text" placeholder="Search memories..." autocomplete="off">
    <button class="mbtn on" data-mode="text" onclick="setMode('text')">Text</button>
    <button class="mbtn" data-mode="semantic" onclick="setMode('semantic')">Semantic</button>
    <button class="mbtn" data-mode="metadata" onclick="setMode('metadata')">Filter</button>
    <button id="go" onclick="run()">Search</button>
  </div>
  <div id="filterbar">
    <label>Category</label>
    <select id="f-cat">
      <option value="">Any</option>
      <option>observation</option><option>pattern</option><option>learning</option>
      <option>decision</option><option>summary</option><option>code</option>
      <option>relationship</option><option>hint</option><option>plan</option>
      <option>worklog</option><option>memory</option>
    </select>
    <label>Scope</label>
    <select id="f-scope"><option value="">Any</option><option>global</option><option>project</option></select>
    <label>Status</label>
    <select id="f-status"><option value="">Any</option><option>active</option><option>superseded</option><option>deleted</option></select>
  </div>
  <div id="sortbar">
    <label>Sort by</label>
    <select id="sort-field"></select>
    <button id="sort-dir" onclick="toggleSortDir()">DESC</button>
  </div>
  <div id="results"><div class="empty">Select a source and search.</div></div>
  <div id="statusbar"></div>
</div>
<div id="detail" class="empty-state">
  <div id="detail-header">
    <span>Detail</span>
    <button id="detail-close" onclick="closeDetail()">&times;</button>
  </div>
  <div id="detail-body"><span class="empty" style="padding:20px">Click a result to view details</span></div>
</div>
</div><!-- /tab-memories -->
<div id="tab-admin"></div>
</div><!-- /main -->

<script>
let cur = null, mode = 'text', page = 0, allCaps = [], selectedCard = null, curFilters = [];
let cachedResults = [], cachedTotal = 0, sortDir = 'desc';

fetch('/api/sources').then(r => r.json()).then(srcs => {
  const el = document.getElementById('source-list');
  el.innerHTML = '';
  srcs.forEach(s => {
    const d = document.createElement('div');
    d.className = 'src';
    d.dataset.id = s.id;
    const dot = document.createElement('span');
    const caps = s.capabilities || [];
    dot.className = 'dot' + (caps.length === 0 ? ' down' : '');
    const lbl = document.createElement('span');
    lbl.textContent = s.label;
    d.appendChild(dot); d.appendChild(lbl);
    d.onclick = () => pick(s.id, caps, s.metadata_filters || [], s.sort_options || []);
    el.appendChild(d);
  });
  if (srcs.length) pick(srcs[0].id, srcs[0].capabilities || [], srcs[0].metadata_filters || [], srcs[0].sort_options || []);
}).catch(err => {
  document.getElementById('source-list').innerHTML = '<div class="empty" style="color:#ef4444">Failed: ' + esc(String(err)) + '</div>';
});

var sourceLabels = {};
fetch('/api/sources').then(r => r.json()).then(srcs2 => {
  srcs2.forEach(function(s) { sourceLabels[s.id] = s.label; });
  return fetch('/api/stats');
}).then(r => r.json()).then(stats => {
  const el = document.getElementById('stats-section');
  let html = '';
  Object.entries(stats).forEach(([id, s]) => {
    const label = sourceLabels[id] || id.replace('remote:', '');
    const n = s.count !== null ? s.count.toLocaleString() : '?';
    html += '<div style="display:flex;justify-content:space-between;margin-bottom:3px"><span>' + esc(label) + '</span><strong>' + n + '</strong></div>';
  });
  el.innerHTML = html || '<span>No data</span>';
}).catch(() => {});

function pick(id, caps, mfilt, sorts) {
  cur = id;
  allCaps = caps;
  curFilters = mfilt || [];
  document.querySelectorAll('.src').forEach(el => el.classList.toggle('active', el.dataset.id === id));
  ['text','semantic','metadata'].forEach(m => {
    document.querySelector('.mbtn[data-mode="' + m + '"]').disabled = !caps.includes(m);
  });
  // Show/hide filter dropdowns based on source capabilities
  var filterMap = {'f-cat':'category', 'f-scope':'scope', 'f-status':'status'};
  Object.entries(filterMap).forEach(function(e) {
    var sel = document.getElementById(e[0]);
    var show = curFilters.includes(e[1]);
    if (sel) {
      sel.style.display = show ? '' : 'none';
      // Also hide the preceding label sibling
      var lbl = sel.previousElementSibling;
      if (lbl && lbl.tagName === 'LABEL') lbl.style.display = show ? '' : 'none';
    }
  });
  // Populate sort field dropdown
  var sf = document.getElementById('sort-field');
  sf.innerHTML = '';
  (sorts || []).forEach(function(s) {
    var opt = document.createElement('option');
    // Strip direction from value (date_desc -> date) — we control dir separately
    opt.value = s.value; opt.textContent = s.label;
    sf.appendChild(opt);
  });
  sf.onchange = function() { clientSort(); };
  cachedResults = []; cachedTotal = 0;
  document.getElementById('sortbar').classList.remove('show');
  document.getElementById('go').disabled = !caps.length;
  if (!caps.includes(mode) && caps.length) setMode(caps[0]);
  if (!caps.length) {
    document.getElementById('results').innerHTML = '<div class="empty">Source unavailable — cannot connect.</div>';
  } else {
    document.getElementById('results').innerHTML = '<div class="empty">Press Search to see results.</div>';
  }
  document.getElementById('statusbar').textContent = '';
  closeDetail();
}

function setMode(m) {
  mode = m;
  document.querySelectorAll('.mbtn').forEach(b => b.classList.toggle('on', b.dataset.mode === m));
  document.getElementById('filterbar').classList.toggle('show', m === 'metadata');
  // Add/remove similarity option for semantic mode
  var sf = document.getElementById('sort-field');
  var hasSim = sf.querySelector('option[value="similarity"]');
  if (m === 'semantic' && !hasSim) {
    var opt = document.createElement('option');
    opt.value = 'similarity'; opt.textContent = 'Similarity';
    sf.insertBefore(opt, sf.firstChild);
    sf.value = 'similarity';
  } else if (m !== 'semantic' && hasSim) {
    hasSim.remove();
    if (sf.value === 'similarity') sf.value = 'date_desc';
  }
  document.getElementById('q').placeholder =
    m === 'semantic' ? 'Describe what you are looking for...' :
    m === 'metadata' ? 'Optional text filter...' : 'Search memories...';
}

function filters() {
  const f = {};
  const cat = document.getElementById('f-cat').value;
  const scope = document.getElementById('f-scope').value;
  const status = document.getElementById('f-status').value;
  if (cat) f.category = cat;
  if (scope) f.scope = scope;
  if (status) f.status = status;
  return f;
}

function closeDetail() {
  const panel = document.getElementById('detail');
  panel.classList.add('empty-state');
  document.getElementById('detail-body').innerHTML = '<span class="empty" style="padding:20px">Click a result to view details</span>';
  if (selectedCard) { selectedCard.classList.remove('selected'); selectedCard = null; }
}

function showDetail(id) {
  const panel = document.getElementById('detail');
  const body = document.getElementById('detail-body');
  panel.classList.remove('empty-state');
  body.innerHTML = '<div class="empty" style="padding:20px">Loading...</div>';

  fetch('/api/content?source=' + encodeURIComponent(cur) + '&id=' + encodeURIComponent(id))
    .then(r => r.json())
    .then(d => {
      let html = '';

      // ID
      html += '<div class="detail-section"><div class="detail-section-title">ID</div>';
      html += '<div style="font-size:11px;color:var(--muted);word-break:break-all">' + esc(d.id || id) + '</div></div>';

      // Content
      html += '<div class="detail-section"><div class="detail-section-title">Content</div>';
      html += '<div class="detail-content">' + esc(d.content || '(empty)') + '</div></div>';

      // Metadata
      if (d.metadata && typeof d.metadata === 'object') {
        html += '<div class="detail-section"><div class="detail-section-title">Metadata</div>';
        html += '<table class="meta-table">';
        var keys = Object.keys(d.metadata).sort();
        keys.forEach(k => {
          var v = d.metadata[k];
          var isObj = typeof v === 'object' && v !== null;
          html += '<tr><td class="meta-key">' + esc(k) + '</td>';
          html += '<td class="meta-val' + (isObj ? ' nested' : '') + '">' + esc(isObj ? JSON.stringify(v, null, 2) : String(v)) + '</td></tr>';
        });
        html += '</table></div>';
      }

      // Timestamps
      html += '<div class="detail-section"><div class="detail-section-title">Timestamps</div>';
      html += '<table class="meta-table">';
      html += '<tr><td class="meta-key">created_at</td><td class="meta-val">' + esc(d.created_at || 'N/A') + '</td></tr>';
      if (d.updated_at) html += '<tr><td class="meta-key">updated_at</td><td class="meta-val">' + esc(d.updated_at) + '</td></tr>';
      html += '</table></div>';

      body.innerHTML = html;
    })
    .catch(err => {
      body.innerHTML = '<div class="empty" style="color:#ef4444">Failed to load: ' + esc(String(err)) + '</div>';
    });
}

function makeCard(r) {
  var card = document.createElement('div');
  card.className = 'card';
  card.dataset.rid = r.id;
  var cid = document.createElement('div'); cid.className = 'cid'; cid.textContent = r.id;
  var csnip = document.createElement('div'); csnip.className = 'csnip'; csnip.textContent = r.snippet;
  var cmeta = document.createElement('div'); cmeta.className = 'cmeta';
  if (r.created_at) { var b = document.createElement('span'); b.className = 'badge'; b.textContent = r.created_at.slice(0,10); cmeta.appendChild(b); }
  if (r.score !== undefined) { var b2 = document.createElement('span'); b2.className = 'badge sbadge'; b2.textContent = 'sim ' + r.score; cmeta.appendChild(b2); }
  card.onclick = function() {
    if (selectedCard) selectedCard.classList.remove('selected');
    card.classList.add('selected');
    selectedCard = card;
    showDetail(r.id);
  };
  card.append(cid, csnip, cmeta);
  return card;
}

function renderResults() {
  var el = document.getElementById('results');
  el.innerHTML = '';
  selectedCard = null;
  if (!cachedResults.length) {
    el.innerHTML = '<div class="empty">No results found.</div>';
    document.getElementById('statusbar').textContent = '';
    return;
  }
  cachedResults.forEach(function(r) { el.appendChild(makeCard(r)); });
  document.getElementById('statusbar').textContent = 'Showing ' + cachedResults.length + ' of ' + cachedTotal + ' results';
  if (cachedResults.length >= (page + 1) * 100) {
    var btn = document.createElement('button');
    btn.id = 'more-btn'; btn.textContent = 'Load more';
    btn.onclick = function() { page++; run(true); };
    el.appendChild(btn);
  }
}

function getSortKey(r) {
  var field = document.getElementById('sort-field').value;
  if (field === 'similarity') return r.score != null ? r.score : -1;
  if (field === 'date_desc' || field === 'date_asc') return r.created_at || '';
  if (field === 'updated_desc') return r.updated_at || r.created_at || '';
  if (field === 'importance_desc' || field === 'importance_asc') return r.importance_score != null ? r.importance_score : -1;
  if (field === 'size_desc' || field === 'size_asc') return r.doc_size != null ? r.doc_size : 0;
  if (field === 'retrieval_desc') return r.retrieval_count != null ? r.retrieval_count : -1;
  return r.created_at || '';
}

function clientSort() {
  if (!cachedResults.length) return;
  var dir = sortDir === 'asc' ? 1 : -1;
  cachedResults.sort(function(a, b) {
    var ka = getSortKey(a), kb = getSortKey(b);
    if (ka < kb) return -1 * dir;
    if (ka > kb) return 1 * dir;
    return 0;
  });
  renderResults();
}

function toggleSortDir() {
  sortDir = sortDir === 'desc' ? 'asc' : 'desc';
  document.getElementById('sort-dir').textContent = sortDir.toUpperCase();
  clientSort();
}

function run(append) {
  if (!cur) return;
  if (!append) page = 0;
  var q = document.getElementById('q').value;
  var sortVal = document.getElementById('sort-field').value || 'date_desc';
  var serverSort = sortVal;
  // Map field + direction to server sort key
  if (sortVal === 'similarity') serverSort = 'similarity';
  else if (sortVal.indexOf('_') === -1) serverSort = sortVal + '_' + sortDir;
  var body = { source: cur, mode: mode, query: q, filters: filters(), page: page, page_size: 100, sort_by: serverSort };
  document.getElementById('statusbar').textContent = 'Searching...';
  fetch('/api/search', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify(body) })
    .then(r => { if (!r.ok) return r.text().then(t => Promise.reject(t)); return r.json(); })
    .then(data => {
      var res = data.results || [];
      if (append) {
        cachedResults = cachedResults.concat(res);
      } else {
        cachedResults = res;
      }
      cachedTotal = data.total || 0;
      document.getElementById('sortbar').classList.add('show');
      renderResults();
    })
    .catch(err => {
      document.getElementById('statusbar').textContent = 'Error: ' + err;
    });
}

function esc(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

document.getElementById('q').addEventListener('keydown', function(e) { if (e.key === 'Enter') run(); });

/* ── Tab switching + Admin panel ──────────────────────────────── */

function switchTab(tab) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === tab));
  var mem = document.getElementById('tab-memories');
  var adm = document.getElementById('tab-admin');
  if (tab === 'admin') {
    mem.style.display = 'none';
    adm.style.display = 'block';
    adm.classList.add('active');
    loadAdmin();
  } else {
    mem.style.display = 'flex';
    adm.style.display = 'none';
    adm.classList.remove('active');
  }
}

var adminLoaded = false;
function loadAdmin() {
  var el = document.getElementById('tab-admin');
  el.innerHTML = '<div class="empty" style="padding:40px">Loading admin data...</div>';
  fetch('/api/admin').then(r => r.json()).then(renderAdmin).catch(err => {
    el.innerHTML = '<div class="empty" style="color:var(--red)">Failed: ' + esc(String(err)) + '</div>';
  });
}

function renderAdmin(d) {
  var el = document.getElementById('tab-admin');
  var h = '';

  /* ── Overview cards ──────────────────────────────── */
  h += '<div class="admin-grid">';

  // Sync status card
  var syncPill = d.sync.enabled
    ? '<span class="pill pill-green">ENABLED</span>'
    : '<span class="pill pill-red">DISABLED</span>';
  h += '<div class="admin-card"><h3>Sync Engine</h3>';
  h += '<div style="margin-bottom:10px">' + syncPill + '</div>';
  h += '<div class="kv"><span class="k">Strategy</span><span class="v">' + esc(d.sync.strategy) + '</span></div>';
  h += '<div class="kv"><span class="k">Push interval</span><span class="v">' + d.sync.worker_interval + 's</span></div>';
  h += '<div class="kv"><span class="k">Pull interval</span><span class="v">' + d.sync.pull_interval + 's</span></div>';
  h += '</div>';

  // Last activity card
  h += '<div class="admin-card"><h3>Last Activity</h3>';
  if (d.last_push) {
    h += '<div class="kv"><span class="k">Last push</span><span class="v"><span class="pill pill-green">OK</span> ' + fmtTime(d.last_push.at) + '</span></div>';
    h += '<div class="kv"><span class="k">Destination</span><span class="v">' + esc(d.last_push.destination) + '</span></div>';
  } else {
    h += '<div class="kv"><span class="k">Last push</span><span class="v ts">No syncs yet</span></div>';
  }
  if (d.last_fail) {
    h += '<div class="kv"><span class="k">Last failure</span><span class="v"><span class="pill pill-red">FAIL</span> ' + fmtTime(d.last_fail.at) + '</span></div>';
    h += '<div class="kv"><span class="k">Error</span><span class="v" style="font-size:11px;color:var(--red);max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(d.last_fail.error || '') + '</span></div>';
  }
  h += '</div>';

  // Memory stats card
  h += '<div class="admin-card"><h3>Memory Stats</h3>';
  if (d.memory_stats && d.memory_stats.total != null) {
    h += '<div style="margin-bottom:10px"><span class="val">' + d.memory_stats.total.toLocaleString() + '</span> <span class="lbl">total memories</span></div>';
    // Status breakdown
    if (d.memory_stats.by_status) {
      Object.entries(d.memory_stats.by_status).forEach(function(e) {
        var pill = e[0] === 'active' ? 'pill-green' : e[0] === 'deleted' ? 'pill-red' : 'pill-orange';
        h += '<div class="kv"><span class="k"><span class="pill ' + pill + '">' + esc(e[0]) + '</span></span><span class="v">' + e[1] + '</span></div>';
      });
    }
  }
  h += '</div>';

  // Category breakdown card
  h += '<div class="admin-card"><h3>Active by Category</h3>';
  if (d.memory_stats && d.memory_stats.by_category) {
    h += '<div style="margin-bottom:8px"><span class="val">' + (d.memory_stats.total_active || 0).toLocaleString() + '</span> <span class="lbl">active</span></div>';
    Object.entries(d.memory_stats.by_category).forEach(function(e) {
      h += '<div class="kv"><span class="k">' + esc(e[0]) + '</span><span class="v">' + e[1] + '</span></div>';
    });
  }
  h += '</div>';

  h += '</div>'; // admin-grid

  /* ── Remotes ──────────────────────────────── */
  h += '<div class="admin-section"><h2>Remotes</h2>';
  if (d.remotes.length === 0) {
    h += '<div class="empty" style="padding:10px">No remotes configured</div>';
  } else {
    h += '<table class="admin-table"><tr><th>Name</th><th>Schema</th><th>Auth</th><th>Status</th></tr>';
    d.remotes.forEach(function(r) {
      var st = r.connected
        ? '<span class="pill pill-green">Connected</span>'
        : '<span class="pill pill-red">Disconnected</span>';
      if (!r.enabled) st = '<span class="pill pill-muted">Disabled</span>';
      h += '<tr><td><strong>' + esc(r.name) + '</strong></td><td>' + esc(r.schema) + '</td>';
      h += '<td><span class="pill pill-blue">' + esc(r.auth_method) + '</span></td><td>' + st + '</td></tr>';
    });
    h += '</table>';
  }
  h += '</div>';

  /* ── Queue stats ──────────────────────────────── */
  h += '<div class="admin-section"><h2>Sync Queue</h2>';
  var hasQueue = Object.keys(d.queue).length > 0;
  if (!hasQueue) {
    h += '<div class="stat-row">';
    h += '<div class="stat-box"><div class="num" style="color:var(--green)">0</div><div class="slbl">Queue Empty</div></div>';
    h += '</div>';
  } else {
    Object.entries(d.queue).forEach(function(e) {
      var dest = e[0], stats = e[1];
      h += '<div style="margin-bottom:8px"><strong>' + esc(dest) + '</strong></div>';
      h += '<div class="stat-row">';
      ['pending','sending','done','dlq'].forEach(function(s) {
        var n = stats[s] || 0;
        var color = s === 'dlq' && n > 0 ? 'var(--red)' : s === 'done' ? 'var(--green)' : s === 'pending' ? 'var(--orange)' : 'var(--text)';
        h += '<div class="stat-box"><div class="num" style="color:' + color + '">' + n + '</div><div class="slbl">' + s + '</div></div>';
      });
      h += '</div>';
    });
  }
  h += '</div>';

  /* ── DLQ entries ──────────────────────────────── */
  if (d.dlq.length > 0) {
    h += '<div class="admin-section"><h2>Dead Letter Queue (' + d.dlq.length + ')</h2>';
    h += '<table class="admin-table"><tr><th>ID</th><th>Memory</th><th>Dest</th><th>Attempts</th><th>Error</th><th>Last Attempt</th></tr>';
    d.dlq.forEach(function(e) {
      h += '<tr><td>' + e.id + '</td>';
      h += '<td style="font-size:11px;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(e.memory_id || '') + '</td>';
      h += '<td>' + esc(e.destination) + '</td>';
      h += '<td>' + e.attempts + '</td>';
      h += '<td class="err" title="' + esc(e.error || '') + '">' + esc(e.error || '') + '</td>';
      h += '<td class="ts">' + fmtTime(e.last_attempt) + '</td></tr>';
    });
    h += '</table></div>';
  }

  /* ── Routing rules ──────────────────────────────── */
  h += '<div class="admin-section"><h2>Routing Rules</h2>';
  if (d.rules.length === 0) {
    h += '<div class="empty" style="padding:10px">No routing rules configured</div>';
  } else {
    h += '<table class="admin-table"><tr><th>#</th><th>Name</th><th>Action</th><th>Match</th><th>Destinations</th></tr>';
    d.rules.forEach(function(r, i) {
      var actionPill = r.action === 'deny'
        ? '<span class="pill pill-red">' + esc(r.action) + '</span>'
        : '<span class="pill pill-green">' + esc(r.action) + '</span>';
      var matchHtml = '';
      Object.entries(r.match).forEach(function(m) {
        var vals = Array.isArray(m[1]) ? m[1] : [m[1]];
        matchHtml += '<span class="match-chip">' + esc(m[0]) + ': ' + vals.map(esc).join(', ') + '</span> ';
      });
      h += '<tr><td>' + (i + 1) + '</td><td><strong>' + esc(r.name) + '</strong></td>';
      h += '<td>' + actionPill + '</td>';
      h += '<td>' + (matchHtml || '<span class="ts">any</span>') + '</td>';
      h += '<td>' + r.destinations.map(function(d) { return '<span class="match-chip">' + esc(d) + '</span>'; }).join(' ') + '</td></tr>';
    });
    h += '</table>';
  }
  h += '</div>';

  /* ── Recent activity ──────────────────────────────── */
  if (d.recent_activity.length > 0) {
    h += '<div class="admin-section"><h2>Recent Activity</h2>';
    h += '<table class="admin-table"><tr><th>Time</th><th>Memory</th><th>Dest</th><th>Status</th></tr>';
    d.recent_activity.forEach(function(a) {
      var stPill = a.status === 'done' ? '<span class="pill pill-green">done</span>'
        : a.status === 'dlq' ? '<span class="pill pill-red">dlq</span>'
        : a.status === 'pending' ? '<span class="pill pill-orange">pending</span>'
        : '<span class="pill pill-blue">' + esc(a.status) + '</span>';
      h += '<tr><td class="ts">' + fmtTime(a.at) + '</td>';
      h += '<td style="font-size:11px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + esc(a.memory_id || '') + '</td>';
      h += '<td>' + esc(a.destination) + '</td>';
      h += '<td>' + stPill + '</td></tr>';
    });
    h += '</table></div>';
  }

  el.innerHTML = h;
}

function fmtTime(iso) {
  if (!iso) return '<span class="ts">N/A</span>';
  try {
    var d = new Date(iso);
    var now = new Date();
    var diff = (now - d) / 1000;
    if (diff < 60) return Math.floor(diff) + 's ago';
    if (diff < 3600) return Math.floor(diff / 60) + 'm ago';
    if (diff < 86400) return Math.floor(diff / 3600) + 'h ago';
    return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'});
  } catch(e) { return esc(iso); }
}
</script>
</body>
</html>"""
