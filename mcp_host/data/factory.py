"""Backend selection — one place that decides SQLite (dev/test) vs Postgres (production).

`make_backends()` returns (store, tenant_manager) sharing a connection per backend:
  - DATABASE_URL starts with postgres  -> (PgStore, PgTenantManager)  [real schemas + RLS]
  - otherwise                          -> (SqliteStore, SqliteTenantManager)  [file or in-memory]

If Postgres is requested (postgres:// DSN) but the connection fails even after PgStore's
retry/backoff, we do NOT crash the whole app at boot — that turns one bad secret into an
edge-level 500 crash-loop with no readable error. Instead we fall back to SQLite, mark the
store `.backend` as degraded, and let `/health` surface it loudly.

Durability: an unattended customer cron persists the API key it was issued at registration, so
that key MUST survive a redeploy. We therefore default the SQLite path to a file on the
persistent Replit workspace (/home/runner/workspace) whenever one is available, instead of an
ephemeral :memory: database. Only local dev / the test suite (no workspace dir, no explicit
MCP_HOST_DB) land on :memory:. Set MCP_HOST_DB to override the path.

Every returned store carries a `.backend` label string read by /health and the platform-health
provider: "sqlite-file" is durable; "sqlite-memory" RESETS on restart.
"""

from __future__ import annotations

import logging
import os

from mcp_host.data.store import SqliteStore
from mcp_host.data.tenant import SqliteTenantManager, open_tenant_conn

logger = logging.getLogger("mcp-host")

WORKSPACE_DIR = "/home/runner/workspace"


def resolve_sqlite_path(env_var: str, filename: str) -> tuple[str, bool]:
    """Resolve a SQLite location for `env_var`, returning (path, durable).

    Explicit env wins (":memory:" is honored and reported non-durable). Otherwise, in a Replit
    deployment or whenever the persistent workspace dir exists, default to a file under it so
    data survives a redeploy. Falls back to :memory: only when no persistent location exists
    (local dev / tests). The workspace dir is created on open (SqliteStore -> _ensure_parent_dir),
    so a deployment on a fresh VM where the dir does not yet exist won't crash boot.
    """
    explicit = os.environ.get(env_var)
    if explicit:
        return explicit, explicit != ":memory:"
    if os.environ.get("REPLIT_DEPLOYMENT") or os.path.isdir(WORKSPACE_DIR):
        return os.path.join(WORKSPACE_DIR, filename), True
    return ":memory:", False


def _sqlite_backends(durable_label: str, ephemeral_label: str):
    db_path, durable = resolve_sqlite_path("MCP_HOST_DB", "mcp-host.db")
    store = SqliteStore(db_path)
    store.backend = durable_label if durable else ephemeral_label  # type: ignore[attr-defined]
    tenant_path, _ = resolve_sqlite_path("MCP_HOST_TENANT_DB", "mcp-host-tenant.db")
    tenant = SqliteTenantManager(open_tenant_conn(tenant_path))
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
                "SQLite. Storage is durable only if a persistent path is available "
                "(MCP_HOST_DB or the Replit workspace); otherwise data is EPHEMERAL.",
                type(exc).__name__,
            )
            return _sqlite_backends(
                "sqlite-file (postgres unreachable)", "sqlite-memory (postgres unreachable)"
            )
    return _sqlite_backends("sqlite-file", "sqlite-memory")
