"""M9 — self-serve platform: open registration, owner-authenticated deploy, and the declarative
(proxied) execution model. Verifies a third party can become an owner and publish a provider
whose tools run OFF-platform, with the host never executing guest code and the SSRF guard holding.
"""

from __future__ import annotations

import json

import pytest

from mcp_host.auth.registration import register_owner
from mcp_host.billing.x402 import StubFacilitator
from mcp_host.data.store import SqliteStore
from mcp_host.data.tenant import open_tenant_conn
from mcp_host.gateway.deploy import load_declarative_providers, publish_declarative_provider
from mcp_host.gateway.router import Gateway, GatewayConfig
from mcp_host.sdk import Principal, ToolContext
from mcp_host.sdk.errors import ErrorCode, ToolError
from mcp_host.sdk.manifest import ManifestError
from mcp_host.sdk.proxy import ProxyProvider, validate_external_endpoint

CFG = GatewayConfig("https://mcp-host", "k", "0xSHARED", "admin")
PLANS = {
    "free": {"quota": 1000, "rate": 120, "scopes_suffix": (":read",)},
    "pro": {"quota": 100000, "rate": 600, "scopes_suffix": (":read", ":write")},
}
PUBLIC = lambda host: ["93.184.216.34"]  # noqa: E731 — a public address; SSRF guard should pass


def _manifest(pid="acme-quotes", endpoint="https://api.acme.example/mcp", backend=True):
    m = {
        "id": pid,
        "display_name": "Acme Quotes",
        "discipline": "market-data",
        "version": "1.0.0",
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
        "limits": {"rate_per_min": 60, "max_request_kb": 50},
    }
    if backend:
        m["backend"] = {"kind": "external-http", "endpoint": endpoint}
    return m


def _gw():
    return Gateway(SqliteStore(), CFG, StubFacilitator(), tenant_conn=open_tenant_conn())


def _ctx(pid="acme-quotes"):
    return ToolContext(pid, Principal(id="caller", kind="user", plan="pro", scopes=("acme:read",)))


# ---- SSRF guard -----------------------------------------------------------
def test_endpoint_guard_rejects_non_https():
    with pytest.raises(ManifestError):
        validate_external_endpoint("http://api.acme.example/mcp", resolver=PUBLIC)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.0.0.5", "192.168.1.9", "169.254.169.254", "::1"])
def test_endpoint_guard_rejects_ip_literals(ip):
    with pytest.raises(ManifestError):
        validate_external_endpoint(f"https://[{ip}]/" if ":" in ip else f"https://{ip}/", resolver=PUBLIC)


@pytest.mark.parametrize("host", ["localhost", "metadata.google.internal"])
def test_endpoint_guard_rejects_local_hostnames(host):
    with pytest.raises(ManifestError):
        validate_external_endpoint(f"https://{host}/mcp", resolver=PUBLIC)


def test_endpoint_guard_rejects_host_resolving_to_private():
    with pytest.raises(ManifestError):
        validate_external_endpoint("https://sneaky.example/", resolver=lambda h: ["10.1.2.3"])


def test_endpoint_guard_accepts_public():
    validate_external_endpoint("https://api.acme.example/mcp", resolver=PUBLIC)  # no raise


# ---- ProxyProvider forwarding --------------------------------------------
def test_proxy_forwards_signed_request_and_returns_payload():
    captured = {}

    def transport(url, body, headers, timeout):
        captured.update(url=url, body=body, headers=headers)
        return 200, json.dumps({"payload": {"bid": 1.0}, "schema_version": "1"}).encode()

    p = ProxyProvider(_manifest(), "k", transport=transport, resolver=PUBLIC)
    out = p.call_tool(_ctx(), "quotes.get", {"ticker": "NVDA"})

    assert out == {"payload": {"bid": 1.0}, "schema_version": "1"}
    assert captured["url"] == "https://api.acme.example/mcp"
    assert "X-MCP-Host-Signature" in captured["headers"] and "X-MCP-Host-Timestamp" in captured["headers"]
    sent = json.loads(captured["body"])
    assert sent["tool"] == "quotes.get"
    assert sent["arguments"] == {"ticker": "NVDA"}
    assert sent["principal"]["id"] == "caller"


@pytest.mark.parametrize("transport", [
    lambda u, b, h, t: (_ for _ in ()).throw(OSError("conn refused")),  # transport raises
    lambda u, b, h, t: (500, b"oops"),                                  # non-2xx
    lambda u, b, h, t: (200, b"not json"),                              # bad body
    lambda u, b, h, t: (200, b"[1,2,3]"),                               # not an object
])
def test_proxy_fails_closed(transport):
    p = ProxyProvider(_manifest(), "k", transport=transport, resolver=PUBLIC)
    with pytest.raises(ToolError) as ei:
        p.call_tool(_ctx(), "quotes.get", {})
    assert ei.value.code == ErrorCode.BACKEND_UNAVAILABLE


