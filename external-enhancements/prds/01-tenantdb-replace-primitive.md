# PRD 01 — `TenantDB` replace/delete primitive

## Problem
`mode: "replace"` ingest (overwrite the live signal set on every fill) needs a tenant-scoped
delete. `TenantDB` (SQLite) and `PgTenantDB` (Postgres) expose `create_table`/`insert`/`query`
but no `delete`. Without it, replace would have to read-all + diff in Python, or leak across the
RLS boundary — both wrong.

## Change
Add `delete(table, where="", params=())` to both tenant backends:

- `mcp_host/data/tenant.py::TenantDB.delete` — ANDs the tenant filter exactly like `query`; empty
  `where` clears all of *this tenant's* rows in the table. Returns rows deleted. `?` placeholders only.
- `mcp_host/data/pg.py::PgTenantDB.delete` — RLS already scopes to the tenant (set via the GUC
  before the statement); empty `where` clears the tenant's rows. `%s` placeholders.

Both keep the existing isolation guarantee: a provider can only delete its own rows.

## Acceptance criteria
1. `delete("t")` on a tenant removes only that tenant's rows; another tenant's rows in the same
   physical table are untouched (isolation preserved).
2. `delete("t", "ticker=?", ("NVDA",))` deletes the matching subset for the tenant only.
3. Returns the count of rows deleted.
4. Pure DDL/string builders unchanged; no new deps.

## Tests (`tests/test_m2_data.py`)
- `test_tenant_delete_all_is_isolated` — two tenants insert into the same table; tenant A's
  `delete("t")` leaves tenant B's rows intact (assert via `raw_count_all`).
- `test_tenant_delete_where_subset` — delete one ticker, others remain.

## Risk
Low. Additive method; existing callers untouched. `DELETE` on a not-yet-created table raises —
documented as "create_table before delete"; the PRD-02 ingest body always creates first.
