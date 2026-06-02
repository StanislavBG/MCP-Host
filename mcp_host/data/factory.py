"""Backend selection — one place that decides SQLite (dev/test) vs Postgres (production).

`make_backends()` returns (store, tenant_manager) sharing a connection per backend:
  - DATABASE_URL starts with postgres  -> (PgStore, PgTenantManager)  [real schemas + RLS]
  - otherwise                          -> (SqliteStore, SqliteTenantManager)  [in-memory/file]

If Postgres is requested (postgres:// DSN) but the connection fails even after PgStore's
retry/backoff, we do NOT crash the whole app at boot — that turns one bad secret into an
edge-level 500 crash-loop with no readable error. Instead we fall back to SQLite, mark the
store `.backend` as degraded, and let `/health` surface it loudly. Data on that fallback is
EPHEMERAL; the operator must fix DATABASE_URL for durability.

Every returned store carries a `.backend` label string read by /health and the platform-health
provider.

On Replit, set DATABASE_URL to the Replit production Postgres connection string and the platform
is production-data-ready on first boot with no code change.
"""

from __future__ import annotations

import logging
import os

from mcp_host.data.store import SqliteStore
from mcp_host.data.tenant import SqliteTenantManager, open_tenant_conn

logger = logging.getLogger("mcp-host")


def _sqlite_backends(label: str):
    store = SqliteStore(os.environ.get("MCP_HOST_DB", ":memory:"))
    store.backend = label  # type: ignore[attr-defined]
    tenant = SqliteTenantManager(open_tenant_conn(os.environ.get("MCP_HOST_TENANT_DB", ":memory:")))
    return store, tenant


def make_backends():
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres"):
        from mcp_host.data.pg import PgStore, PgTenantManager

        try:
            store = PgStore(url)
            store.backend = "postgres"  # type: ignore[attr-defined]
            return store, PgTenantManager(store)
        except Exception as exc:  # connect failed after PgStore's retries — degrade, don't crash
            logger.error(
                "[backend] DATABASE_URL is set but Postgres is unreachable (%s). Falling back to "
                "in-memory SQLite — DATA IS EPHEMERAL and resets on restart. Fix DATABASE_URL for "
                "durable storage.", type(exc).__name__,
            )
            return _sqlite_backends("sqlite-memory (postgres unreachable)")
    return _sqlite_backends("sqlite-memory")
