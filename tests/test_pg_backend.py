"""Postgres backend: pure DDL/SQL builders + backend routing (run locally) and a full
control-plane + RLS integration test gated behind MCP_HOST_TEST_PG (run in Replit).

To run the live test in Replit:  MCP_HOST_TEST_PG="$DATABASE_URL" pytest tests/test_pg_backend.py
"""

from __future__ import annotations

import os

import pytest

from mcp_host.data.pg import _ident, tenant_table_ddl
from mcp_host.data.tenant import IsolationError


# ---- pure builders (no DB needed) ---------------------------------------
def test_ident_rejects_injection():
    assert _ident("panel_history") == "panel_history"
    for bad in ("a.b", "x; drop", "with space", "", "a-b"):
        with pytest.raises(IsolationError):
            _ident(bad)


def test_tenant_table_ddl_has_rls_and_default():
    stmts = tenant_table_ddl("signal", "tracked", "ticker TEXT")
    joined = " ".join(stmts)
    assert "signal.tracked" in joined
    assert "current_setting('app.tenant_id', true)" in joined  # column default + policy
    assert "ENABLE ROW LEVEL SECURITY" in joined
    assert "FORCE ROW LEVEL SECURITY" in joined
    assert "CREATE POLICY tracked_tenant_isolation" in joined
    assert "WITH CHECK (tenant_id = current_setting('app.tenant_id', true))" in joined


def test_tenant_table_ddl_guards_names():
    with pytest.raises(IsolationError):
        tenant_table_ddl("signal", "t; drop table x", "c TEXT")


# ---- backend routing -----------------------------------------------------
def test_make_backends_sqlite_default(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    from mcp_host.data.factory import make_backends
    from mcp_host.data.store import SqliteStore
    from mcp_host.data.tenant import SqliteTenantManager

    store, tenant = make_backends()
    assert isinstance(store, SqliteStore) and isinstance(tenant, SqliteTenantManager)


def test_make_backends_routes_to_postgres(monkeypatch):
    """postgres:// DSN selects PgStore/PgTenantManager without opening a real connection."""
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")
    captured = {}

    class FakePgStore:
        def __init__(self, dsn):
            captured["dsn"] = dsn

    monkeypatch.setattr("mcp_host.data.pg.PgStore", FakePgStore)
    from mcp_host.data.factory import make_backends
    from mcp_host.data.pg import PgTenantManager

    store, tenant = make_backends()
    assert isinstance(store, FakePgStore) and isinstance(tenant, PgTenantManager)
    assert captured["dsn"].startswith("postgresql://")


# ---- live integration (Replit) ------------------------------------------
PG = os.environ.get("MCP_HOST_TEST_PG")
pg_live = pytest.mark.skipif(not PG, reason="set MCP_HOST_TEST_PG to a Postgres DSN to run live")


@pg_live
def test_pg_control_plane_roundtrip():
    from mcp_host.data.pg import PgStore
    from mcp_host.data.store import Entitlement
    from tests.conftest import HELLO_MANIFEST

    s = PgStore(PG)
    s.register_provider(HELLO_MANIFEST)
    assert s.get_provider("hello").display_name == "Hello Provider"
    s.create_principal("alice", plan="pro")
    s.add_api_key("k1", "alice", "raw-secret", ["hello:read"])
    assert s.principal_for_key("raw-secret") == ("alice", "pro", ("hello:read",))
    s.set_entitlement(Entitlement("pro", "hello", "hello:read", 100, 60))
    assert s.get_entitlement("pro", "hello", "hello:read").monthly_quota == 100
    s.record_usage("hello", "echo", "alice", 5, paid=False, status_code=200)
    assert s.usage_count_since("alice", "hello", "1970-01-01T00:00:00+00:00") >= 1


@pg_live
def test_pg_rls_isolation():
    """Even pointing a signal-tenant session at edgar's table must return zero rows (RLS)."""
    from mcp_host.data.pg import PgStore, PgTenantManager

    s = PgStore(PG)
    mgr = PgTenantManager(s)
    mgr.provision("edgar", "edgar_test")
    mgr.provision("signal", "signal_test")
    edgar = mgr.handle("edgar")
    edgar.create_table("notes", "body TEXT")
    edgar.insert("notes", {"body": "edgar-secret"})
    assert [r["body"] for r in edgar.query("notes")] == ["edgar-secret"]

    # Switch tenant to signal and read edgar's physical table directly -> RLS yields nothing.
    with s.lock:
        s._conn.execute("SELECT set_config('app.tenant_id', 'signal', false)")
        rows = s._conn.execute("SELECT * FROM edgar_test.notes").fetchall()
    assert rows == []
