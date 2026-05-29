"""M4-M6 — the three pilots conform to the protocol and serve correctly through the gateway."""

from __future__ import annotations

import json

import pytest

from mcp_host.auth.principal import mint_token
from mcp_host.billing.x402 import StubFacilitator
from mcp_host.data.store import Entitlement, SqliteStore
from mcp_host.data.tenant import open_tenant_conn
from mcp_host.gateway.router import Gateway, GatewayConfig
from providers import load_pilots

CFG = GatewayConfig("https://mcp-host", "k", "0xSHARED", "admin")


@pytest.fixture
def gw():
    store = SqliteStore()
    g = Gateway(store, CFG, StubFacilitator(), tenant_conn=open_tenant_conn())
    for provider, secrets in load_pilots():
        g.mount(provider, secrets)
        for scope in provider.manifest["auth"]["scopes"]:
            store.set_entitlement(Entitlement("pro", provider.id, scope, 100000, 1000))
    return g


def _hdr(provider, scopes, extra=None):
    h = {"authorization": f"Bearer {mint_token('k', 'u', 'pro', list(scopes), f'https://mcp-host/mcp/{provider}')}"}
    if extra:
        h.update(extra)
    return h


def _call(gw, provider, tool, args, scopes, extra=None):
    return gw.handle(provider, {"id": 1, "method": "tools/call",
                                "params": {"name": tool, "arguments": args}},
                     _hdr(provider, scopes, extra))


def _payload(res):
    return json.loads(res.body["result"]["content"][0]["text"])


def test_all_pilots_mount_and_list(gw):
    assert {p.id for p in gw.providers()} == {"edgar-rag", "signal-builder", "social-trader"}
    r = gw.handle("edgar-rag", {"id": 1, "method": "tools/list"}, _hdr("edgar-rag", ["edgar:read"]))
    names = {t["name"] for t in r.body["result"]["tools"]}
    assert names == {"search_filings", "list_companies", "get_filing", "get_data_catalog"}


def test_edgar_search(gw):
    res = _call(gw, "edgar-rag", "search_filings", {"query": "supply chain risk"}, ["edgar:search"])
    assert res.status == 200
    p = _payload(res)
    assert p["count"] >= 1 and p["results"][0]["company"] == "APPLE INC"


def test_edgar_list_companies(gw):
    res = _call(gw, "edgar-rag", "list_companies", {}, ["edgar:read"])
    companies = {c["company"] for c in _payload(res)["companies"]}
    assert {"APPLE INC", "NVIDIA CORP", "TESLA INC"} <= companies


def test_signal_trending_envelope(gw):
    res = _call(gw, "signal-builder", "panels.trending", {"limit": 3}, ["signal:read"])
    p = _payload(res)
    assert p["schema_version"] == 3 and "built_at" in p
    assert p["payload"]["tickers"][0]["ticker"] == "NVDA"


def test_signal_write_then_read_isolated(gw):
    w = _call(gw, "signal-builder", "signal.track_ticker", {"ticker": "amd"}, ["signal:write"])
    assert w.status == 200
    r = _call(gw, "signal-builder", "signal.list_tracked_tickers", {}, ["signal:read"])
    assert "AMD" in _payload(r)["payload"]["tracked"]


def test_trader_feed_is_priced_and_gated(gw):
    # subscribe scope but no payment -> 402 (priced tool)
    res = _call(gw, "social-trader", "signals.feed", {"limit": 5}, ["trader:subscribe"])
    assert res.status == 402
    # with payment -> 200
    res2 = _call(gw, "social-trader", "signals.feed", {"limit": 5}, ["trader:subscribe"],
                 extra={"x-payment": "paid:abc"})
    assert res2.status == 200
    assert _payload(res2)["count"] >= 1


def test_trader_history_free(gw):
    res = _call(gw, "social-trader", "signals.history", {}, ["trader:read"])
    assert res.status == 200 and _payload(res)["count"] >= 1
