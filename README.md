# MCP-Host — the iStore for MCPs

A single Replit-hosted control plane + runtime + storefront for a fleet of MCP servers from
disconnected disciplines. One gateway mounts every provider at `/mcp/<provider>`, sharing
auth (OAuth 2.1), billing (one x402 wallet), a Postgres data layer (per-provider RLS schemas),
metering, audit, and one-command registry syndication. Providers conform to a private
**Provider Protocol** (`provider.json` + the SDK `Provider` base) and get all of the above for
free.

- **Rules for providers:** `CLAUDE.md`
- **Deploy contract (Replit):** `replit.md`
- **Onboard a new MCP / what the host needs from your repo:** `ONBOARDING.md`
- **Protocol schema:** `schemas/provider.schema.json`
- **Design plan:** `~/.claude/plans/get-your-big-planning-lively-eich.md`

## Layout

```
mcp_host/
  sdk/            Provider base, @tool, ToolContext, ErrorCode, content helpers, manifest validator
  gateway/        Gateway orchestrator (auth → entitlement → billing → dispatch → metering)
  auth/           OAuth2.1-style token + API-key validation; entitlement engine
  billing/        shared x402 wallet, per-tool price map, fail-closed, admin bypass
  data/           SqliteStore control plane (Postgres/RLS in prod) + tenant-scoped TenantDB
  artifacts/      HMAC chunked upload store + read-only ArtifactView
  registry/       server.json generation, TDQS quality gate, syndication planner
  observability/  (admin/usage + inspector live in server.py)
  server.py       FastAPI app: /mcp/{provider}, health, .well-known, data, upload, admin, inspector, index
cli/main.py       mcp-host: scaffold / validate / tdqs / syndicate
providers/        the three pilots (edgar-rag, signal-builder, social-trader) — worked examples
schemas/          provider.schema.json
tests/            81 tests across all milestones
```

## Run locally

```bash
uv venv .venv && uv pip install -r deps-dev.txt
.venv/bin/python -m pytest                       # full suite
MCP_HOST_SIGNING_KEY=k WALLET_ADDRESS=0xSHARED UPLOAD_SECRET=admin \
  .venv/bin/uvicorn mcp_host.server:app --port 8080
# then: curl localhost:8080/health  ·  open localhost:8080/  ·  localhost:8080/inspector
```

## Provider CLI

```bash
.venv/bin/python -m cli.main scaffold my-mcp
.venv/bin/python -m cli.main validate providers/edgar_rag
.venv/bin/python -m cli.main syndicate providers/edgar_rag --base-url https://mcp-host
```

## Status

Milestones M0–M8 implemented and tested on a SQLite/stub-facilitator dev backend. Production
swaps three seams behind the same interfaces: `SqliteStore`→Postgres+RLS, `StubFacilitator`→the
real x402 HTTP client, and the local artifact dir→object storage. Nothing else changes.
