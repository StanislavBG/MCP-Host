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

import json
import sqlite3
import time
from typing import Any, Iterable

from mcp_host.data import dataset_sql as ds
from mcp_host.data.store import _ensure_parent_dir


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


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

    # ---- managed datasets (free-form JSON documents) ---------------------
    # A dataset is one table per provider: (tenant_id, doc_key, doc JSON, updated_at). Rows are
    # arbitrary JSON; filtering/sorting use json_extract(doc, '$.<field>') where <field> has been
    # regex-validated (ds.validate_field) before reaching SQL — so the path is embedded literally,
    # matching the expression index built in dataset_provision (a *bound* path parameter would NOT
    # match that index, leaving `indexed` hints dead). Values are always bound. doc_key is the
    # agent-designated key field; it is indexed, not unique — `get` returns the latest row for a key.
    def _ds_table(self, dataset: str) -> str:
        ds.validate_dataset_name(dataset)
        return f"ds_{dataset}"

    def dataset_provision(self, dataset: str, indexed: Iterable[str] = ()) -> None:
        table = self._ds_table(dataset)
        self._conn.execute(
            f"CREATE TABLE IF NOT EXISTS {self._t(table)} "
            "(tenant_id TEXT NOT NULL, doc_key TEXT NOT NULL, doc TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        self._conn.execute(
            f"CREATE INDEX IF NOT EXISTS {self._t(table)}_key ON {self._t(table)} (tenant_id, doc_key)"
        )
        for field in indexed or ():
            ds.validate_field(field)  # field is regex-checked → safe to embed in the json path
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS {self._t(table)}_f_{field} "
                f"ON {self._t(table)} (json_extract(doc, '$.{field}'))"
            )
        self._conn.commit()

    def dataset_write(self, dataset: str, key_field: str, rows: list[dict], mode: str = "upsert") -> int:
        """replace (clear tenant rows then insert), append (insert), or upsert (replace by key).
        Returns rows written. Each row must be a JSON object containing `key_field`."""
        ds.validate_field(key_field)
        ds.validate_mode(mode)
        if not isinstance(rows, list):
            raise ds.DatasetError("rows must be an array")
        if len(rows) > ds.MAX_ROWS:
            raise ds.DatasetError(f"at most {ds.MAX_ROWS} rows per publish")
        self.dataset_provision(dataset)
        table = self._t(self._ds_table(dataset))
        now = _now_iso()
        if mode == "replace":
            self._conn.execute(f"DELETE FROM {table} WHERE tenant_id=?", [self.provider_id])
        for row in rows:
            if not isinstance(row, dict):
                raise ds.DatasetError("each row must be a JSON object")
            if key_field not in row:
                raise ds.DatasetError(f"row missing key field '{key_field}'")
            doc = json.dumps(row, separators=(",", ":"))
            if len(doc.encode()) > ds.MAX_DOC_BYTES:
                raise ds.DatasetError("row exceeds max document size")
            key = str(row[key_field])
            if mode == "upsert":
                self._conn.execute(
                    f"DELETE FROM {table} WHERE tenant_id=? AND doc_key=?", [self.provider_id, key])
            self._conn.execute(
                f"INSERT INTO {table} (tenant_id, doc_key, doc, updated_at) VALUES (?,?,?,?)",
                [self.provider_id, key, doc, now])
        self._conn.commit()
        return len(rows)

    def dataset_query(self, dataset: str, filters: Any = None, sort: Any = None,
                      limit: Any = None, cursor: Any = None) -> dict[str, Any]:
        """Filtered/sorted/paginated read over this tenant's dataset rows. Returns
        {rows: [...docs], next_cursor: str|None}."""
        norm = ds.normalize_filters(filters)
        srt = ds.parse_sort(sort)
        lim = ds.clamp_limit(limit)
        off = ds.decode_cursor(cursor)
        self.dataset_provision(dataset)
        table = self._t(self._ds_table(dataset))

        where = ["tenant_id=?"]
        args: list[Any] = [self.provider_id]
        for field, op, val in norm:
            # field is regex-validated (normalize_filters -> validate_field), so embedding the
            # json path literally is safe and lets SQLite use the matching expression index.
            expr = f"json_extract(doc, '$.{field}')"
            if op == "in":
                qs = ",".join("?" * len(val))
                where.append(f"{expr} IN ({qs})")
                args.extend(val)
            elif val is None and op in ("eq", "ne"):
                # `field == null` must match missing/null rows; `= NULL` is never true in SQL.
                where.append(f"{expr} IS {'NOT ' if op == 'ne' else ''}NULL")
            else:
                where.append(f"{expr} {ds.OPS[op]} ?")
                args.append(val)
        sql = f"SELECT doc FROM {table} WHERE " + " AND ".join(where)
        if srt:
            sql += f" ORDER BY json_extract(doc, '$.{srt[0]}') " + srt[1]
        else:
            sql += " ORDER BY updated_at DESC"
        sql += " LIMIT ? OFFSET ?"
        args.append(lim + 1)  # fetch one extra to detect a next page
        args.append(off)
        out = self._conn.execute(sql, args).fetchall()
        docs = [json.loads(r["doc"]) for r in out]
        next_cursor = ds.encode_cursor(off + lim) if len(docs) > lim else None
        return {"rows": docs[:lim], "next_cursor": next_cursor}

    def dataset_get(self, dataset: str, key: Any) -> dict[str, Any] | None:
        self.dataset_provision(dataset)
        table = self._t(self._ds_table(dataset))
        r = self._conn.execute(
            f"SELECT doc FROM {table} WHERE tenant_id=? AND doc_key=? ORDER BY updated_at DESC LIMIT 1",
            [self.provider_id, str(key)]).fetchone()
        return json.loads(r["doc"]) if r else None

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
    _ensure_parent_dir(path)
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
