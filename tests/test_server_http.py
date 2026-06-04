"""End-to-end HTTP tests via FastAPI TestClient: the full stack over the wire (M1 + M8)."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from mcp_host.auth.principal import mint_token


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("MCP_HOST_SIGNING_KEY", "k")
    monkeypatch.setenv("WALLET_ADDRESS", "0xSHARED")
    monkeypatch.setenv("UPLOAD_SECRET", "admin")
    monkeypatch.setenv("MCP_HOST_BASE_URL", "https://mcp-host")
    import importlib

    import mcp_host.server as srv
    importlib.reload(srv)
    with TestClient(srv.app) as c:
        yield c


def _bearer(provider, scopes):
    return {"Authorization": f"Bearer {mint_token('k', 'u', 'pro', list(scopes), f'https://mcp-host/mcp/{provider}')}"}


def test_health_lists_providers(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert set(r.json()["providers"]) == {
        "platform-health", "platform-publisher", "edgar-rag", "signal-builder", "social-trader"}


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200 and "MCP-Host" in r.text and "edgar-rag" in r.text


def test_well_known(client):
    r = client.get("/mcp/edgar-rag/.well-known/mcp.json")
    assert r.status_code == 200
    assert r.json()["name"] == "io.github.StanislavBG/edgar-rag"


def test_provider_data_catalog(client):
    r = client.get("/mcp/edgar-rag/data")
    assert r.status_code == 200 and r.json()["status"] == "available"


def test_initialize_over_http_sets_session(client):
    r = client.post("/mcp/edgar-rag", json={"id": 1, "method": "initialize"},
                    headers=_bearer("edgar-rag", ["edgar:read"]))
    assert r.status_code == 200
    assert "Mcp-Session-Id" in r.headers
    assert r.json()["result"]["serverInfo"]["name"] == "edgar-rag"


def test_tools_call_over_http(client):
    r = client.post("/mcp/edgar-rag",
                    json={"id": 2, "method": "tools/call",
                          "params": {"name": "search_filings", "arguments": {"query": "AI data center GPUs"}}},
                    headers=_bearer("edgar-rag", ["edgar:search"]))
    assert r.status_code == 200
    payload = json.loads(r.json()["result"]["content"][0]["text"])
    assert payload["results"][0]["company"] == "NVIDIA CORP"


def test_owner_ingest_over_http_then_feed_reflects(client):
    """Enhancement 001 end-to-end: owner refreshes the live set in ONE authenticated call,
    then the priced subscriber feed serves that data instead of the static seed."""
    owner = {"Authorization": f"Bearer {mint_token('k', 'StanislavBG', 'pro', ['trader:admin'], 'https://mcp-host/mcp/social-trader')}"}
    ing = client.post("/mcp/social-trader",
                      json={"id": 1, "method": "tools/call",
                            "params": {"name": "signals.ingest",
                                       "arguments": {"dataset": "signals", "mode": "replace",
                                                     "rows": [{"ticker": "HPE", "side": "short",
                                                               "ts": "2026-06-02T15:00:00+00:00"}]}}},
                      headers=owner)
    assert ing.status_code == 200
    assert json.loads(ing.json()["result"]["content"][0]["text"])["total"] == 1

    h = _bearer("social-trader", ["trader:subscribe"])
    h["X-Payment"] = "paid:beef"
    feed = client.post("/mcp/social-trader",
                       json={"id": 2, "method": "tools/call",
                             "params": {"name": "signals.feed", "arguments": {}}}, headers=h)
    payload = json.loads(feed.json()["result"]["content"][0]["text"])
    assert [s["ticker"] for s in payload["signals"]] == ["HPE"]


def test_owner_ingest_denied_for_non_owner_over_http(client):
    r = client.post("/mcp/social-trader",
                    json={"id": 1, "method": "tools/call",
                          "params": {"name": "signals.ingest",
                                     "arguments": {"dataset": "signals", "rows": [{"ticker": "X", "side": "buy"}]}}},
                    headers=_bearer("social-trader", ["trader:admin"]))  # sub 'u' != owner
    assert r.status_code == 403


def test_paid_feed_402_then_paid(client):
    h = _bearer("social-trader", ["trader:subscribe"])
    body = {"id": 3, "method": "tools/call", "params": {"name": "signals.feed", "arguments": {}}}
    r = client.post("/mcp/social-trader", json=body, headers=h)
    assert r.status_code == 402
    assert r.json()["error"]["data"]["challenge"]["accepts"][0]["payTo"] == "0xSHARED"
    h["X-Payment"] = "paid:beef"
    r2 = client.post("/mcp/social-trader", json=body, headers=h)
    assert r2.status_code == 200
    assert r2.headers.get("X-Payment-Response", "").startswith("0x")


def test_artifact_upload_auth(client):
    # wrong secret rejected
    r = client.post("/mcp/edgar-rag/upload/vectors", content=b"data",
                    headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401
    # correct super-admin secret (UPLOAD_SECRET) accepted, against a DECLARED artifact
    r2 = client.post("/mcp/edgar-rag/upload/vectors", content=b"data",
                     headers={"Authorization": "Bearer admin"})
    assert r2.status_code == 200 and r2.json()["bytes"] == 4


def test_admin_usage_after_calls(client):
    client.post("/mcp/edgar-rag",
                json={"id": 9, "method": "tools/call",
                      "params": {"name": "list_companies", "arguments": {}}},
                headers=_bearer("edgar-rag", ["edgar:read"]))
    r = client.get("/admin/usage")
    assert r.status_code == 200
    tools = {u["tool"] for u in r.json()["usage"]}
    assert "list_companies" in tools


# ---- self-serve registration + declarative deploy (M9) --------------------
def _declarative_manifest(pid="acme-quotes"):
    # IP-literal public endpoint keeps the SSRF guard's DNS path out of the test.
    return {
        "id": pid, "display_name": "Acme Quotes", "discipline": "market-data", "version": "1.0.0",
        "summary": "Acme real-time quotes provider used to exercise the declarative proxy path.",
        "transport": "streamable-http",
        "auth": {"modes": ["api_key"], "scopes": ["acme:read"]},
        "data": {"postgres_schema": "acme_quotes"},
        "tools": [{
            "name": "quotes.get", "scope": "acme:read", "price_usdc": "0.00",
            "description": "Return the latest bid/ask quote for a ticker symbol from Acme's feed.",
            "annotations": {"readOnlyHint": True},
            "input_schema": {"type": "object", "properties": {"ticker": {"type": "string"}}},
        }],
        "backend": {"kind": "external-http", "endpoint": "https://93.184.216.34/mcp"},
        "limits": {"rate_per_min": 60, "max_request_kb": 50},
    }


def test_register_returns_owner_and_one_time_key(client):
    r = client.post("/register", json={"display_name": "Acme"})
    assert r.status_code == 201
    body = r.json()
    assert body["owner_id"].startswith("usr_") and body["api_key"].startswith("mch_sk_")


def test_register_rate_limited(client):
    last = None
    for _ in range(7):
        last = client.post("/register", json={})
    assert last.status_code == 429


def test_publish_requires_api_key(client):
    r = client.post("/providers", json=_declarative_manifest())
    assert r.status_code == 401


def test_register_then_publish_lists_provider(client):
    key = client.post("/register", json={}).json()["api_key"]
    r = client.post("/providers", json=_declarative_manifest(), headers={"x-api-key": key})
    assert r.status_code == 201, r.text
    assert r.json()["id"] == "acme-quotes"
    assert "acme-quotes" in client.get("/health").json()["providers"]


def test_publish_rejects_reserved_id(client):
    key = client.post("/register", json={}).json()["api_key"]
    r = client.post("/providers", json=_declarative_manifest("platform-evil"),
                    headers={"x-api-key": key})
    assert r.status_code == 403


# ---- managed-dataset providers (M10) --------------------------------------
def _dataset_manifest(pid="social-signals-trader"):
    return {
        "id": pid, "display_name": "Social Signals Trader", "discipline": "social-trading-signals",
        "version": "1.0.0", "summary": "Buy/sell signals derived from social sentiment per ticker.",
        "transport": "streamable-http",
        "datasets": [{"name": "signals", "key": "ticker",
                      "description": "Latest signal per ticker.", "indexed": ["ticker"]}],
    }


def test_register_publish_dataset_then_query(client):
    key = client.post("/register", json={}).json()["api_key"]
    r = client.post("/providers", json=_dataset_manifest(), headers={"x-api-key": key})
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "managed-dataset"
    assert "social-signals-trader" in client.get("/health").json()["providers"]

    # owner publishes rows via the REST write path
    w = client.post("/mcp/social-signals-trader/datasets/signals/data",
                    json={"mode": "replace",
                          "rows": [{"ticker": "NVDA", "signal": "buy", "confidence": 0.9}]},
                    headers={"x-api-key": key})
    assert w.status_code == 200 and w.json()["written"] == 1

    # consumer retrieves via the host-generated query tool (read-scoped bearer)
    q = client.post("/mcp/social-signals-trader",
                    json={"id": 1, "method": "tools/call",
                          "params": {"name": "signals.query",
                                     "arguments": {"filters": {"ticker": "NVDA"}}}},
                    headers=_bearer("social-signals-trader", ["social-signals-trader:read"]))
    assert q.status_code == 200, q.text
    assert q.json()["result"]["payload"]["rows"][0]["signal"] == "buy"


def test_owner_can_publish_via_tools_call(client):
    key = client.post("/register", json={}).json()["api_key"]
    client.post("/providers", json=_dataset_manifest(), headers={"x-api-key": key})
    r = client.post("/mcp/social-signals-trader",
                    json={"id": 2, "method": "tools/call",
                          "params": {"name": "signals.publish",
                                     "arguments": {"mode": "append",
                                                   "rows": [{"ticker": "AMD", "signal": "sell"}]}}},
                    headers={"x-api-key": key})  # owner api-key authorizes the :admin tool by ownership
    assert r.status_code == 200, r.text
    assert r.json()["result"]["payload"]["written"] == 1


def test_dataset_write_requires_owner(client):
    key = client.post("/register", json={}).json()["api_key"]
    client.post("/providers", json=_dataset_manifest(), headers={"x-api-key": key})
    # no credential
    r = client.post("/mcp/social-signals-trader/datasets/signals/data", json={"rows": [{"ticker": "X"}]})
    assert r.status_code == 401
    # a different registered principal is not the owner
    other = client.post("/register", json={}).json()["api_key"]
    r2 = client.post("/mcp/social-signals-trader/datasets/signals/data",
                     json={"rows": [{"ticker": "X"}]}, headers={"x-api-key": other})
    assert r2.status_code == 401
