"""Remote PostgreSQL connection pool management.

Manages connection pools to remote Jarvis instances, supporting three
authentication methods:
- password: Standard password auth with sslmode=verify-full
- iam: AWS Aurora IAM auth (boto3 generate_db_auth_token)
- mtls: Mutual TLS with client certificate

Pools are created lazily on first use and recycled based on max_lifetime
(600s default, critical for IAM token expiry).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

logger = logging.getLogger("jarvis-core")

# Module-level pool storage
_remote_pools: dict[str, object] = {}  # name → ConnectionPool


@dataclass
class RemoteConfig:
    """Configuration for a remote database connection."""
    name: str
    host: str = "localhost"
    port: int = 5432
    database: str = "jarvis"
    user: str = "jarvis"
    password: Optional[str] = None
    auth_method: str = "password"  # "password", "iam", "mtls"
    # URL-based connection (takes precedence over host/port/database/user)
    url: Optional[str] = None
    # SSL/TLS options
    sslmode: str = "verify-full"
    sslrootcert: Optional[str] = None
    # mTLS options
    sslcert: Optional[str] = None
    sslkey: Optional[str] = None
    # IAM options
    region: Optional[str] = None
    # Pool options
    min_size: int = 1
    max_size: int = 3
    max_lifetime: int = 600  # seconds (10 min, < IAM token 15 min TTL)


def _build_conninfo(config: RemoteConfig) -> str:
    """Build a psycopg conninfo string from RemoteConfig.

    When config.url is set, returns it directly (already resolved).
    Otherwise builds from individual fields.

    Handles password resolution for each auth method:
    - password: uses config.password directly
    - iam: generates short-lived token via boto3
    - mtls: no password, uses client cert
    """
    if config.url:
        return config.url

    password = config.password

    if config.auth_method == "iam":
        password = _iam_password(
            host=config.host,
            port=config.port,
            user=config.user,
            region=config.region,
        )
    elif config.auth_method == "mtls":
        password = None  # mTLS uses client cert, no password

    parts = [
        f"host={config.host}",
        f"port={config.port}",
        f"dbname={config.database}",
        f"user={config.user}",
        f"sslmode={config.sslmode}",
    ]

    if password:
        # Escape single quotes in password
        escaped = password.replace("'", "\\'")
        parts.append(f"password='{escaped}'")

    if config.sslrootcert:
        parts.append(f"sslrootcert={config.sslrootcert}")

    if config.auth_method == "mtls":
        if config.sslcert:
            parts.append(f"sslcert={config.sslcert}")
        if config.sslkey:
            parts.append(f"sslkey={config.sslkey}")

    return " ".join(parts)


def _iam_password(
    host: str, port: int, user: str, region: Optional[str] = None
) -> str:
    """Generate an IAM auth token for AWS Aurora/RDS.

    Uses boto3's generate_db_auth_token() which returns a short-lived
    (15-minute) token. Pool max_lifetime should be < 15 min to ensure
    connections are recycled before token expiry.
    """
    try:
        import boto3

        client = boto3.client("rds", region_name=region)
        token = client.generate_db_auth_token(
            DBHostname=host,
            Port=port,
            DBUsername=user,
            Region=region,
        )
        return token
    except ImportError:
        raise RuntimeError(
            "boto3 is required for IAM authentication. "
            "Install with: pip install boto3"
        )
    except Exception as e:
        raise RuntimeError(f"Failed to generate IAM auth token: {e}") from e


def get_remote_pool(name: str):
    """Get or create a connection pool for a named remote.

    Args:
        name: Remote config name (matches sync config key).

    Returns:
        psycopg_pool.ConnectionPool instance.

    Raises:
        KeyError: If remote is not configured.
        RuntimeError: If pool creation fails.
    """
    if name in _remote_pools:
        pool = _remote_pools[name]
        # Check if pool is still open
        try:
            if not getattr(pool, 'closed', True):
                return pool
        except Exception:
            pass

    config = _load_remote_config(name)
    pool = _create_pool(config)
    _remote_pools[name] = pool
    if config.url:
        from .sync_config import redact_dsn
        logger.info("Created remote pool: %s (%s)", name, redact_dsn(config.url))
    else:
        logger.info("Created remote pool: %s (%s@%s:%d/%s)",
                    name, config.user, config.host, config.port, config.database)
    return pool


def _load_remote_config(name: str) -> RemoteConfig:
    """Load remote config from Jarvis config file.

    Reads from memory.sync.remotes.<name> in config.json.
    URL-first: when ``url`` is present, resolve env vars and use it
    as the conninfo directly instead of building from host/port/database.
    """
    from .config import get_sync_config
    from .sync_config import resolve_env_vars

    sync_cfg = get_sync_config()
    remotes = sync_cfg.get("remotes", {})

    if name not in remotes:
        raise KeyError(f"Remote not configured: {name!r}")

    remote = remotes[name]

    # URL takes precedence over individual fields
    resolved_url = None
    raw_url = remote.get("url")
    if raw_url:
        resolved_url = resolve_env_vars(raw_url)

    # Resolve env vars in password (e.g., "$AURORA_PASSWORD" → actual value)
    raw_password = remote.get("password")
    resolved_password = resolve_env_vars(raw_password) if raw_password else None

    return RemoteConfig(
        name=name,
        url=resolved_url,
        host=remote.get("host", "localhost"),
        port=remote.get("port", 5432),
        database=remote.get("database", "jarvis"),
        user=remote.get("user", "jarvis"),
        password=resolved_password,
        auth_method=remote.get("auth_method", "password"),
        sslmode=remote.get("sslmode", "verify-full"),
        sslrootcert=remote.get("sslrootcert"),
        sslcert=remote.get("sslcert"),
        sslkey=remote.get("sslkey"),
        region=remote.get("region"),
        min_size=remote.get("pool_min_size", 1),
        max_size=remote.get("pool_max_size", 3),
        max_lifetime=remote.get("pool_max_lifetime", 600),
    )


def _create_pool(config: RemoteConfig):
    """Create a connection pool for a remote config."""
    import psycopg_pool
    from pgvector.psycopg import register_vector

    conninfo = _build_conninfo(config)

    pool = psycopg_pool.ConnectionPool(
        conninfo=conninfo,
        min_size=config.min_size,
        max_size=config.max_size,
        max_lifetime=config.max_lifetime,
        open=True,
        configure=lambda conn: register_vector(conn),
    )
    return pool


def connect_remote(name: str):
    """Get a connection from a named remote pool.

    Returns a context manager that yields a connection.
    Usage:
        pool = get_remote_pool("work")
        with pool.connection() as conn:
            ...
    """
    pool = get_remote_pool(name)
    return pool.connection()


def close_remote(name: str) -> bool:
    """Close a specific remote pool.

    Returns True if the pool was closed, False if not found.
    """
    pool = _remote_pools.pop(name, None)
    if pool is not None:
        try:
            pool.close()
        except Exception as e:
            logger.warning("Error closing remote pool %s: %s", name, e)
        logger.info("Closed remote pool: %s", name)
        return True
    return False


def close_all_remotes() -> int:
    """Close all remote connection pools.

    Called at server shutdown.

    Returns:
        Number of pools closed.
    """
    count = 0
    for name in list(_remote_pools.keys()):
        if close_remote(name):
            count += 1
    logger.info("Closed %d remote pool(s)", count)
    return count


def list_remotes() -> list[str]:
    """List names of all active remote pools."""
    return list(_remote_pools.keys())
