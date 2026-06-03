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
    assert {p.id for p in gw.providers()} == {
        "platform-health", "platform-publisher", "edgar-rag", "signal-builder", "social-trader"}
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


# ---- enhancement 001: owner live-signal ingest ------------------------------
def _owner_call(gw, tool, args, sub="StanislavBG"):
    """Call a social-trader tool as the declared owner (gateway :admin gate)."""
    hdr = {"authorization": f"Bearer {mint_token('k', sub, 'pro', ['trader:admin'], 'https://mcp-host/mcp/social-trader')}"}
    return gw.handle("social-trader", {"id": 1, "method": "tools/call",
                                       "params": {"name": tool, "arguments": args}}, hdr)


_SIG_ROWS = [
    {"ticker": "HPE", "side": "short", "conviction": 0.7, "rationale": "Short into earnings",
     "exit_intent": "Cover at open T+1; 10d time-stop", "ts": "2026-06-02T15:00:00+00:00",
     "status": "OPEN", "outcome_pct": None},
    {"ticker": "MSFT", "side": "buy", "rationale": "Cloud reaccel", "ts": "2026-06-02T16:00:00+00:00"},
]


def test_trader_ingest_owner_then_reads_live(gw):
    ing = _owner_call(gw, "signals.ingest", {"dataset": "signals", "mode": "replace", "rows": _SIG_ROWS})
    assert ing.status == 200
    p = _payload(ing)
    assert p["ingested"] == 2 and p["total"] == 2

    # feed (paid+subscribe) now serves MY tickers, newest-first, no outcome_pct
    feed = _call(gw, "social-trader", "signals.feed", {"limit": 10}, ["trader:subscribe"],
                 extra={"x-payment": "paid:abc"})
    fp = _payload(feed)
    assert [s["ticker"] for s in fp["signals"]] == ["MSFT", "HPE"]
    assert all("outcome_pct" not in s for s in fp["signals"])

    # history serves the same set WITH outcomes
    hist = _payload(_call(gw, "social-trader", "signals.history", {}, ["trader:read"]))
    assert {s["ticker"] for s in hist["signals"]} == {"HPE", "MSFT"}
    assert any("outcome_pct" in s for s in hist["signals"])


def test_trader_ingest_positions_then_read(gw):
    rows = [{"ticker": "HPE", "side": "short", "weight": 0.04, "entry": "2026-06-02"}]
    _owner_call(gw, "signals.ingest", {"dataset": "positions", "mode": "replace", "rows": rows})
    pos = _payload(_call(gw, "social-trader", "portfolio.positions", {}, ["trader:read"]))
    assert pos["count"] == 1 and pos["positions"][0]["ticker"] == "HPE"


def test_trader_ingest_denies_non_owner(gw):
    res = _owner_call(gw, "signals.ingest",
                      {"dataset": "signals", "mode": "replace", "rows": _SIG_ROWS}, sub="mallory")
    assert res.status == 403 and "owner-only" in res.body["error"]["message"]
    # nothing written -> history still on the static seed (NVDA/TSLA)
    hist = _payload(_call(gw, "social-trader", "signals.history", {}, ["trader:read"]))
    assert {s["ticker"] for s in hist["signals"]} == {"NVDA", "TSLA"}


def test_trader_ingest_replace_overwrites(gw):
    _owner_call(gw, "signals.ingest", {"dataset": "signals", "mode": "replace", "rows": _SIG_ROWS})
    second = _owner_call(gw, "signals.ingest",
                         {"dataset": "signals", "mode": "replace",
                          "rows": [{"ticker": "AMD", "side": "buy"}]})
    assert _payload(second)["total"] == 1  # replaced, not accumulated
    hist = _payload(_call(gw, "social-trader", "signals.history", {}, ["trader:read"]))
    assert {s["ticker"] for s in hist["signals"]} == {"AMD"}


def test_trader_ingest_append_adds(gw):
    _owner_call(gw, "signals.ingest", {"dataset": "signals", "mode": "replace", "rows": _SIG_ROWS})
    app = _owner_call(gw, "signals.ingest",
                      {"dataset": "signals", "mode": "append",
                       "rows": [{"ticker": "AMD", "side": "buy"}]})
    assert _payload(app)["total"] == 3


def test_trader_ingest_rejects_bad_row(gw):
    res = _owner_call(gw, "signals.ingest",
                      {"dataset": "signals", "mode": "replace",
                       "rows": [{"ticker": "TOOLONGTICKER", "side": "buy"}]})
    assert res.status >= 400 and res.body.get("error")
