"""M2 — Data layer: control-plane store + tenant isolation (the RLS-equivalent test)."""

from __future__ import annotations

import pytest

from mcp_host.data.store import Entitlement, SqliteStore
from mcp_host.data.tenant import IsolationError, TenantDB, open_tenant_conn
from tests.conftest import HELLO_MANIFEST


def test_register_and_get_provider():
    s = SqliteStore()
    s.register_provider(HELLO_MANIFEST)
    p = s.get_provider("hello")
    assert p is not None and p.display_name == "Hello Provider"
    assert {t for t in (HELLO_MANIFEST["tools"][0]["name"],)} <= {"echo"}
    assert [pr.id for pr in s.list_providers()] == ["hello"]


def test_api_key_roundtrip_stores_only_hash():
    s = SqliteStore()
    s.create_principal("alice", plan="pro")
    s.add_api_key("k1", "alice", "secret-raw-key", ["hello:read", "hello:write"])
    got = s.principal_for_key("secret-raw-key")
    assert got == ("alice", "pro", ("hello:read", "hello:write"))
    assert s.principal_for_key("wrong") is None
    # raw key never stored
    row = s._conn.execute("SELECT key_hash FROM api_keys WHERE id='k1'").fetchone()
    assert row["key_hash"] != "secret-raw-key"


def test_entitlement_and_usage_quota():
    s = SqliteStore()
    s.create_principal("bob", plan="free")
    s.set_entitlement(Entitlement("free", "hello", "hello:read", monthly_quota=2, rate_per_min=10))
    e = s.get_entitlement("free", "hello", "hello:read")
    assert e.monthly_quota == 2
    for _ in range(2):
        s.record_usage("hello", "echo", "bob", 5, paid=False, status_code=200)
    assert s.usage_count_since("bob", "hello", "1970-01-01T00:00:00+00:00") == 2


def test_session_lifecycle():
    s = SqliteStore()
    s.create_session("sess-1", "hello", "bob", ttl_secs=3600)
    assert s.get_session("sess-1")["provider_id"] == "hello"
    s.create_session("sess-2", "hello", "bob", ttl_secs=-1)  # already expired
    assert s.get_session("sess-2") is None


def test_tenant_isolation():
    """A provider must NOT see another provider's rows even though they share the DB."""
    conn = open_tenant_conn()
    edgar = TenantDB(conn, "edgar")
    signal = TenantDB(conn, "signal")
    # Same logical table name in both tenants.
    edgar.create_table("notes", "body TEXT")
    signal.create_table("notes", "body TEXT")  # distinct physical table; that's fine
    edgar.insert("notes", {"body": "edgar-secret"})
    signal.insert("notes", {"body": "signal-secret"})

    # Each sees only its own.
    assert [r["body"] for r in edgar.query("notes")] == ["edgar-secret"]
    assert [r["body"] for r in signal.query("notes")] == ["signal-secret"]

    # Even a tenant-filtered query on a shared table name can't reach across.
    e2 = TenantDB(conn, "edgar")
    e2.create_table("shared", "body TEXT")
    e2.insert("shared", {"body": "e"})
    s2 = TenantDB(conn, "signal")
    # signal opening the SAME logical table sees zero edgar rows
    s2.create_table("shared", "body TEXT")
    assert s2.query("shared") == []


def test_tenant_delete_all_clears_only_caller():
    """replace-mode primitive: delete() with no where clears the caller's rows and no one else's."""
    conn = open_tenant_conn()
    a = TenantDB(conn, "trader")
    b = TenantDB(conn, "signal")
    a.create_table("signals", "ticker TEXT")
    b.create_table("signals", "ticker TEXT")
    a.insert("signals", {"ticker": "NVDA"})
    a.insert("signals", {"ticker": "TSLA"})
    b.insert("signals", {"ticker": "GME"})

    removed = a.delete("signals")
    assert removed == 2
    assert a.query("signals") == []
    assert a.raw_count_all("signals") == 0           # caller's physical table emptied
    assert [r["ticker"] for r in b.query("signals")] == ["GME"]  # other tenant untouched


def test_tenant_delete_where_subset():
    conn = open_tenant_conn()
    t = TenantDB(conn, "trader")
    t.create_table("signals", "ticker TEXT")
    for sym in ("NVDA", "TSLA", "AAPL"):
        t.insert("signals", {"ticker": sym})
    removed = t.delete("signals", "ticker=?", ("TSLA",))
    assert removed == 1
    assert {r["ticker"] for r in t.query("signals")} == {"NVDA", "AAPL"}


def test_tenant_rejects_manual_tenant_id():
    conn = open_tenant_conn()
    t = TenantDB(conn, "edgar")
    t.create_table("x", "body TEXT")
    with pytest.raises(IsolationError):
        t.insert("x", {"tenant_id": "signal", "body": "evil"})


def test_tenant_rejects_bad_table_name():
    conn = open_tenant_conn()
    t = TenantDB(conn, "edgar")
    with pytest.raises(IsolationError):
        t.create_table("notes; DROP TABLE users", "body TEXT")
