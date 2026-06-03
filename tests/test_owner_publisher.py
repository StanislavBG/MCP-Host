"""Phase 2 — owner/admin isolation + the publisher MCP + owner-gated artifact upload."""

from __future__ import annotations

import json

import pytest
from starlette.testclient import TestClient

from mcp_host.auth.principal import mint_token
from mcp_host.data.store import SqliteStore
from mcp_host.gateway.router import Gateway, GatewayConfig
from mcp_host.sdk import Provider, tool

KEY = "dev-signing-key"
BASE = "https://mcp-host"


def _tok(provider, sub, scopes):
    return {"Authorization": f"Bearer {mint_token(KEY, sub, 'pro', list(scopes), f'{BASE}/mcp/{provider}')}"}


def _text(res_json):
    return json.loads(res_json["result"]["content"][0]["text"])


# ---- gateway :admin owner-gate (the general primitive) ----------------------
_ADMIN_MANIFEST = {
    "$schema": "x", "id": "adm", "display_name": "Adm", "discipline": "x", "version": "0.1.0",
    "summary": "admin test provider", "transport": "streamable-http", "owner": "bob",
    "auth": {"modes": ["oauth2.1"], "scopes": ["adm:admin"]},
    "data": {"postgres_schema": "adm"},
    "tools": [{"name": "danger", "scope": "adm:admin", "price_usdc": "0.00",
               "annotations": {"destructiveHint": True},
               "description": "An owner-only admin tool used to test the gateway :admin gate."}],
}


class _AdminProv(Provider):
    def __init__(self):
        super().__init__(manifest=_ADMIN_MANIFEST)

    @tool("danger")
    def danger(self, ctx):
        return ctx.json_text({"did": "danger"})


def _admin_gw():
    store = SqliteStore(":memory:")
    gw = Gateway(store, GatewayConfig(BASE, KEY, "0xS", "", platform_owner="root"), facilitator=None)
    gw.mount(_AdminProv(), {})
    return gw


def _call(gw, provider, sub, scopes, tool_name):
    return gw.handle(provider, {"id": 1, "method": "tools/call",
                                "params": {"name": tool_name, "arguments": {}}},
                     _tok(provider, sub, scopes))


def test_admin_scope_allows_owner():
    r = _call(_admin_gw(), "adm", "bob", ["adm:admin"], "danger")
    assert r.status == 200 and _text(r.body)["did"] == "danger"


def test_admin_scope_allows_platform_super_admin():
    r = _call(_admin_gw(), "adm", "root", ["adm:admin"], "danger")
    assert r.status == 200


def test_admin_scope_denies_non_owner():
    r = _call(_admin_gw(), "adm", "mallory", ["adm:admin"], "danger")
    assert r.status == 403 and "owner-only" in r.body["error"]["message"]


# ---- publisher MCP + upload (full app) --------------------------------------
@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP_HOST_SIGNING_KEY", KEY)
    monkeypatch.setenv("MCP_HOST_BASE_URL", BASE)
    monkeypatch.setenv("MCP_HOST_PLATFORM_OWNER", "StanislavBG")
    monkeypatch.setenv("MCP_HOST_ARTIFACTS", str(tmp_path / "artifacts"))  # isolate artifact bytes
    monkeypatch.delenv("UPLOAD_SECRET", raising=False)
    import importlib

    import mcp_host.server as srv
    importlib.reload(srv)
    with TestClient(srv.app) as c:
        yield c


def _owner_hdr(provider):
    # platform owner defaults to "StanislavBG"; all seeded providers are owned by it.
    return _tok(provider, "StanislavBG", ["publisher:write"])


def test_list_datasets_owner_sees_declared_artifact(client):
    r = client.post("/mcp/platform-publisher",
                    json={"id": 1, "method": "tools/call",
                          "params": {"name": "list_datasets", "arguments": {"provider_id": "edgar-rag"}}},
                    headers=_owner_hdr("platform-publisher"))
    out = _text(r.json())
    assert {d["name"] for d in out["datasets"]} == {"vectors"}
    assert out["datasets"][0]["status"] == "pending"


def test_publisher_denies_non_owner(client):
    r = client.post("/mcp/platform-publisher",
                    json={"id": 1, "method": "tools/call",
                          "params": {"name": "list_datasets", "arguments": {"provider_id": "edgar-rag"}}},
                    headers=_tok("platform-publisher", "stranger", ["publisher:write"]))
    assert r.json()["error"]["data"]["code"]  # ToolError envelope
    assert "do not own" in r.json()["error"]["message"]


def test_upload_finalize_list_delete_cycle(client):
    # 1. owner uploads bytes for the DECLARED artifact "vectors" of edgar-rag
    up = client.post("/mcp/edgar-rag/upload/vectors", content=b"VECTORDATA",
                     headers=_tok("edgar-rag", "StanislavBG", []))
    assert up.status_code == 200 and up.json()["bytes"] == 10

    # 2. finalize via the publisher records size + sha
    fin = client.post("/mcp/platform-publisher",
                      json={"id": 2, "method": "tools/call",
                            "params": {"name": "finalize_upload",
                                       "arguments": {"provider_id": "edgar-rag", "name": "vectors"}}},
                      headers=_owner_hdr("platform-publisher"))
    fo = _text(fin.json())
    assert fo["status"] == "ready" and fo["bytes"] == 10 and fo["sha256"]

    # 3. list now shows it ready
    ls = client.post("/mcp/platform-publisher",
                     json={"id": 3, "method": "tools/call",
                           "params": {"name": "list_datasets", "arguments": {"provider_id": "edgar-rag"}}},
                     headers=_owner_hdr("platform-publisher"))
    assert _text(ls.json())["datasets"][0]["status"] == "ready"

    # 4. delete removes bytes + metadata
    dl = client.post("/mcp/platform-publisher",
                     json={"id": 4, "method": "tools/call",
                           "params": {"name": "delete_dataset",
                                      "arguments": {"provider_id": "edgar-rag", "name": "vectors"}}},
                     headers=_owner_hdr("platform-publisher"))
    assert _text(dl.json())["deleted"] is True


def test_upload_rejects_non_owner(client):
    r = client.post("/mcp/edgar-rag/upload/vectors", content=b"x",
                    headers=_tok("edgar-rag", "stranger", []))
    assert r.status_code == 401


def test_upload_rejects_undeclared_artifact(client):
    r = client.post("/mcp/edgar-rag/upload/not_declared", content=b"x",
                    headers=_tok("edgar-rag", "StanislavBG", []))
    assert r.status_code == 400 and "not declared" in r.json()["error"]
