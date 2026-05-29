"""Gateway hot-path integration: auth -> entitlement -> billing -> dispatch -> metering."""

from __future__ import annotations

import pytest

from mcp_host.auth.principal import mint_token
from mcp_host.billing.x402 import StubFacilitator
from mcp_host.data.store import Entitlement, SqliteStore
from mcp_host.data.tenant import open_tenant_conn
from mcp_host.gateway.router import Gateway, GatewayConfig
from tests.conftest import HelloProvider

CFG = GatewayConfig(base_url="https://mcp-host", signing_key="k", wallet_address="0xSHARED",
                    admin_key="admin")


def _gw():
    store = SqliteStore()
    gw = Gateway(store, CFG, StubFacilitator(), tenant_conn=open_tenant_conn())
    gw.mount(HelloProvider(), secrets={})
    # Grant the free plan both scopes.
    store.set_entitlement(Entitlement("free", "hello", "hello:read", 1000, 100))
    store.set_entitlement(Entitlement("free", "hello", "hello:write", 1000, 100))
    return gw, store


def _tok(scopes, provider="hello", plan="free"):
    return mint_token("k", "alice", plan, list(scopes), f"https://mcp-host/mcp/{provider}")


def _bearer(scopes, **kw):
    return {"authorization": f"Bearer {_tok(scopes, **kw)}"}


def test_initialize_opens_session():
    gw, _ = _gw()
    r = gw.handle("hello", {"id": 1, "method": "initialize"}, _bearer(["hello:read"]))
    assert r.status == 200
    assert r.body["result"]["serverInfo"]["name"] == "hello"
    assert "Mcp-Session-Id" in r.headers


def test_unknown_provider():
    gw, _ = _gw()
    r = gw.handle("ghost", {"id": 1, "method": "initialize"}, _bearer(["hello:read"]))
    assert r.status == 404
    assert r.body["error"]["data"]["code"] == "PROVIDER_NOT_FOUND"


def test_missing_auth_rejected():
    gw, _ = _gw()
    r = gw.handle("hello", {"id": 1, "method": "tools/list"}, {})
    assert r.status == 401


def test_free_tool_call_ok_and_metered():
    gw, store = _gw()
    r = gw.handle("hello", {"id": 2, "method": "tools/call",
                            "arguments": None, "params": {"name": "echo",
                            "arguments": {"message": "hi", "times": 2}}},
                  _bearer(["hello:read"]))
    assert r.status == 200
    assert r.body["result"]["content"][0]["text"] == "hi hi"
    assert store.usage_summary()[0]["calls"] == 1


def test_paid_tool_requires_payment():
    gw, _ = _gw()
    r = gw.handle("hello", {"id": 3, "method": "tools/call",
                            "params": {"name": "shout", "arguments": {"message": "hey"}}},
                  _bearer(["hello:write"]))
    assert r.status == 402
    assert r.body["error"]["data"]["code"] == "PAYMENT_REQUIRED"
    # 402 carries the x402 challenge so a client SDK can pay.
    assert r.body["error"]["data"]["challenge"]["accepts"][0]["payTo"] == "0xSHARED"
    assert r.body["error"]["code"] == -32003


def test_paid_tool_with_payment_succeeds():
    gw, store = _gw()
    headers = _bearer(["hello:write"])
    headers["x-payment"] = "paid:abc123"
    r = gw.handle("hello", {"id": 4, "method": "tools/call",
                            "params": {"name": "shout", "arguments": {"message": "hey"}}}, headers)
    assert r.status == 200
    assert r.body["result"]["content"][0]["text"] == "HEY"
    assert r.headers.get("X-Payment-Response", "").startswith("0x")
    # metered as paid
    paid = store._conn.execute("SELECT paid, tx_hash FROM usage WHERE tool='shout'").fetchone()
    assert paid["paid"] == 1 and paid["tx_hash"].startswith("0x")


def test_paid_tool_admin_bypass():
    gw, _ = _gw()
    headers = _bearer(["hello:write"])
    headers["x-admin-key"] = "admin"
    r = gw.handle("hello", {"id": 5, "method": "tools/call",
                            "params": {"name": "shout", "arguments": {"message": "x"}}}, headers)
    assert r.status == 200


def test_scope_enforced():
    gw, _ = _gw()
    # token only has read scope, tries the write tool
    r = gw.handle("hello", {"id": 6, "method": "tools/call",
                            "params": {"name": "shout", "arguments": {"message": "x"}}},
                  _bearer(["hello:read"]))
    assert r.status == 403
    assert r.body["error"]["data"]["code"] == "FORBIDDEN_SCOPE"


def test_resource_indicator_enforced_across_providers():
    gw, _ = _gw()
    # token minted for a different provider URI
    bad = {"authorization": f"Bearer {mint_token('k', 'alice', 'free', ['hello:read'], 'https://mcp-host/mcp/other')}"}
    r = gw.handle("hello", {"id": 7, "method": "tools/list"}, bad)
    assert r.status == 403


def test_quota_exhaustion_returns_429():
    store = SqliteStore()
    gw = Gateway(store, CFG, StubFacilitator(), tenant_conn=open_tenant_conn())
    gw.mount(HelloProvider())
    store.set_entitlement(Entitlement("free", "hello", "hello:read", monthly_quota=1, rate_per_min=100))
    body = {"method": "tools/call", "params": {"name": "echo", "arguments": {"message": "x"}}}
    assert gw.handle("hello", {**body, "id": 1}, _bearer(["hello:read"])).status == 200
    r = gw.handle("hello", {**body, "id": 2}, _bearer(["hello:read"]))
    assert r.status == 429
    assert r.body["error"]["data"]["code"] == "QUOTA_EXCEEDED"
