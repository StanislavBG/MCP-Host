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
