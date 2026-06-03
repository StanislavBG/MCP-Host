"""FastAPI app — the HTTP adapter over the Gateway, plus storefront / inspector / admin.

This is the only place transport lives. It:
  - mounts pilot providers at boot (lifespan) and seeds default entitlements,
  - routes POST /mcp/{provider} through Gateway.handle (Streamable HTTP JSON-RPC),
  - serves /mcp/{provider}/health, /mcp/{provider}/.well-known/mcp.json, /mcp/{provider}/data,
  - exposes the artifact upload API (HMAC bearer), /admin (usage), /inspector, and the index.

Run: uvicorn mcp_host.server:app --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import asynccontextmanager

logger = logging.getLogger("mcp-host")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from mcp_host import __version__
from mcp_host.artifacts.store import ArtifactStore, verify_upload_auth
from mcp_host.auth.principal import verify_token
from mcp_host.billing.x402 import StubFacilitator
from mcp_host.data.factory import make_backends
from mcp_host.data.store import Entitlement
from mcp_host.gateway.router import Gateway, GatewayConfig
from mcp_host.registry.serverjson import to_server_json
from mcp_host.registry.tdqs import passes
from mcp_host.sdk import ToolError

BASE_URL = os.environ.get("MCP_HOST_BASE_URL", "https://mcp-host")
SIGNING_KEY = os.environ.get("MCP_HOST_SIGNING_KEY", "dev-signing-key")
WALLET = os.environ.get("WALLET_ADDRESS", "0xSHARED")
ADMIN_KEY = os.environ.get("UPLOAD_SECRET", "")
ARTIFACT_ROOT = os.environ.get("MCP_HOST_ARTIFACTS", "/tmp/mcp-host-artifacts")
# Principal id of the platform super-admin: may wield any provider's :admin scope and upload
# any provider's artifacts. Per-provider ownership lives in each provider.json `owner` field.
PLATFORM_OWNER = os.environ.get("MCP_HOST_PLATFORM_OWNER", "StanislavBG")
# Providers whose scopes are authorized per-target IN-BODY (not by the plan-entitlement table),
# so their scopes are granted to every plan and the tool body enforces ownership.
OWNER_MANAGED_PROVIDERS = {"platform-publisher"}

# Default entitlement matrix: free plan gets read scopes broadly; paid scopes go to 'pro'.
DEFAULT_PLANS = {
    "free": {"quota": 1000, "rate": 120, "scopes_suffix": (":read",)},
    "pro": {"quota": 100000, "rate": 600, "scopes_suffix": (":read", ":search", ":write", ":subscribe")},
}


def mask_ip(ip: str) -> str:
    parts = ip.split(".")
    return ".".join(parts[:2] + ["x", "x"]) if len(parts) == 4 else "x"


def _build_id() -> str:
    """Short id of the running code so /health reveals exactly what's deployed. Reads the git
    short SHA if a checkout is present (the preferred sync path keeps .git); otherwise falls back
    to MCP_HOST_BUILD or the package version (covers the zip-sync path that has no .git)."""
    try:
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        head = open(os.path.join(repo, ".git", "HEAD")).read().strip()
        ref = head.split(" ", 1)[1] if head.startswith("ref:") else head
        sha = open(os.path.join(repo, ".git", ref)).read().strip() if head.startswith("ref:") else ref
        return sha[:7]
    except Exception:
        return os.environ.get("MCP_HOST_BUILD", __version__)


BUILD_ID = _build_id()


def build_gateway() -> Gateway:
    store, tenant = make_backends()
    gw = Gateway(store, GatewayConfig(BASE_URL, SIGNING_KEY, WALLET, ADMIN_KEY, PLATFORM_OWNER),
                 facilitator=StubFacilitator(), tenant=tenant)
    from providers import load_pilots

    for provider, secrets in load_pilots():
        gw.mount(provider, secrets)
        owner_managed = provider.id in OWNER_MANAGED_PROVIDERS
        # Seed entitlements per declared scope across plans. :admin scopes are NEVER seeded —
        # the gateway authorizes them by ownership, not plan. Owner-managed providers grant their
        # (non-admin) scopes to every plan because the tool body enforces per-target ownership.
        for scope in provider.manifest["auth"]["scopes"]:
            if scope.endswith(":admin"):
                continue
            for plan, cfg in DEFAULT_PLANS.items():
                if scope.endswith(cfg["scopes_suffix"]) or owner_managed:
                    store.set_entitlement(Entitlement(plan, provider.id, scope,
                                                      cfg["quota"], cfg["rate"]))
    return gw


def preflight() -> list[str]:
    """Loud config check at boot. Returns the list of problems (also logged).

    These are the misconfigurations that silently break a first deploy: dev signing key (anyone
    could mint tokens), wrong/default public URL (OAuth resource-indicator + .well-known/server.json
    break), no Postgres (in-memory SQLite that resets every restart), default wallet, ephemeral
    artifact dir.
    """
    problems: list[str] = []
    if SIGNING_KEY == "dev-signing-key":
        problems.append("MCP_HOST_SIGNING_KEY is the dev default — set a strong secret (tokens are forgeable otherwise)")
    if BASE_URL == "https://mcp-host":
        problems.append("MCP_HOST_BASE_URL is the placeholder — set it to your real public URL "
                        "(OAuth resource indicators, .well-known and server.json all derive from it)")
    if not os.environ.get("DATABASE_URL", "").startswith("postgres"):
        problems.append("DATABASE_URL is not a Postgres URL — running on in-memory SQLite that RESETS "
                        "on every restart. Add Replit Postgres before deploying.")
    if WALLET == "0xSHARED":
        problems.append("WALLET_ADDRESS is the placeholder — set your shared wallet address")
    if not ADMIN_KEY:
        problems.append("UPLOAD_SECRET is empty — artifact upload and admin bypass are disabled")
    if ARTIFACT_ROOT.startswith("/tmp"):
        problems.append("MCP_HOST_ARTIFACTS points at /tmp (ephemeral) — set a persistent path on the VM")
    for p in problems:
        logger.warning("[preflight] %s", p)
    if not problems:
        logger.info("[preflight] configuration OK")
    return problems


@asynccontextmanager
async def lifespan(app: FastAPI):
    started_at = time.time()
    app.state.started_at = started_at
    app.state.preflight = preflight()
    gw = build_gateway()
    app.state.gw = gw
    app.state.artifacts = ArtifactStore(ARTIFACT_ROOT)
    # First-party platform providers get a live view of the host injected at boot.
    host_meta = {
        "version": app.version,
        "build": BUILD_ID,
        "base_url": BASE_URL,
        "backend": getattr(gw.store, "backend", "unknown"),
        "started_at": started_at,
        "config_warnings": app.state.preflight,
        "artifacts": app.state.artifacts,
        "artifact_root": ARTIFACT_ROOT,
        "platform_owner": PLATFORM_OWNER,
    }
    for p in gw.providers():
        if hasattr(p, "bind_host"):
            p.bind_host(gw, host_meta)
    logger.info("[boot] backend=%s mounted providers: %s",
                host_meta["backend"], [p.id for p in gw.providers()])
    yield


app = FastAPI(title="MCP-Host", version=__version__, lifespan=lifespan)


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
    problems = getattr(request.app.state, "preflight", [])
    backend = getattr(gw.store, "backend", "unknown")
    degraded = bool(problems) or "unreachable" in backend
    return {"status": "ok" if not degraded else "degraded",
            "version": __version__,
            "build": BUILD_ID,
            "providers": [p.id for p in gw.providers()],
            "config_warnings": problems,
            "backend": backend,
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


def _authorize_upload(headers: dict, provider, canonical_uri: str) -> bool:
    """Upload is allowed for the platform super-admin (UPLOAD_SECRET) OR the provider's owner
    (a bearer token resource-bound to this provider whose sub == the declared owner). Both checks
    are constant-time / signature-verified; we never trust a client-claimed identity."""
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if verify_upload_auth(auth, ADMIN_KEY):
        return True
    if auth.lower().startswith("bearer "):
        try:
            principal = verify_token(SIGNING_KEY, auth[7:].strip(), canonical_uri)
        except ToolError:
            return False
        owner = provider.manifest.get("owner")
        return bool(owner and principal.id == owner) or principal.id == PLATFORM_OWNER
    return False


@app.post("/mcp/{provider_id}/upload/{artifact}")
async def upload_artifact(provider_id: str, artifact: str, request: Request):
    """Owner-authenticated artifact upload (single-shot). The provider owner (or platform
    super-admin) pushes bytes for an artifact DECLARED in that provider's provider.json."""
    gw: Gateway = request.app.state.gw
    provider = gw.provider(provider_id)
    if not provider:
        return JSONResponse({"error": "unknown provider"}, status_code=404)
    if not _authorize_upload(dict(request.headers), provider, gw.canonical_uri(provider_id)):
        return JSONResponse({"error": "unauthorized — owner token or UPLOAD_SECRET required"}, status_code=401)
    declared = {a["name"] for a in provider.manifest.get("data", {}).get("artifacts", [])}
    if artifact not in declared:
        return JSONResponse(
            {"error": f"artifact '{artifact}' is not declared in {provider_id}'s provider.json"},
            status_code=400)
    kind = next(a.get("kind", "blob") for a in provider.manifest["data"]["artifacts"] if a["name"] == artifact)
    store: ArtifactStore = request.app.state.artifacts
    data = await request.body()
    nbytes = store.put(provider_id, artifact, data)
    gw.store.record_artifact(provider_id, artifact, kind, nbytes,
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
        demo = bool(p.manifest.get("demo", False))
        badge = ("<span class=demo>demo · sample data</span>" if demo
                 else "<span class=live>live</span>")
        cards += (f"<div class='card{' demo-card' if demo else ''}'><h2>{p.manifest['display_name']} {badge}</h2>"
                  f"<p>{p.manifest['summary']}</p>"
                  f"<p><small>{p.manifest['discipline']} · TDQS {score} {'✓' if ok else '✗'}</small></p>"
                  f"<p><small>tools: {tools}</small></p>"
                  f"<code>{BASE_URL}/mcp/{p.id}</code></div>")
    return (f"<html><head><title>MCP-Host — the iStore for MCPs</title>"
            f"<style>body{{font-family:system-ui;max-width:880px;margin:2rem auto}}"
            f".card{{border:1px solid #ddd;border-radius:8px;padding:1rem;margin:1rem 0}}"
            f".demo-card{{opacity:.75;border-style:dashed}}"
            f".demo,.live{{font-size:.6em;vertical-align:middle;padding:.15em .5em;border-radius:1em;color:#fff}}"
            f".demo{{background:#b8860b}}.live{{background:#2e7d32}}</style></head>"
            f"<body><h1>MCP-Host</h1><p>The iStore for MCPs — {len(gw.providers())} providers hosted.</p>"
            f"{cards}<p><a href='/inspector'>Inspector</a> · <a href='/admin/usage'>Usage</a></p></body></html>")
