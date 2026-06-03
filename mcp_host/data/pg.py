"""Production Postgres backend — same interface as SqliteStore/TenantDB, real schemas + RLS.

Activated automatically when DATABASE_URL starts with `postgres`. Control-plane tables live in
the `platform` schema; each provider gets its own `<schema>` with Row-Level Security so a
provider can only see rows where tenant_id = current_setting('app.tenant_id'). The gateway sets
that GUC per request via PgTenantManager.handle().

Concurrency: a single connection guarded by one lock (fine for the single Reserved VM model).
Every tenant operation sets app.tenant_id under the lock immediately before its statement, so
the shared connection can never serve one provider's query under another's tenant id. Swap in
psycopg_pool later for parallelism without changing callers.

psycopg is imported lazily so the module loads even where psycopg isn't installed; SQL/DDL
builders are pure functions so they're unit-testable without a database.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any

from mcp_host.data.store import Entitlement, ISO, ProviderRow, hash_key
from mcp_host.data.tenant import IsolationError, TenantManager

logger = logging.getLogger("mcp-host")

# Cold/serverless Postgres (Replit/Neon) suspends when idle and the FIRST connect after a deploy
# can fail or race the DB's wake-up. Retry with backoff so a transient hiccup can't kill boot.
# Total wait budget is ~sum(delays); tunable via env for slow/cold tiers.
_CONNECT_TIMEOUT = int(os.environ.get("MCP_HOST_PG_CONNECT_TIMEOUT", "10"))  # seconds per attempt
_CONNECT_RETRIES = int(os.environ.get("MCP_HOST_PG_CONNECT_RETRIES", "6"))   # attempts after the first
_CONNECT_BACKOFF = (1, 2, 4, 8, 15, 15)  # seconds between attempts; last value repeats if retries exceed it

PLATFORM_DDL = [
    "CREATE SCHEMA IF NOT EXISTS platform",
    """CREATE TABLE IF NOT EXISTS platform.providers(
        id TEXT PRIMARY KEY, display_name TEXT, discipline TEXT, version TEXT, owner TEXT,
        manifest_json JSONB NOT NULL, status TEXT NOT NULL DEFAULT 'active',
        created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    "ALTER TABLE platform.providers ADD COLUMN IF NOT EXISTS owner TEXT",
    """CREATE TABLE IF NOT EXISTS platform.tools(
        provider_id TEXT NOT NULL, name TEXT NOT NULL, scope TEXT NOT NULL,
        price_usdc TEXT NOT NULL, annotations_json JSONB,
        PRIMARY KEY(provider_id, name))""",
    """CREATE TABLE IF NOT EXISTS platform.principals(
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT,
        plan TEXT NOT NULL DEFAULT 'free', created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
    """CREATE TABLE IF NOT EXISTS platform.api_keys(
        id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
        scopes TEXT NOT NULL, revoked_at TIMESTAMPTZ)""",
    """CREATE TABLE IF NOT EXISTS platform.entitlements(
        plan TEXT NOT NULL, provider_id TEXT NOT NULL, scope TEXT NOT NULL,
        monthly_quota INTEGER NOT NULL, rate_per_min INTEGER NOT NULL,
        PRIMARY KEY(plan, provider_id, scope))""",
    """CREATE TABLE IF NOT EXISTS platform.usage(
        id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(), provider_id TEXT NOT NULL,
        tool TEXT NOT NULL, principal_id TEXT NOT NULL, duration_ms INTEGER NOT NULL,
        paid BOOLEAN NOT NULL, tx_hash TEXT, status_code INTEGER NOT NULL)""",
    "CREATE INDEX IF NOT EXISTS idx_usage_lookup ON platform.usage(principal_id, provider_id, ts)",
    """CREATE TABLE IF NOT EXISTS platform.sessions(
        mcp_session_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, principal_id TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS platform.audit(
        id BIGSERIAL PRIMARY KEY, ts TIMESTAMPTZ NOT NULL DEFAULT now(), method TEXT, path TEXT,
        principal_id TEXT, status_code INTEGER, duration_ms INTEGER, ip_masked TEXT)""",
    """CREATE TABLE IF NOT EXISTS platform.artifacts(
        provider_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT, bytes BIGINT,
        uri TEXT, uploaded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY(provider_id, name))""",
]


def now_iso() -> str:
    return time.strftime(ISO, time.gmtime())


def _ident(name: str) -> str:
    """Validate a SQL identifier (schema/table). Raise IsolationError on anything unsafe."""
    if not name or not name.replace("_", "").isalnum():
        raise IsolationError(f"Illegal SQL identifier: {name!r}")
    return name


def tenant_table_ddl(schema: str, table: str, columns: str) -> list[str]:
    """DDL to create an RLS-isolated tenant table. Pure function (testable without a DB).

    tenant_id defaults to the session GUC, and an RLS policy restricts every row to the
    current tenant for SELECT/INSERT/UPDATE/DELETE.
    """
    s, t = _ident(schema), _ident(table)
    fq = f"{s}.{t}"
    pol = f"{t}_tenant_isolation"
    return [
        f"CREATE TABLE IF NOT EXISTS {fq} ("
        f"tenant_id TEXT NOT NULL DEFAULT current_setting('app.tenant_id', true), {columns})",
        f"ALTER TABLE {fq} ENABLE ROW LEVEL SECURITY",
        f"ALTER TABLE {fq} FORCE ROW LEVEL SECURITY",
        f"DROP POLICY IF EXISTS {pol} ON {fq}",
        f"CREATE POLICY {pol} ON {fq} USING (tenant_id = current_setting('app.tenant_id', true)) "
        f"WITH CHECK (tenant_id = current_setting('app.tenant_id', true))",
    ]


def _connect_with_retry(dsn: str):  # pragma: no cover - needs a live DB
    """Open the shared connection, retrying transient failures with backoff.

    A cold/suspended serverless DB, a DNS blip, or "the database system is starting up" on the
    first post-deploy connect would otherwise raise out of the app's lifespan and abort startup
    (the edge then serves a bare 500). We retry a bounded number of times, then re-raise the last
    error loud — we do NOT silently fall back to ephemeral storage in a Postgres deploy.

    Time: O(retries); wall-clock bounded by sum of backoff delays. Space: O(1).
    """
    import psycopg
    from psycopg.rows import dict_row

    last_exc: Exception | None = None
    for attempt in range(_CONNECT_RETRIES + 1):
        try:
            conn = psycopg.connect(
                dsn, autocommit=True, row_factory=dict_row, connect_timeout=_CONNECT_TIMEOUT
            )
            if attempt:
                logger.info("[pg] connected after %d retry(ies)", attempt)
            return conn
        except Exception as exc:  # psycopg.OperationalError et al. — retry the transient ones
            last_exc = exc
            if attempt == _CONNECT_RETRIES:
                break
            delay = _CONNECT_BACKOFF[min(attempt, len(_CONNECT_BACKOFF) - 1)]
            # Don't log the DSN (credentials); log only the error class + message.
            logger.warning(
                "[pg] connect attempt %d/%d failed (%s); retrying in %ds",
                attempt + 1, _CONNECT_RETRIES + 1, type(exc).__name__, delay,
            )
            time.sleep(delay)
    logger.error("[pg] could not connect after %d attempts: %s", _CONNECT_RETRIES + 1, last_exc)
    raise last_exc  # type: ignore[misc]


class PgStore:
    """Control-plane store on Postgres. Mirrors SqliteStore's method surface exactly."""

    def __init__(self, dsn: str, conn: Any | None = None) -> None:
        self.lock = threading.RLock()
        if conn is not None:
            self._conn = conn  # injected (tests)
        else:  # pragma: no cover - needs a live DB
            self._conn = _connect_with_retry(dsn)
        self._init_schema()

    def _exec(self, sql: str, params: tuple = ()):
        with self.lock:
            return self._conn.execute(sql, params)

    def _one(self, sql: str, params: tuple = ()):
        with self.lock:
            return self._conn.execute(sql, params).fetchone()

    def _all(self, sql: str, params: tuple = ()):
        with self.lock:
            return self._conn.execute(sql, params).fetchall()

    def _init_schema(self) -> None:
        with self.lock:
            for stmt in PLATFORM_DDL:
                self._conn.execute(stmt)

    # ---- providers -------------------------------------------------------
    def register_provider(self, manifest: dict[str, Any]) -> None:
        with self.lock:
            self._conn.execute(
                "INSERT INTO platform.providers(id, display_name, discipline, version, owner, manifest_json, status)"
                " VALUES(%s,%s,%s,%s,%s,%s,'active') ON CONFLICT (id) DO UPDATE SET"
                " display_name=EXCLUDED.display_name, discipline=EXCLUDED.discipline,"
                " version=EXCLUDED.version, owner=EXCLUDED.owner, manifest_json=EXCLUDED.manifest_json",
                (manifest["id"], manifest["display_name"], manifest["discipline"],
                 manifest["version"], manifest.get("owner"), json.dumps(manifest)),
            )
            self._conn.execute("DELETE FROM platform.tools WHERE provider_id=%s", (manifest["id"],))
            for t in manifest["tools"]:
                self._conn.execute(
                    "INSERT INTO platform.tools(provider_id, name, scope, price_usdc, annotations_json)"
                    " VALUES(%s,%s,%s,%s,%s)",
                    (manifest["id"], t["name"], t["scope"], t.get("price_usdc", "0.00"),
                     json.dumps(t.get("annotations", {}))),
                )

    def get_provider(self, provider_id: str) -> ProviderRow | None:
        r = self._one("SELECT * FROM platform.providers WHERE id=%s", (provider_id,))
        if not r:
            return None
        m = r["manifest_json"]
        m = m if isinstance(m, dict) else json.loads(m)
        return ProviderRow(r["id"], r["display_name"], r["discipline"], r["version"], m, r["status"])

    def list_providers(self) -> list[ProviderRow]:
        out = []
        for r in self._all("SELECT * FROM platform.providers ORDER BY id"):
            m = r["manifest_json"]
            m = m if isinstance(m, dict) else json.loads(m)
            out.append(ProviderRow(r["id"], r["display_name"], r["discipline"], r["version"], m, r["status"]))
        return out

    # ---- principals / api keys ------------------------------------------
    def create_principal(self, principal_id: str, kind: str = "user", plan: str = "free",
                         owner: str | None = None) -> None:
        self._exec(
            "INSERT INTO platform.principals(id, kind, owner, plan) VALUES(%s,%s,%s,%s)"
            " ON CONFLICT (id) DO UPDATE SET kind=EXCLUDED.kind, plan=EXCLUDED.plan, owner=EXCLUDED.owner",
            (principal_id, kind, owner, plan),
        )

    def add_api_key(self, key_id: str, principal_id: str, raw_key: str, scopes: list[str]) -> None:
        self._exec(
            "INSERT INTO platform.api_keys(id, principal_id, key_hash, scopes, revoked_at)"
            " VALUES(%s,%s,%s,%s,NULL) ON CONFLICT (id) DO UPDATE SET key_hash=EXCLUDED.key_hash,"
            " scopes=EXCLUDED.scopes, revoked_at=NULL",
            (key_id, principal_id, hash_key(raw_key), ",".join(scopes)),
        )

    def principal_for_key(self, raw_key: str) -> tuple[str, str, tuple[str, ...]] | None:
        r = self._one(
            "SELECT k.principal_id, k.scopes, p.plan FROM platform.api_keys k"
            " JOIN platform.principals p ON p.id=k.principal_id"
            " WHERE k.key_hash=%s AND k.revoked_at IS NULL",
            (hash_key(raw_key),),
        )
        if not r:
            return None
        scopes = tuple(s for s in r["scopes"].split(",") if s)
        return r["principal_id"], r["plan"], scopes

    # ---- entitlements ----------------------------------------------------
    def set_entitlement(self, e: Entitlement) -> None:
        self._exec(
            "INSERT INTO platform.entitlements(plan, provider_id, scope, monthly_quota, rate_per_min)"
            " VALUES(%s,%s,%s,%s,%s) ON CONFLICT (plan, provider_id, scope) DO UPDATE SET"
            " monthly_quota=EXCLUDED.monthly_quota, rate_per_min=EXCLUDED.rate_per_min",
            (e.plan, e.provider_id, e.scope, e.monthly_quota, e.rate_per_min),
        )

    def get_entitlement(self, plan: str, provider_id: str, scope: str) -> Entitlement | None:
        r = self._one(
            "SELECT * FROM platform.entitlements WHERE plan=%s AND provider_id=%s AND scope=%s",
            (plan, provider_id, scope),
        )
        if not r:
            return None
        return Entitlement(r["plan"], r["provider_id"], r["scope"], r["monthly_quota"], r["rate_per_min"])

    # ---- usage / metering ------------------------------------------------
    def record_usage(self, provider_id: str, tool: str, principal_id: str, duration_ms: int,
                     paid: bool, status_code: int, tx_hash: str | None = None) -> None:
        self._exec(
            "INSERT INTO platform.usage(provider_id, tool, principal_id, duration_ms, paid, tx_hash, status_code)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s)",
            (provider_id, tool, principal_id, duration_ms, paid, tx_hash, status_code),
        )

    def usage_count_since(self, principal_id: str, provider_id: str, since_iso: str) -> int:
        r = self._one(
            "SELECT COUNT(*) AS n FROM platform.usage WHERE principal_id=%s AND provider_id=%s"
            " AND ts>=%s::timestamptz AND status_code<400",
            (principal_id, provider_id, since_iso),
        )
        return int(r["n"])

    def recent_call_count(self, principal_id: str, provider_id: str, window_secs: int) -> int:
        r = self._one(
            "SELECT COUNT(*) AS n FROM platform.usage WHERE principal_id=%s AND provider_id=%s"
            " AND ts >= now() - make_interval(secs => %s)",
            (principal_id, provider_id, window_secs),
        )
        return int(r["n"])

    def usage_summary(self) -> list[dict[str, Any]]:
        rows = self._all(
            "SELECT provider_id, tool, COUNT(*) AS calls, COALESCE(SUM(CASE WHEN paid THEN 1 ELSE 0 END),0) AS paid_calls,"
            " AVG(duration_ms) AS avg_ms FROM platform.usage GROUP BY provider_id, tool ORDER BY calls DESC"
        )
        return [dict(r) for r in rows]

    # ---- sessions --------------------------------------------------------
    def create_session(self, session_id: str, provider_id: str, principal_id: str, ttl_secs: int = 3600) -> None:
        self._exec(
            "INSERT INTO platform.sessions(mcp_session_id, provider_id, principal_id, expires_at)"
            " VALUES(%s,%s,%s, now() + make_interval(secs => %s))"
            " ON CONFLICT (mcp_session_id) DO UPDATE SET expires_at=EXCLUDED.expires_at",
            (session_id, provider_id, principal_id, ttl_secs),
        )

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        r = self._one(
            "SELECT * FROM platform.sessions WHERE mcp_session_id=%s AND expires_at > now()",
            (session_id,),
        )
        return dict(r) if r else None

    # ---- audit / artifacts ----------------------------------------------
    def audit(self, method: str, path: str, principal_id: str | None, status_code: int,
              duration_ms: int, ip_masked: str) -> None:
        self._exec(
            "INSERT INTO platform.audit(method, path, principal_id, status_code, duration_ms, ip_masked)"
            " VALUES(%s,%s,%s,%s,%s,%s)",
            (method, path, principal_id, status_code, duration_ms, ip_masked),
        )

    def record_artifact(self, provider_id: str, name: str, kind: str, nbytes: int, uri: str) -> None:
        self._exec(
            "INSERT INTO platform.artifacts(provider_id, name, kind, bytes, uri) VALUES(%s,%s,%s,%s,%s)"
            " ON CONFLICT (provider_id, name) DO UPDATE SET kind=EXCLUDED.kind, bytes=EXCLUDED.bytes,"
            " uri=EXCLUDED.uri, uploaded_at=now()",
            (provider_id, name, kind, nbytes, uri),
        )

    def get_artifact(self, provider_id: str, name: str) -> dict[str, Any] | None:
        r = self._one(
            "SELECT * FROM platform.artifacts WHERE provider_id=%s AND name=%s", (provider_id, name)
        )
        return dict(r) if r else None

    def list_artifacts(self, provider_id: str) -> list[dict[str, Any]]:
        return [dict(r) for r in self._all(
            "SELECT * FROM platform.artifacts WHERE provider_id=%s ORDER BY name", (provider_id,))]

    def delete_artifact(self, provider_id: str, name: str) -> None:
        self._exec("DELETE FROM platform.artifacts WHERE provider_id=%s AND name=%s", (provider_id, name))


class PgTenantDB:
    """Per-provider relational handle. Sets app.tenant_id under the store lock before each op,
    so RLS enforces isolation even against raw SQL."""

    def __init__(self, store: PgStore, schema: str, provider_id: str) -> None:
        self._store = store
        self.schema = _ident(schema)
        self.provider_id = provider_id

    def _set_tenant(self) -> None:
        self._store._conn.execute("SELECT set_config('app.tenant_id', %s, false)", (self.provider_id,))

    def create_table(self, name: str, columns: str) -> None:
        with self._store.lock:
            self._set_tenant()
            for stmt in tenant_table_ddl(self.schema, name, columns):
                self._store._conn.execute(stmt)

    def insert(self, table: str, row: dict[str, Any]) -> None:
        if "tenant_id" in row:
            raise IsolationError("Do not set tenant_id; the host owns it.")
        cols = list(row.keys())
        placeholders = ",".join(["%s"] * len(cols))
        with self._store.lock:
            self._set_tenant()  # tenant_id column default fills from the GUC
            self._store._conn.execute(
                f"INSERT INTO {self.schema}.{_ident(table)} ({','.join(_ident(c) for c in cols)})"
                f" VALUES ({placeholders})", tuple(row.values()),
            )

    def query(self, table: str, where: str = "", params: tuple = ()) -> list[dict[str, Any]]:
        clause = f"WHERE {where}" if where else ""  # RLS already scopes to this tenant
        with self._store.lock:
            self._set_tenant()
            rows = self._store._conn.execute(
                f"SELECT * FROM {self.schema}.{_ident(table)} {clause}", tuple(params)
            ).fetchall()
        return [dict(r) for r in rows]


class PgTenantManager(TenantManager):
    def __init__(self, store: PgStore) -> None:
        self._store = store

    def provision(self, provider_id: str, schema: str) -> None:
        with self._store.lock:
            self._store._conn.execute(f"CREATE SCHEMA IF NOT EXISTS {_ident(schema)}")
        self._schema_for = getattr(self, "_schema_for", {})
        self._schema_for[provider_id] = _ident(schema)

    def handle(self, provider_id: str) -> PgTenantDB:
        schema = getattr(self, "_schema_for", {}).get(provider_id, _ident(provider_id.replace("-", "_")))
        return PgTenantDB(self._store, schema, provider_id)
