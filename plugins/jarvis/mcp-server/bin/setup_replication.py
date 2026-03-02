#!/usr/bin/env python3
"""Set up PostgreSQL logical replication for Jarvis.

Standalone CLI script for configuring bidirectional logical replication
between central and local Jarvis instances.

Usage:
    python bin/setup_replication.py --role central --pg-url URL
    python bin/setup_replication.py --role local --pg-url URL --central-url URL
    python bin/setup_replication.py --status --pg-url URL
    python bin/setup_replication.py --teardown --pg-url URL
"""

import argparse
import json
import logging
import os
import sys

# Allow imports from the parent mcp-server directory
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

logger = logging.getLogger("jarvis-replication")


def _detect_pg_version(conn) -> int:
    """Detect the PostgreSQL major version.

    Returns the major version number (e.g. 17, 18).
    PG18+ supports native last_update_wins conflict resolution.
    """
    cur = conn.cursor()
    cur.execute("SHOW server_version")
    version_str = cur.fetchone()[0]
    # version_str is like "17.2" or "18.0"
    major = int(version_str.split(".")[0])
    return major


def _get_connection(pg_url: str):
    """Create a psycopg connection for the given URL."""
    import psycopg
    return psycopg.connect(pg_url, autocommit=True)


def setup_central(pg_url: str, replication_user: str = "jarvis_repl"):
    """Configure this instance as the central replication hub.

    Creates:
    - Replication user (if not exists)
    - Grants on jarvis + jarvis_meta tables
    - Publication for both tables
    """
    conn = _get_connection(pg_url)
    pg_version = _detect_pg_version(conn)
    logger.info("PostgreSQL version: %d", pg_version)

    cur = conn.cursor()

    # Create replication user (idempotent)
    cur.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = %s) THEN
                EXECUTE format('CREATE USER %%I WITH REPLICATION', %s);
            END IF;
        END;
        $$;
    """, (replication_user, replication_user))
    logger.info("Replication user '%s' ensured.", replication_user)

    # Grant permissions
    cur.execute(f"GRANT SELECT ON jarvis, jarvis_meta TO {replication_user}")
    logger.info("Grants applied.")

    # Create publication (idempotent via DO block)
    cur.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'jarvis_pub') THEN
                CREATE PUBLICATION jarvis_pub FOR TABLE jarvis, jarvis_meta;
            END IF;
        END;
        $$;
    """)
    logger.info("Publication 'jarvis_pub' ensured.")

    conn.close()
    logger.info("Central setup complete.")


def setup_local(pg_url: str, central_url: str):
    """Configure this instance as a local node subscribing to central.

    Creates:
    - Local publication (for bidirectional sync)
    - Subscription to central
    """
    conn = _get_connection(pg_url)
    pg_version = _detect_pg_version(conn)
    logger.info("PostgreSQL version: %d", pg_version)

    cur = conn.cursor()

    # Create local publication
    cur.execute("""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_publication WHERE pubname = 'jarvis_local_pub') THEN
                CREATE PUBLICATION jarvis_local_pub FOR TABLE jarvis, jarvis_meta;
            END IF;
        END;
        $$;
    """)
    logger.info("Local publication 'jarvis_local_pub' ensured.")

    # Create subscription to central (not idempotent — check first)
    cur.execute(
        "SELECT 1 FROM pg_subscription WHERE subname = 'central_sub'"
    )
    if cur.fetchone() is None:
        # Build subscription options
        sub_opts = "origin = 'none', copy_data = true, streaming = 'parallel'"
        cur.execute(f"""
            CREATE SUBSCRIPTION central_sub
                CONNECTION '{central_url}'
                PUBLICATION jarvis_pub
                WITH ({sub_opts})
        """)
        logger.info("Subscription 'central_sub' created.")
    else:
        logger.info("Subscription 'central_sub' already exists — skipping.")

    conn.close()
    logger.info("Local setup complete.")


def show_status(pg_url: str):
    """Display replication status for this instance."""
    conn = _get_connection(pg_url)
    pg_version = _detect_pg_version(conn)
    cur = conn.cursor()

    status = {"pg_version": pg_version}

    # Publications
    cur.execute("SELECT pubname, puballtables FROM pg_publication")
    pubs = cur.fetchall()
    status["publications"] = [
        {"name": p[0], "all_tables": p[1]} for p in pubs
    ]

    # Subscriptions
    cur.execute("SELECT subname, subenabled, subconninfo FROM pg_subscription")
    subs = cur.fetchall()
    status["subscriptions"] = [
        {
            "name": s[0],
            "enabled": s[1],
            "connection": s[2].split("@")[-1] if "@" in s[2] else s[2],
        }
        for s in subs
    ]

    # Subscription stats
    try:
        cur.execute(
            "SELECT subname, received_lsn, last_msg_send_time "
            "FROM pg_stat_subscription"
        )
        stats = cur.fetchall()
        status["subscription_stats"] = [
            {
                "name": s[0],
                "received_lsn": str(s[1]) if s[1] else None,
                "last_msg": str(s[2]) if s[2] else None,
            }
            for s in stats
        ]
    except Exception:
        status["subscription_stats"] = []

    # Replication slots
    cur.execute(
        "SELECT slot_name, slot_type, active FROM pg_replication_slots"
    )
    slots = cur.fetchall()
    status["replication_slots"] = [
        {"name": s[0], "type": s[1], "active": s[2]} for s in slots
    ]

    conn.close()

    print(json.dumps(status, indent=2, default=str))


def teardown(pg_url: str):
    """Remove replication configuration from this instance."""
    conn = _get_connection(pg_url)
    cur = conn.cursor()

    # Drop subscriptions
    cur.execute("SELECT subname FROM pg_subscription")
    for row in cur.fetchall():
        cur.execute(f"DROP SUBSCRIPTION {row[0]}")
        logger.info("Dropped subscription '%s'.", row[0])

    # Drop publications
    cur.execute("SELECT pubname FROM pg_publication")
    for row in cur.fetchall():
        cur.execute(f"DROP PUBLICATION {row[0]}")
        logger.info("Dropped publication '%s'.", row[0])

    conn.close()
    logger.info("Teardown complete.")


def main():
    parser = argparse.ArgumentParser(
        description="Set up PostgreSQL logical replication for Jarvis."
    )
    parser.add_argument(
        "--pg-url",
        default=os.environ.get("POSTGRES_URL", "postgresql://jarvis:jarvis@localhost:5432/jarvis"),
        help="PostgreSQL connection URL for this instance",
    )
    parser.add_argument(
        "--role",
        choices=["central", "local"],
        help="Set up as central hub or local node",
    )
    parser.add_argument(
        "--central-url",
        help="Connection URL for the central instance (required with --role local)",
    )
    parser.add_argument(
        "--replication-user",
        default="jarvis_repl",
        help="Replication user name (default: jarvis_repl)",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Show replication status",
    )
    parser.add_argument(
        "--teardown",
        action="store_true",
        help="Remove replication configuration",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable verbose logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    if args.status:
        show_status(args.pg_url)
    elif args.teardown:
        teardown(args.pg_url)
    elif args.role == "central":
        setup_central(args.pg_url, args.replication_user)
    elif args.role == "local":
        if not args.central_url:
            parser.error("--central-url is required with --role local")
        setup_local(args.pg_url, args.central_url)
    else:
        parser.error("Specify --role, --status, or --teardown")


if __name__ == "__main__":
    main()
