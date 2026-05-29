"""FastAPI app — the HTTP adapter over the Gateway, plus storefront / inspector / admin.

This is the only place transport lives. It:
  - mounts pilot providers at boot (lifespan) and seeds default entitlements,
  - routes POST /mcp/{provider} through Gateway.handle (Streamable HTTP JSON-RPC),
  - serves /mcp/{provider}/health, /mcp/{provider}/.well-known/mcp.json, /mcp/{provider}/data,
  - exposes the artifact upload API (HMAC bearer), /admin (usage), /inspector, and the index.

Run: uvicorn mcp_host.server:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from mcp_host.artifacts.store import ArtifactStore, verify_upload_auth
from mcp_host.billing.x402 import StubFacilitator
from mcp_host.data.factory import make_backends
from mcp_host.data.store import Entitlement
from mcp_host.gateway.router import Gateway, GatewayConfig
from mcp_host.registry.serverjson import to_server_json
from mcp_host.registry.tdqs import passes

BASE_URL = os.environ.get("MCP_HOST_BASE_URL", "https://mcp-host")
SIGNING_KEY = os.environ.get("MCP_HOST_SIGNING_KEY", "dev-signing-key")
WALLET = os.environ.get("WALLET_ADDRESS", "0xSHARED")
ADMIN_KEY = os.environ.get("UPLOAD_SECRET", "")
ARTIFACT_ROOT = os.environ.get("MCP_HOST_ARTIFACTS", "/tmp/mcp-host-artifacts")

# Default entitlement matrix: free plan gets read scopes broadly; paid scopes go to 'pro'.
DEFAULT_PLANS = {
    "free": {"quota": 1000, "rate": 120, "scopes_suffix": (":read",)},
    "pro": {"quota": 100000, "rate": 600, "scopes_suffix": (":read", ":search", ":write", ":subscribe")},
}


def mask_ip(ip: str) -> str:
    parts = ip.split(".")
    return ".".join(parts[:2] + ["x", "x"]) if len(parts) == 4 else "x"


def build_gateway() -> Gateway:
    store, tenant = make_backends()
    gw = Gateway(store, GatewayConfig(BASE_URL, SIGNING_KEY, WALLET, ADMIN_KEY),
                 facilitator=StubFacilitator(), tenant=tenant)
    from providers import load_pilots

    for provider, secrets in load_pilots():
        gw.mount(provider, secrets)
        # Seed entitlements for each declared scope across plans.
        for scope in provider.manifest["auth"]["scopes"]:
            for plan, cfg in DEFAULT_PLANS.items():
                if scope.endswith(cfg["scopes_suffix"]):
                    store.set_entitlement(Entitlement(plan, provider.id, scope,
                                                      cfg["quota"], cfg["rate"]))
    return gw


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.gw = build_gateway()
    app.state.artifacts = ArtifactStore(ARTIFACT_ROOT)
    yield


app = FastAPI(title="MCP-Host", version="0.1.0", lifespan=lifespan)


@app.middleware("http")
async def audit_and_security(request: Request, call_next):
    t0 = time.perf_counter()
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Strict-Transport-Security"] = "max-age=31536000"
    try:
        gw: Gateway = request.app.state.gw
        gw.store.audit(request.method, request.url.path, None, response.status_code,
                       int((time.perf_counter() - t0) * 1000),
                       mask_ip(request.client.host if request.client else "x"))
    except Exception:
        pass
    return response


@app.get("/health")
async def health(request: Request):
    gw: Gateway = request.app.state.gw
    return {"status": "ok", "providers": [p.id for p in gw.providers()],
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())}


@app.post("/mcp/{provider_id}")
async def mcp_endpoint(provider_id: str, request: Request):
    gw: Gateway = request.app.state.gw
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"jsonrpc": "2.0", "id": None,
                             "error": {"code": -32700, "message": "Parse error"}}, status_code=400)
    headers = {k.lower(): v for k, v in request.headers.items()}
    res = gw.handle(provider_id, body, headers)
    return JSONResponse(res.body, status_code=res.status, headers=res.headers)


@app.get("/mcp/{provider_id}/health")
async def provider_health(provider_id: str, request: Request):
    gw: Gateway = request.app.state.gw
    p = gw.provider(provider_id)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    from mcp_host.sdk import Principal, ToolContext

    ctx = ToolContext(provider_id, Principal(id="health", scopes=()))
    return p.health(ctx)


@app.get("/mcp/{provider_id}/.well-known/mcp.json")
async def well_known(provider_id: str, request: Request):
    gw: Gateway = request.app.state.gw
    p = gw.provider(provider_id)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    return to_server_json(p.manifest, BASE_URL)


@app.get("/mcp/{provider_id}/data")
async def provider_data(provider_id: str, request: Request):
    gw: Gateway = request.app.state.gw
    p = gw.provider(provider_id)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    from mcp_host.sdk import Principal, ToolContext

    ctx = ToolContext(provider_id, Principal(id="catalog", scopes=()))
    return p.catalog(ctx)


@app.post("/mcp/{provider_id}/upload/{artifact}")
async def upload_artifact(provider_id: str, artifact: str, request: Request):
    """HMAC-bearer authenticated artifact upload (single-shot). Chunked variant adds X-Chunk-* headers."""
    if not verify_upload_auth(request.headers.get("authorization"), ADMIN_KEY):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    gw: Gateway = request.app.state.gw
    if not gw.provider(provider_id):
        return JSONResponse({"error": "unknown provider"}, status_code=404)
    store: ArtifactStore = request.app.state.artifacts
    data = await request.body()
    nbytes = store.put(provider_id, artifact, data)
    gw.store.record_artifact(provider_id, artifact, "blob", nbytes,
                             f"{ARTIFACT_ROOT}/{provider_id}/{artifact}")
    return {"status": "ok", "bytes": nbytes, "sha256": store.sha256(provider_id, artifact)}


@app.get("/admin/usage")
async def admin_usage(request: Request):
    gw: Gateway = request.app.state.gw
    return {"usage": gw.store.usage_summary()}


@app.get("/inspector", response_class=HTMLResponse)
async def inspector(request: Request):
    gw: Gateway = request.app.state.gw
    rows = "".join(
        f"<li><b>{p.id}</b> — {p.manifest['summary']} "
        f"<code>POST {BASE_URL}/mcp/{p.id}</code></li>" for p in gw.providers()
    )
    return f"<h1>MCP-Host Inspector</h1><p>Mounted providers:</p><ul>{rows}</ul>"


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    gw: Gateway = request.app.state.gw
    cards = ""
    for p in gw.providers():
        ok, score, _ = passes(p)
        tools = ", ".join(t["name"] for t in p.manifest["tools"])
        cards += (f"<div class=card><h2>{p.manifest['display_name']}</h2>"
                  f"<p>{p.manifest['summary']}</p>"
                  f"<p><small>{p.manifest['discipline']} · TDQS {score} {'✓' if ok else '✗'}</small></p>"
                  f"<p><small>tools: {tools}</small></p>"
                  f"<code>{BASE_URL}/mcp/{p.id}</code></div>")
    return (f"<html><head><title>MCP-Host — the iStore for MCPs</title>"
            f"<style>body{{font-family:system-ui;max-width:880px;margin:2rem auto}}"
            f".card{{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}}</style></head>"
            f"<body><h1>MCP-Host</h1><p>The iStore for MCPs — {len(gw.providers())} providers hosted.</p>"
            f"{cards}<p><a href='/inspector'>Inspector</a> · <a href='/admin/usage'>Usage</a></p></body></html>")
