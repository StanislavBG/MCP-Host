"""M10 — managed-dataset providers: an agent declares a data shape, the host stores rows and
serves auto-generated query/get tools. Covers the normalization layer, the tenant store ops
(write modes + filtered query + isolation), manifest expansion, the provider, and deploy.
"""

from __future__ import annotations

import pytest

from mcp_host.billing.x402 import StubFacilitator
from mcp_host.data import dataset_sql as ds
from mcp_host.data.store import SqliteStore
from mcp_host.data.tenant import TenantDB, open_tenant_conn
from mcp_host.gateway.deploy import publish_provider
from mcp_host.gateway.router import Gateway, GatewayConfig
from mcp_host.sdk import Principal, ToolContext
from mcp_host.sdk.dataset import ManagedDatasetProvider, expand_dataset_manifest
from mcp_host.sdk.errors import ErrorCode, ToolError
from mcp_host.sdk.manifest import ManifestError

CFG = GatewayConfig("https://mcp-host", "k", "0xSHARED", "admin")
PLANS = {
    "free": {"quota": 1000, "rate": 120, "scopes_suffix": (":read",)},
    "pro": {"quota": 100000, "rate": 600, "scopes_suffix": (":read", ":write")},
}

ROWS = [
    {"ticker": "NVDA", "signal": "buy", "confidence": 0.9, "as_of": "2026-06-01"},
    {"ticker": "AMD", "signal": "sell", "confidence": 0.4, "as_of": "2026-06-02"},
    {"ticker": "INTC", "signal": "hold", "confidence": 0.6, "as_of": "2026-06-03"},
]


def _terse(pid="social-signals-trader"):
    return {
        "id": pid, "display_name": "Social Signals Trader", "discipline": "social-trading-signals",
        "version": "1.0.0", "summary": "Buy/sell trading signals derived from social sentiment per ticker.",
        "transport": "streamable-http",
        "datasets": [{"name": "signals", "key": "ticker",
                      "description": "Latest signal and confidence per ticker.",
                      "indexed": ["ticker", "as_of"]}],
    }


def _tdb(provider_id="social-signals-trader"):
    return TenantDB(open_tenant_conn(), provider_id)


# ---- normalization (dataset_sql) -----------------------------------------
def test_normalize_filters_forms():
    out = ds.normalize_filters({"ticker": "NVDA", "confidence": {"gte": 0.5}})
    assert ("ticker", "eq", "NVDA") in out and ("confidence", "gte", 0.5) in out


def test_normalize_filters_in():
    assert ds.normalize_filters({"ticker": {"in": ["NVDA", "AMD"]}}) == [("ticker", "in", ["NVDA", "AMD"])]


@pytest.mark.parametrize("bad", [
    {"bad field": 1}, {"x": {"bogus": 1}}, {"x": {"gt": 1, "lt": 2}}, {"x": {"in": []}}, "notadict",
])
def test_normalize_filters_rejects(bad):
    with pytest.raises(ds.DatasetError):
        ds.normalize_filters(bad)


def test_sort_and_limit_and_cursor():
    assert ds.parse_sort("-as_of") == ("as_of", "DESC")
    assert ds.clamp_limit(99999) == ds.MAX_LIMIT
    assert ds.decode_cursor(ds.encode_cursor(40)) == 40
    with pytest.raises(ds.DatasetError):
        ds.decode_cursor("!!!notbase64!!!")


# ---- tenant store ops -----------------------------------------------------
def test_write_replace_then_query_all():
    t = _tdb()
    assert t.dataset_write("signals", "ticker", ROWS, "replace") == 3
    res = t.dataset_query("signals")
    assert {r["ticker"] for r in res["rows"]} == {"NVDA", "AMD", "INTC"}
    assert res["next_cursor"] is None


def test_query_filter_and_sort():
    t = _tdb()
    t.dataset_write("signals", "ticker", ROWS, "replace")
    res = t.dataset_query("signals", filters={"confidence": {"gte": 0.6}}, sort="-confidence")
    tickers = [r["ticker"] for r in res["rows"]]
    assert tickers == ["NVDA", "INTC"]  # 0.9 then 0.6; AMD (0.4) excluded


def test_query_in_filter():
    t = _tdb()
    t.dataset_write("signals", "ticker", ROWS, "replace")
    res = t.dataset_query("signals", filters={"ticker": {"in": ["NVDA", "AMD"]}})
    assert {r["ticker"] for r in res["rows"]} == {"NVDA", "AMD"}


def test_pagination_cursor():
    t = _tdb()
    t.dataset_write("signals", "ticker", ROWS, "replace")
    p1 = t.dataset_query("signals", sort="ticker", limit=2)
    assert len(p1["rows"]) == 2 and p1["next_cursor"]
    p2 = t.dataset_query("signals", sort="ticker", limit=2, cursor=p1["next_cursor"])
    assert len(p2["rows"]) == 1 and p2["next_cursor"] is None


