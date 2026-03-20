#!/usr/bin/env python3
"""Remote memory manager — list, soft-delete, and hard-delete memories on remote schemas.

Usage:
    # List all memories in remote_personio schema
    python scripts/remote-memory-manager.py list

    # Filter by project (metadata JSONB)
    python scripts/remote-memory-manager.py list --project personio-framework

    # Filter by category
    python scripts/remote-memory-manager.py list --category observation

    # Filter by date range
    python scripts/remote-memory-manager.py list --since 2026-03-18

    # Filter by tags (substring match)
    python scripts/remote-memory-manager.py list --tag security

    # Combine filters
    python scripts/remote-memory-manager.py list --project personio-framework --category learning --since 2026-03-01

    # Soft-delete specific IDs
    python scripts/remote-memory-manager.py soft-delete obs::123 obs::456

    # Soft-delete all matching a filter
    python scripts/remote-memory-manager.py soft-delete --project personio-framework --since 2026-03-18

    # Hard-delete (irreversible!)
    python scripts/remote-memory-manager.py hard-delete obs::123

    # Hard-delete matching filter
    python scripts/remote-memory-manager.py hard-delete --project personio-framework --category observation

    # Restore soft-deleted memories
    python scripts/remote-memory-manager.py restore obs::123

    # Show stats
    python scripts/remote-memory-manager.py stats

    # Use a different remote
    python scripts/remote-memory-manager.py --remote work list

Environment:
    Reads connection details from ~/.jarvis/config.json (memory.sync.remotes.<name>).
    Password env vars (e.g. $PG_REMOTE_PASS) are resolved automatically.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_dotenv():
    """Load ~/.jarvis/.env if it exists (simple KEY=VALUE, no shell expansion)."""
    env_path = Path.home() / ".jarvis" / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = val


def load_config():
    config_path = Path.home() / ".jarvis" / "config.json"
    if not config_path.exists():
        print(f"ERROR: {config_path} not found", file=sys.stderr)
        sys.exit(1)
    return json.loads(config_path.read_text())


def resolve_env_vars(value):
    """Replace $VAR with environment variable value."""
    if not value or not isinstance(value, str):
        return value
    return re.sub(r'\$(\w+)', lambda m: os.environ.get(m.group(1), m.group(0)), value)


def get_remote_config(config, remote_name):
    remotes = config.get("memory", {}).get("sync", {}).get("remotes", {})
    if remote_name not in remotes:
        available = ", ".join(remotes.keys()) or "(none)"
        print(f"ERROR: Remote '{remote_name}' not found. Available: {available}", file=sys.stderr)
        sys.exit(1)
    return remotes[remote_name]


def build_conninfo(remote_cfg):
    """Build psycopg connection string from remote config."""
    if remote_cfg.get("url"):
        return resolve_env_vars(remote_cfg["url"])

    host = remote_cfg.get("host", "localhost")
    port = remote_cfg.get("port", 5432)
    database = remote_cfg.get("database", "jarvis")
    user = remote_cfg.get("user", "jarvis")
    password = resolve_env_vars(remote_cfg.get("password", ""))
    sslmode = remote_cfg.get("ssl_mode", "require")

    conninfo = f"host={host} port={port} dbname={database} user={user}"
    if password:
        conninfo += f" password={password}"
    if sslmode:
        conninfo += f" sslmode={sslmode}"
    sslrootcert = remote_cfg.get("sslrootcert")
    if sslrootcert:
        conninfo += f" sslrootcert={resolve_env_vars(sslrootcert)}"
    return conninfo


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def get_connection(conninfo):
    try:
        import psycopg
    except ImportError:
        print("ERROR: psycopg not installed. Run: pip install psycopg[binary]", file=sys.stderr)
        sys.exit(1)
    return psycopg.connect(conninfo, autocommit=False)


def get_schema_table(remote_cfg, conn):
    """Return (schema, read_table, write_table) for the remote.

    On the *remote* database, the schema name is whatever is configured
    (e.g. 'personio'), NOT 'remote_personio' — that's the local mirror name.

    Auto-discovers the primary table: prefers 'memories' (single-table schema),
    falls back to 'active_memories' view + 'memory_refs' write table
    (content-addressable remote schema).
    """
    schema = remote_cfg.get("schema", "public")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = %s AND table_name IN ('memories', 'active_memories', 'memory_refs') "
            "ORDER BY table_name",
            [schema],
        )
        tables = [r[0] for r in cur.fetchall()]

    if "memories" in tables:
        # Single-table schema: read and write same table
        return schema, "memories", "memories"
    elif "active_memories" in tables:
        # Content-addressable schema: read from view, write to memory_refs
        write_table = "memory_refs" if "memory_refs" in tables else "active_memories"
        return schema, "active_memories", write_table
    else:
        print(f"ERROR: No memories/active_memories table found in schema '{schema}'", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Filter builder
# ---------------------------------------------------------------------------

def build_where(args, schema, table):
    """Build WHERE clause from CLI args. Returns (clause, params)."""
    conditions = []
    params = []

    if args.status:
        conditions.append("status = %s")
        params.append(args.status)

    if args.project:
        # Try column first, fall back to JSONB
        conditions.append("(project = %s OR metadata->>'project_dir' = %s)")
        params.extend([args.project, args.project])

    if args.category:
        conditions.append("category = %s")
        params.append(args.category)

    if args.since:
        conditions.append("created_at >= %s")
        params.append(args.since)

    if args.until:
        conditions.append("created_at < %s")
        params.append(args.until)

    if args.tag:
        conditions.append("metadata->>'tags' LIKE %s")
        params.append(f"%{args.tag}%")

    if args.scope:
        conditions.append("scope = %s")
        params.append(args.scope)

    if args.source:
        conditions.append("source LIKE %s")
        params.append(f"%{args.source}%")

    if args.search:
        conditions.append("document ILIKE %s")
        params.append(f"%{args.search}%")

    where = " AND ".join(conditions) if conditions else "1=1"
    return where, params


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def cmd_list(conn, schema, table, args):
    """List memories matching filters."""
    where, params = build_where(args, schema, table)
    status_filter = "" if args.status else "AND status = 'active'"
    if args.status:
        status_filter = ""
    else:
        # Default: show active unless --status specified or --all
        if hasattr(args, 'all') and args.all:
            status_filter = ""
        else:
            status_filter = "AND status = 'active'"

    sql = f"""
        SELECT id, category, scope, project, source,
               importance_score, status,
               LEFT(document, 120) AS preview,
               metadata->>'tags' AS tags,
               metadata->>'project_dir' AS project_dir,
               created_at
        FROM {schema}.{table}
        WHERE {where} {status_filter}
        ORDER BY created_at DESC
        LIMIT %s
    """
    params.append(args.limit)

    with conn.cursor() as cur:
        cur.execute(sql, params)
        cols = [d.name for d in cur.description]
        rows = cur.fetchall()

    if not rows:
        print("No memories found matching filters.")
        return

    # Count total
    count_sql = f"SELECT count(*) FROM {schema}.{table} WHERE {where} {status_filter}"
    with conn.cursor() as cur:
        cur.execute(count_sql, params[:-1])  # exclude LIMIT param
        total = cur.fetchone()[0]

    print(f"\n{'='*100}")
    print(f"  Remote: {schema}.{table}  |  Showing {len(rows)} of {total} matching memories")
    print(f"{'='*100}\n")

    for row in rows:
        r = dict(zip(cols, row))
        status_badge = "" if r["status"] == "active" else f"  [{r['status'].upper()}]"
        project_display = r["project"] or r["project_dir"] or "-"
        preview = (r["preview"] or "").replace("\n", " ").strip()
        if len(preview) > 100:
            preview = preview[:100] + "..."

        print(f"  {r['id']}{status_badge}")
        print(f"    category: {r['category']}  |  scope: {r['scope']}  |  project: {project_display}")
        print(f"    source: {r['source']}  |  importance: {r['importance_score']}")
        print(f"    tags: {r['tags'] or '-'}")
        print(f"    created: {r['created_at']}")
        print(f"    preview: {preview}")
        print()


def cmd_stats(conn, schema, table, args):
    """Show summary stats."""
    sqls = {
        "total": f"SELECT count(*) FROM {schema}.{table}",
        "active": f"SELECT count(*) FROM {schema}.{table} WHERE status = 'active'",
        "deleted": f"SELECT count(*) FROM {schema}.{table} WHERE status = 'deleted'",
        "superseded": f"SELECT count(*) FROM {schema}.{table} WHERE status = 'superseded'",
    }
    counts = {}
    with conn.cursor() as cur:
        for label, sql in sqls.items():
            cur.execute(sql)
            counts[label] = cur.fetchone()[0]

    print(f"\n  {schema}.{table} Stats")
    print(f"  {'─'*40}")
    for label, count in counts.items():
        print(f"    {label:>12}: {count}")

    # By category
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT category, count(*) as cnt
            FROM {schema}.{table}
            WHERE status = 'active'
            GROUP BY category ORDER BY cnt DESC
        """)
        cats = cur.fetchall()

    print(f"\n  By Category (active only)")
    print(f"  {'─'*40}")
    for cat, cnt in cats:
        print(f"    {cat:>20}: {cnt}")

    # By project
    with conn.cursor() as cur:
        cur.execute(f"""
            SELECT COALESCE(project, metadata->>'project_dir', '(none)') AS proj,
                   count(*) as cnt
            FROM {schema}.{table}
            WHERE status = 'active'
            GROUP BY proj ORDER BY cnt DESC
            LIMIT 10
        """)
        projs = cur.fetchall()

    print(f"\n  By Project (active, top 10)")
    print(f"  {'─'*40}")
    for proj, cnt in projs:
        print(f"    {proj:>30}: {cnt}")
    print()