def test_proxy_rechecks_ssrf_at_call_time():
    class Flip:
        ip = "93.184.216.34"

        def __call__(self, host):
            return [self.ip]

    r = Flip()
    p = ProxyProvider(_manifest(), "k", transport=lambda *a: (200, b"{}"), resolver=r)
    r.ip = "127.0.0.1"  # DNS rebinds to loopback after deploy-time validation passed
    with pytest.raises(ToolError) as ei:
        p.call_tool(_ctx(), "quotes.get", {})
    assert ei.value.code == ErrorCode.BACKEND_UNAVAILABLE


# ---- registration ---------------------------------------------------------
def test_register_owner_issues_usable_api_key():
    store = SqliteStore()
    reg = register_owner(store, "Acme Inc")
    assert reg.owner_id.startswith("usr_")
    got = store.principal_for_key(reg.api_key)
    assert got is not None and got[0] == reg.owner_id


def test_register_owner_keys_are_unique():
    store = SqliteStore()
    a, b = register_owner(store), register_owner(store)
    assert a.owner_id != b.owner_id and a.api_key != b.api_key


# ---- owner-authenticated deploy ------------------------------------------
def test_publish_binds_owner_and_mounts_proxy():
    gw = _gw()
    reg = register_owner(gw.store)
    # Manifest lies about ownership; publish must overwrite it with the authenticated principal.
    m = _manifest()
    m["owner"] = "someone-else"
    res = publish_declarative_provider(gw, m, reg.owner_id, "k", default_plans=PLANS, resolver=PUBLIC)

    assert res["owner"] == reg.owner_id and res["mounted"] is True
    p = gw.provider("acme-quotes")
    assert isinstance(p, ProxyProvider)
    assert gw.store.get_provider("acme-quotes").manifest["owner"] == reg.owner_id
    # consumer read scope was seeded for the free plan
    assert gw.store.get_entitlement("free", "acme-quotes", "acme:read") is not None


def test_publish_rejects_reserved_prefix():
    gw = _gw()
    with pytest.raises(ToolError) as ei:
        publish_declarative_provider(gw, _manifest("platform-evil"), "usr_x", "k",
                                     default_plans=PLANS, resolver=PUBLIC)
    assert ei.value.code == ErrorCode.FORBIDDEN_SCOPE


def test_publish_rejects_duplicate_id():
    gw = _gw()
    publish_declarative_provider(gw, _manifest(), "usr_x", "k", default_plans=PLANS, resolver=PUBLIC)
    with pytest.raises(ToolError) as ei:
        publish_declarative_provider(gw, _manifest(), "usr_x", "k", default_plans=PLANS, resolver=PUBLIC)
    assert ei.value.code == ErrorCode.INVALID_REQUEST


def test_publish_rejects_non_declarative():
    gw = _gw()
    with pytest.raises(ToolError) as ei:
        publish_declarative_provider(gw, _manifest(backend=False), "usr_x", "k",
                                     default_plans=PLANS, resolver=PUBLIC)
    assert ei.value.code == ErrorCode.INVALID_REQUEST


def test_publish_rejects_ssrf_endpoint():
    gw = _gw()
    with pytest.raises(ToolError) as ei:
        publish_declarative_provider(gw, _manifest(endpoint="https://evil.example/"), "usr_x", "k",
                                     default_plans=PLANS, resolver=lambda h: ["169.254.169.254"])
    assert ei.value.code == ErrorCode.VALIDATION_ERROR


def test_publish_enforces_tdqs_gate():
    gw = _gw()
    weak = _manifest("weak-prov")
    weak["tools"][0].pop("description")
    weak["tools"][0].pop("annotations")
    weak["tools"][0].pop("input_schema")
    with pytest.raises(ToolError) as ei:
        publish_declarative_provider(gw, weak, "usr_x", "k", default_plans=PLANS, resolver=PUBLIC)
    assert ei.value.code == ErrorCode.VALIDATION_ERROR


# ---- boot-time re-mount ---------------------------------------------------
def test_load_declarative_providers_remounts_persisted_only():
    store = SqliteStore()
    store.register_provider(_manifest())  # a persisted declarative provider from a prior run
    store.register_provider(_manifest("first-party", backend=False))  # a code provider: skip
    gw = Gateway(store, CFG, StubFacilitator(), tenant_conn=open_tenant_conn())

    mounted = load_declarative_providers(gw, "k", code_ids=["first-party"], resolver=PUBLIC)

    assert mounted == ["acme-quotes"]
    assert isinstance(gw.provider("acme-quotes"), ProxyProvider)
    assert gw.provider("first-party") is None
