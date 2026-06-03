"""TenantDB — the ONLY way a provider touches relational data.

Plan boundary: a provider may read/write only its own `<provider>.*` schema. The gateway
hands the provider a TenantDB pinned to its provider_id. Every statement is automatically
scoped, so a missing manual filter can't leak across providers.

Production (Postgres): each provider gets its own schema and RLS policy
    tenant_id = current_setting('app.tenant_id')
and the gateway runs `SET app.tenant_id = '<provider>'` per request.

Dev/test (SQLite): one DB, every provider table carries a tenant_id column, and TenantDB
injects/filters it on insert/select so the isolation guarantee holds identically. The
isolation test (tests/test_m2_data.py) proves a provider cannot read another's rows.
"""

from __future__ import annotations

import sqlite3
from typing import Any, Iterable


class IsolationError(RuntimeError):
    pass


class TenantDB:
    """A relational handle pinned to one provider. Use create_table/insert/query."""

    def __init__(self, conn: sqlite3.Connection, provider_id: str) -> None:
        self._conn = conn
        self.provider_id = provider_id

    def create_table(self, name: str, columns: str) -> None:
        """Create a tenant table. A tenant_id column is added + enforced automatically."""
        self._guard_name(name)
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._t(name)} (tenant_id TEXT NOT NULL, {columns})"
        )
        self._conn.commit()

    def insert(self, table: str, row: dict[str, Any]) -> None:
        self._guard_name(table)
        if "tenant_id" in row:
            raise IsolationError("Do not set tenant_id; TenantDB owns it.")
        cols = ["tenant_id", *row.keys()]
        vals = [self.provider_id, *row.values()]
        placeholders = ",".join("?" * len(cols))
        self._conn.execute(
            f"INSERT INTO {self._t(table)} ({','.join(cols)}) VALUES ({placeholders})", vals
        )
        self._conn.commit()

    def query(self, table: str, where: str = "", params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        """SELECT * scoped to this tenant. `where` is ANDed with the tenant filter.
        Caller MUST use ? placeholders in `where` (no string interpolation)."""
        self._guard_name(table)
        clause = "WHERE tenant_id=?"
        args: list[Any] = [self.provider_id]
        if where:
            clause += f" AND ({where})"
            args.extend(params)
        rows = self._conn.execute(f"SELECT * FROM {self._t(table)} {clause}", args).fetchall()
        return [dict(r) for r in rows]

    def delete(self, table: str, where: str = "", params: Iterable[Any] = ()) -> int:
        """Tenant-scoped DELETE. Empty `where` clears ALL of this tenant's rows in `table`
        (the replace-mode primitive); a `where` is ANDed with the tenant filter. Returns the
        number of rows deleted. Caller MUST use ? placeholders in `where` (no interpolation).

        The table must already exist (create_table first) — DELETE on a missing table raises."""
        self._guard_name(table)
        clause = "WHERE tenant_id=?"
        args: list[Any] = [self.provider_id]
        if where:
            clause += f" AND ({where})"
            args.extend(params)
        cur = self._conn.execute(f"DELETE FROM {self._t(table)} {clause}", args)
        self._conn.commit()
        return cur.rowcount

    def raw_count_all(self, table: str) -> int:
        """Test/inspection helper: count rows WITHOUT the tenant filter (proves isolation)."""
        self._guard_name(table)
        return int(self._conn.execute(f"SELECT COUNT(*) FROM {self._t(table)}").fetchone()[0])

    # ---- internals -------------------------------------------------------
    def _t(self, name: str) -> str:
        # Emulate `<provider>.<table>` as a single safe identifier in SQLite.
        # Provider ids may contain hyphens (schema pattern allows them); they're illegal in
        # SQL identifiers, so normalize to underscores for the physical table prefix.
        safe_provider = self.provider_id.replace("-", "_")
        return f"{safe_provider}__{name}"

    @staticmethod
    def _guard_name(name: str) -> None:
        if not name.replace("_", "").isalnum():
            raise IsolationError(f"Illegal table name: {name!r}")


def open_tenant_conn(path: str = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


class TenantManager:
    """Produces a per-provider tenant handle and provisions a provider's isolated storage.

    The gateway holds one of these and calls handle(provider_id) per request and
    provision(provider_id, schema) at mount/deploy. Two implementations exist:
    SqliteTenantManager (dev/test) and PgTenantManager (production, real schemas + RLS).
    """

    def provision(self, provider_id: str, schema: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError

    def handle(self, provider_id: str):  # pragma: no cover - interface
        raise NotImplementedError


class SqliteTenantManager(TenantManager):
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def provision(self, provider_id: str, schema: str) -> None:
        # SQLite emulation creates tables lazily on first create_table; nothing to do here.
        return None

    def handle(self, provider_id: str) -> TenantDB:
        return TenantDB(self._conn, provider_id)