def cmd_soft_delete(conn, schema, table, args):
    """Soft-delete memories (set status='deleted')."""
    ids = args.ids
    if not ids:
        # Use filters to find IDs
        where, params = build_where(args, schema, table)
        sql = f"SELECT id FROM {schema}.{table} WHERE {where} AND status = 'active'"
        with conn.cursor() as cur:
            cur.execute(sql, params)
            ids = [r[0] for r in cur.fetchall()]

    if not ids:
        print("No matching memories to soft-delete.")
        return

    print(f"\nAbout to SOFT-DELETE {len(ids)} memories:")
    for mid in ids[:20]:
        print(f"  - {mid}")
    if len(ids) > 20:
        print(f"  ... and {len(ids) - 20} more")

    if not args.yes:
        confirm = input(f"\nProceed with soft-delete? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            return

    sql = f"""
        UPDATE {schema}.{table}
        SET status = 'deleted', deleted_at = now(), updated_at = now()
        WHERE id = ANY(%s) AND status = 'active'
    """
    with conn.cursor() as cur:
        cur.execute(sql, [ids])
        affected = cur.rowcount
    conn.commit()
    print(f"\nSoft-deleted {affected} memories.")


def cmd_hard_delete(conn, schema, table, args):
    """Hard-delete memories (irreversible!)."""
    ids = args.ids
    if not ids:
        where, params = build_where(args, schema, table)
        # Hard-delete can target any status (including already soft-deleted)
        sql = f"SELECT id, status FROM {schema}.{table} WHERE {where}"
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()
            ids = [r[0] for r in rows]

    if not ids:
        print("No matching memories to hard-delete.")
        return

    print(f"\n{'!'*60}")
    print(f"  HARD-DELETE is IRREVERSIBLE. {len(ids)} memories will be permanently removed.")
    print(f"{'!'*60}\n")
    for mid in ids[:20]:
        print(f"  - {mid}")
    if len(ids) > 20:
        print(f"  ... and {len(ids) - 20} more")

    if not args.yes:
        confirm = input(f"\nType 'DELETE' to confirm: ").strip()
        if confirm != "DELETE":
            print("Aborted.")
            return

    sql = f"DELETE FROM {schema}.{table} WHERE id = ANY(%s)"
    with conn.cursor() as cur:
        cur.execute(sql, [ids])
        affected = cur.rowcount

        # Clean up orphaned content rows (content-addressable schema)
        orphan_sql = f"""
            DELETE FROM {schema}.content c
            WHERE NOT EXISTS (
                SELECT 1 FROM {schema}.memory_refs r WHERE r.content_hash = c.hash
            )
        """
        try:
            cur.execute(orphan_sql)
            orphans = cur.rowcount
        except Exception:
            orphans = 0  # content table may not exist in single-table schema

    conn.commit()
    print(f"\nHard-deleted {affected} memories.")
    if orphans:
        print(f"Cleaned up {orphans} orphaned content rows.")


def cmd_restore(conn, schema, table, args):
    """Restore soft-deleted memories back to active."""
    ids = args.ids
    if not ids:
        where, params = build_where(args, schema, table)
        sql = f"SELECT id FROM {schema}.{table} WHERE {where} AND status = 'deleted'"
        with conn.cursor() as cur:
            cur.execute(sql, params)
            ids = [r[0] for r in cur.fetchall()]

    if not ids:
        print("No soft-deleted memories matching filters.")
        return

    print(f"\nAbout to RESTORE {len(ids)} memories to active:")
    for mid in ids[:20]:
        print(f"  - {mid}")
    if len(ids) > 20:
        print(f"  ... and {len(ids) - 20} more")

    if not args.yes:
        confirm = input(f"\nProceed? (yes/no): ").strip().lower()
        if confirm != "yes":
            print("Aborted.")
            return

    sql = f"""
        UPDATE {schema}.{table}
        SET status = 'active', deleted_at = NULL, updated_at = now()
        WHERE id = ANY(%s) AND status = 'deleted'
    """
    with conn.cursor() as cur:
        cur.execute(sql, [ids])
        affected = cur.rowcount
    conn.commit()
    print(f"\nRestored {affected} memories.")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_filter_args(parser):
    """Add common filter arguments to a subparser."""
    parser.add_argument("--project", help="Filter by project name (column + JSONB fallback)")
    parser.add_argument("--category", help="Filter by category (observation, learning, etc.)")
    parser.add_argument("--scope", help="Filter by scope (global, project)")
    parser.add_argument("--since", help="Filter created_at >= date (YYYY-MM-DD)")
    parser.add_argument("--until", help="Filter created_at < date (YYYY-MM-DD)")
    parser.add_argument("--tag", help="Filter by tag substring")
    parser.add_argument("--source", help="Filter by source substring")
    parser.add_argument("--search", help="Full-text search in document content (ILIKE)")
    parser.add_argument("--status", help="Filter by status (active, deleted, superseded)")


def main():
    parser = argparse.ArgumentParser(
        description="Manage memories on Jarvis remote schemas",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--remote", default="personio",
                        help="Remote name from config (default: personio)")

    subs = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = subs.add_parser("list", aliases=["ls"], help="List memories")
    add_filter_args(p_list)
    p_list.add_argument("--limit", type=int, default=50, help="Max results (default 50)")
    p_list.add_argument("--all", action="store_true", help="Show all statuses")

    # stats
    subs.add_parser("stats", help="Show summary stats")

    # soft-delete
    p_soft = subs.add_parser("soft-delete", aliases=["sd"], help="Soft-delete memories")
    p_soft.add_argument("ids", nargs="*", help="Memory IDs to delete")
    add_filter_args(p_soft)
    p_soft.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")

    # hard-delete
    p_hard = subs.add_parser("hard-delete", aliases=["hd"], help="Hard-delete (IRREVERSIBLE)")
    p_hard.add_argument("ids", nargs="*", help="Memory IDs to delete")
    add_filter_args(p_hard)
    p_hard.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")

    # restore
    p_restore = subs.add_parser("restore", help="Restore soft-deleted memories")
    p_restore.add_argument("ids", nargs="*", help="Memory IDs to restore")
    add_filter_args(p_restore)
    p_restore.add_argument("-y", "--yes", action="store_true", help="Skip confirmation")

    args = parser.parse_args()

    # Load .env for password env vars, then config
    load_dotenv()
    config = load_config()
    remote_cfg = get_remote_config(config, args.remote)
    conninfo = build_conninfo(remote_cfg)

    print(f"Connecting to {args.remote}...")
    conn = get_connection(conninfo)
    schema, table, write_table = get_schema_table(remote_cfg, conn)
    print(f"Using {schema}.{table} (writes → {write_table})")

    try:
        cmd = args.command
        if cmd in ("list", "ls"):
            cmd_list(conn, schema, table, args)
        elif cmd == "stats":
            cmd_stats(conn, schema, table, args)
        elif cmd in ("soft-delete", "sd"):
            cmd_soft_delete(conn, schema, write_table, args)
        elif cmd in ("hard-delete", "hd"):
            cmd_hard_delete(conn, schema, write_table, args)
        elif cmd == "restore":
            cmd_restore(conn, schema, write_table, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