def test_upsert_replaces_by_key_get_latest():
    t = _tdb()
    t.dataset_write("signals", "ticker", ROWS, "replace")
    t.dataset_write("signals", "ticker", [{"ticker": "NVDA", "signal": "sell", "confidence": 0.1}], "upsert")
    got = t.dataset_get("signals", "NVDA")
    assert got["signal"] == "sell"
    assert len(t.dataset_query("signals")["rows"]) == 3  # still 3 distinct keys


def test_write_missing_key_field_rejected():
    t = _tdb()
    with pytest.raises(ds.DatasetError):
        t.dataset_write("signals", "ticker", [{"signal": "buy"}], "append")


def test_dataset_isolation_between_providers():
    conn = open_tenant_conn()
    a, b = TenantDB(conn, "prov-a"), TenantDB(conn, "prov-b")
    a.dataset_write("signals", "ticker", ROWS, "replace")
    b.dataset_write("signals", "ticker", [{"ticker": "TSLA", "signal": "buy"}], "replace")
    assert {r["ticker"] for r in a.dataset_query("signals")["rows"]} == {"NVDA", "AMD", "INTC"}
    assert {r["ticker"] for r in b.dataset_query("signals")["rows"]} == {"TSLA"}


# ---- manifest expansion ---------------------------------------------------
def test_expand_generates_tools_and_scopes():
    m = expand_dataset_manifest(_terse())
    names = {t["name"] for t in m["tools"]}
    assert names == {"signals.query", "signals.get", "signals.publish"}
    assert m["auth"]["scopes"] == ["social-signals-trader:read", "social-signals-trader:admin"]
    assert m["data"]["postgres_schema"] == "social_signals_trader"
    # publish is the only :admin (owner-gated) tool
    pub = next(t for t in m["tools"] if t["name"] == "signals.publish")
    assert pub["scope"].endswith(":admin")


@pytest.mark.parametrize("mutate", [
    lambda m: m.update(datasets=[]),
    lambda m: m.update(datasets=[{"name": "Bad Name", "key": "id"}]),
    lambda m: m["datasets"].append({"name": "signals", "key": "id"}),  # duplicate
])
def test_expand_rejects_bad_datasets(mutate):
    m = _terse()
    mutate(m)
    with pytest.raises(ManifestError):
        expand_dataset_manifest(m)


# ---- ManagedDatasetProvider ----------------------------------------------
def test_provider_publish_query_get_roundtrip():
    p = ManagedDatasetProvider(expand_dataset_manifest(_terse()))
    ctx = ToolContext("social-signals-trader",
                      Principal(id="owner", plan="pro", scopes=("social-signals-trader:admin",)),
                      tenant_db=_tdb())
    pub = p.call_tool(ctx, "signals.publish", {"mode": "replace", "rows": ROWS})
    assert pub["payload"]["written"] == 3 and pub["schema_version"] == "1"
    q = p.call_tool(ctx, "signals.query", {"filters": {"signal": "buy"}})
    assert [r["ticker"] for r in q["payload"]["rows"]] == ["NVDA"]
    g = p.call_tool(ctx, "signals.get", {"key": "AMD"})
    assert g["payload"]["found"] and g["payload"]["record"]["signal"] == "sell"


def test_provider_publish_bad_rows_is_validation_error():
    p = ManagedDatasetProvider(expand_dataset_manifest(_terse()))
    ctx = ToolContext("social-signals-trader", Principal(id="owner"), tenant_db=_tdb())
    with pytest.raises(ToolError) as ei:
        p.call_tool(ctx, "signals.publish", {"rows": [{"no_key": 1}]})
    assert ei.value.code == ErrorCode.VALIDATION_ERROR


# ---- deploy ---------------------------------------------------------------
def _gw():
    return Gateway(SqliteStore(), CFG, StubFacilitator(), tenant_conn=open_tenant_conn())


def test_publish_dataset_provider_mounts_and_binds_owner():
    gw = _gw()
    res = publish_provider(gw, _terse(), "usr_abc", "k", default_plans=PLANS)
    assert res["kind"] == "managed-dataset" and res["owner"] == "usr_abc"
    assert res["datasets"] == ["signals"]
    assert isinstance(gw.provider("social-signals-trader"), ManagedDatasetProvider)
    assert gw.store.get_entitlement("free", "social-signals-trader", "social-signals-trader:read")


def test_publish_rejects_neither_backend_nor_datasets():
    gw = _gw()
    bare = _terse()
    bare.pop("datasets")
    with pytest.raises(ToolError) as ei:
        publish_provider(gw, bare, "usr_abc", "k", default_plans=PLANS)
    assert ei.value.code == ErrorCode.INVALID_REQUEST
