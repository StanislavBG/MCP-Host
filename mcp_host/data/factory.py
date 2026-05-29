"""Backend selection — one place that decides SQLite (dev/test) vs Postgres (production).

`make_backends()` returns (store, tenant_manager) sharing a connection per backend:
  - DATABASE_URL starts with postgres  -> (PgStore, PgTenantManager)  [real schemas + RLS]
  - otherwise                          -> (SqliteStore, SqliteTenantManager)  [in-memory/file]

On Replit, set DATABASE_URL to the Replit/Neon Postgres connection string and the platform is
production-data-ready on first boot with no code change.
"""

from __future__ import annotations

import os

from mcp_host.data.store import SqliteStore
from mcp_host.data.tenant import SqliteTenantManager, open_tenant_conn


def make_backends():
    url = os.environ.get("DATABASE_URL", "")
    if url.startswith("postgres"):
        from mcp_host.data.pg import PgStore, PgTenantManager

        store = PgStore(url)
        return store, PgTenantManager(store)
    store = SqliteStore(os.environ.get("MCP_HOST_DB", ":memory:"))
    return store, SqliteTenantManager(open_tenant_conn(os.environ.get("MCP_HOST_TENANT_DB", ":memory:")))
