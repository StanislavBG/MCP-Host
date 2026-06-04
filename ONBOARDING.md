# Onboarding an MCP onto MCP-Host — what the host needs from your repo

This is the contract a **provider dev-agent** follows to host an MCP and its independent data
on MCP-Host. Point your agent at this file. If your repo produces the five things in §1 and
passes `mcp-host validate`, the host can mount, bill, meter, and syndicate it. The host owns
transport, auth, billing, isolation, metering, and registration — **you write only tool logic,
a manifest, your schema migrations, and (optionally) data artifacts.**

---

## 1. What you must deliver (the deliverables checklist)

| # | Deliverable | Where | Validated by |
|---|---|---|---|
| 1 | `provider.json` manifest | repo root (or provider dir) | `schemas/provider.schema.json` + `mcp-host validate` |
| 2 | A `Provider` subclass with `@tool` methods | `provider.py` | boot-time reconcile (tools ↔ manifest) |
| 3 | Pydantic input model per tool (`extra="forbid"`, `max_length` on strings) | `provider.py` | request validation |
| 4 | Tenant schema migrations (only if you store relational data) | your repo, applied on deploy | RLS-scoped to `<your-schema>.*` |
| 5 | Data artifacts (only if you have vectors/blobs) + an upload step | object store via upload API | HMAC bearer auth |

You do **not** deliver: a web server, OAuth code, an x402/wallet integration, a session layer,
a rate limiter, an audit logger, or registry-publishing code. Those are the host's. Writing any
of them is a protocol violation (see `CLAUDE.md`).

---

## 2. The manifest is the single source of truth

Everything the host configures for you is derived from `provider.json`: your route
(`/mcp/<id>`), OAuth scopes, the per-tool price map, your Postgres schema + RLS policy, rate
limits, the registry `server.json`, and your storefront listing. **Do not hardcode any of those
anywhere else.** Minimum shape (see `schemas/provider.schema.json` for the full spec, and
`providers/edgar_rag/provider.json` for a complete real example):

```jsonc
{
  "id": "your-mcp",                       // url-safe; becomes /mcp/your-mcp and your schema
  "display_name": "Your MCP",
  "discipline": "your-discipline",
  "version": "0.1.0",
  "summary": ">= 40 chars: what it does (scored by TDQS)",
  "owner_namespace": "io.github.YOURNAME",// for registry namespacing (verify ownership once)
  "transport": "streamable-http",
  "auth": { "modes": ["oauth2.1","api_key"], "scopes": ["your-mcp:read","your-mcp:write"] },
  "data": { "postgres_schema": "your_mcp", // underscores only
            "artifacts": [{ "name": "vectors", "kind": "lancedb", "max_gb": 5 }] },
  "tools": [
    { "name": "do_thing", "scope": "your-mcp:read", "price_usdc": "0.00",
      "description": ">= 40 chars stating what it does + constraints",
      "annotations": { "readOnlyHint": true } }
  ],
  "limits": { "rate_per_min": 60, "max_request_kb": 50 },
  "syndication": { "official_registry": true, "glama": true, "mcp_so": true, "pulsemcp": true }
}
```

Rules the validator enforces: every `tools[].scope` must be declared in `auth.scopes`; every
`@tool` in code must match a `tools[]` entry (no drift); `postgres_schema` is `[a-z_]` only;
descriptions should clear the TDQS gate (0.6) or the provider is **not deployable**.

---

## 3. The provider code (write only this)

```python
from pydantic import BaseModel, ConfigDict, Field
from mcp_host.sdk import Provider, tool

class DoThingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    q: str = Field(min_length=1, max_length=200)

class YourMcpProvider(Provider):
    manifest_path = "provider.json"

    @tool("do_thing", input_model=DoThingInput)
    def do_thing(self, ctx, q: str):
        # By the time you run, the call is authenticated, authorized, and (if priced) billed.
        # ctx.principal    — verified caller (id, plan, scopes)
        # ctx.tenant_db    — RLS-scoped handle to YOUR schema only (create_table/insert/query)
        # ctx.artifacts    — read-only access to YOUR uploaded artifacts
        # ctx.secret(name) — a third-party secret the host injected (you never read env)
        return ctx.json_text({"result": q})
```

Override `health(self, ctx)` and `catalog(self, ctx)` to self-describe readiness and data.
Raise `ToolError(ErrorCode.X, "msg")` for clean errors; uncaught exceptions are contained and
metered as `INTERNAL_ERROR`.

---

## 4. Your independent data — two homes, both isolated

- **Relational data → your Postgres schema `<id>.*`.** Reach it only through `ctx.tenant_db`.
  In production this is a real Postgres schema with an RLS policy
  `tenant_id = current_setting('app.tenant_id')`; the gateway sets `app.tenant_id=<your-id>` per
  request, so you physically cannot read another provider's rows. Ship migrations in your repo;
  they run on `deploy`. (Pilots: signal-builder's `panel_history` → `signal.*`, trader outcomes
  → `trader.*`.)
- **Vectors / blobs → artifacts.** Never put these in Postgres and never write them to the VM
  disk as source of truth. Push them through the authenticated upload API; the host mounts them
  read-only at `ctx.artifacts`. (Pilot: edgar-rag's LanceDB `vectors`.)

Upload (single-shot; chunked variant adds `X-Chunk-*` headers, mirroring edgar-rag):

```
curl -X POST https://<host>/mcp/<id>/upload/<artifact-name> \
     -H "Authorization: Bearer $UPLOAD_SECRET" --data-binary @vectors.bin
```

---

## 4a. Keeping relational data fresh — owner ingest (the owner-write-tool pattern)

Artifacts are for large vector/blob data. For **small, high-frequency rows** an off-platform owner
needs to keep current (e.g. live trade signals refreshed on every fill), declare an **owner-only
write tool** scoped `<ns>:admin`. The gateway authorizes `:admin` scopes by **ownership** — only
the provider's declared `owner` (or the platform super-admin) can call them — so no consumer plan
can ever reach the tool. The tool body writes through `ctx.tenant_db` like any other; it just runs
through the normal authenticated, audited, metered `tools/call` path (no new endpoint, no new auth).

`social-trader` is the worked example. Manifest:

```jsonc
"auth": { "modes": ["oauth2.1","api_key"], "scopes": ["trader:read","trader:subscribe","trader:admin"] },
"tools": [
  { "name": "signals.ingest", "scope": "trader:admin", "price_usdc": "0.00",
    "description": "Owner-only: replace or append the live signal/position rows this MCP publishes…",
    "annotations": { "readOnlyHint": false, "idempotentHint": true } }
]
```

Tool body (abridged — see `providers/social_trader/provider.py`): validate each row through a
Pydantic model, then `mode="replace"` does `tenant_db.delete(dataset)` + insert; `"append"` inserts.
The read tools serve `ctx.tenant_db` and fall back to a static seed only while no rows exist.

Refresh from cron / a post-fill hook with the CLI:

```bash
# 1. obtain a bearer resource-bound to your provider (self-host/dev issuer;
#    in production your OAuth 2.1 AS issues this — the gateway checks are identical).
export MCP_HOST_SIGNING_KEY=...                       # the host signing secret
TOKEN=$(mcp-host token --provider social-trader --sub StanislavBG --scopes trader:admin \
                       --base-url https://<host>)

# 2. push rows (replace the whole live set on each fill, or --mode append)
mcp-host ingest social-trader signals ./signals.json --base-url https://<host> --token "$TOKEN"
#   signals.json: a JSON array of rows, or { "rows": [ ... ] }
#   add --dry-run to print the exact JSON-RPC request without sending.
```

Equivalent raw Streamable-HTTP call (what `ingest` POSTs):

```bash
curl -X POST https://<host>/mcp/social-trader \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"id":1,"method":"tools/call","params":{"name":"signals.ingest",
       "arguments":{"dataset":"signals","mode":"replace",
       "rows":[{"ticker":"HPE","side":"short","rationale":"…","exit_intent":"…",
                "ts":"2026-06-02T15:00:00+00:00","status":"OPEN"}]}}}'
```

`replace` is idempotent (safe to retry); `append` is not. Keep payloads under your manifest's
`max_request_kb`. Subscribers then read your live data through the existing `signals.feed`
(priced + `trader:subscribe`-gated), `signals.history`, and `portfolio.positions` tools.

---

## 5. Secrets — you declare names, the host holds values

Third-party credentials your tools need (e.g. `SEC_USER_AGENT`, `ALPACA_API_KEY`,
`ANTHROPIC_API_KEY`) are stored in the host secret store and injected into `ctx.secret(name)`.
Tell the host operator which names you need; never read `os.environ` for secrets and never
commit them. The shared **wallet** is the host's alone — you never see or set it. Pricing is
declared per tool in `price_usdc`; the host runs the x402 billing.

---

## 6. The lifecycle commands you run

```bash
mcp-host scaffold your-mcp        # generate a valid skeleton (manifest + provider.py)
mcp-host validate ./your-mcp      # schema check + tool/manifest reconcile + TDQS gate  (CI gate)
mcp-host tdqs ./your-mcp          # see per-tool quality breakdown and fix weak descriptions
mcp-host syndicate ./your-mcp     # preview server.json + registry targets + client install snippets
# then, against a running host (operator step):
#   deploy  -> mounts you, runs migrations, provisions <id>.* schema + RLS + artifact bucket
#   upload  -> push artifacts
#   live syndicate -> publish to the official registry (Glama/mcp.so/PulseMCP auto-ingest)
```

CI in your repo must run `mcp-host validate` and fail the build if it returns non-zero.

---

## 6a. Self-serve hosting — register and publish a declarative provider (no operator)

Everything above describes a **first-party / code** provider: a `Provider` subclass that runs
in-process, mounted by a host operator (`deploy`). There is a second path that needs **no
operator and no host code review** — a **declarative provider**, where *you* run the tool logic
on your own public HTTPS service and the host proxies calls to it. Your code never runs inside
the host process (which is why it can be fully self-serve next to the shared wallet).

**1. Register as an owner — get a one-time API key.**
```bash
mcp-host register --base-url https://<host>            # → { "owner_id": "usr_…", "api_key": "mch_sk_…" }
# or: curl -X POST https://<host>/register -d '{"display_name":"Acme"}'
```
Store the `api_key` immediately — it is shown once and stored only as a hash. It is your
credential for publishing.

**2. Write a declarative manifest.** Same `provider.json` as §2, with two differences: add a
top-level `backend`, and give each tool an inline `input_schema` (no Pydantic model — there is no
`provider.py`). `owner` is ignored on submit; the host binds it to *you* (the authenticated key),
so you can only publish under your own ownership.
```jsonc
{
  "id": "acme-quotes", "display_name": "Acme Quotes", "discipline": "market-data",
  "version": "1.0.0", "summary": "Real-time equity quotes …",
  "transport": "streamable-http",
  "auth": { "modes": ["api_key"], "scopes": ["acme:read"] },
  "data": { "postgres_schema": "acme_quotes" },
  "tools": [{
    "name": "quotes.get", "scope": "acme:read", "price_usdc": "0.00",
    "description": "Return the latest bid/ask quote for a ticker symbol from Acme's feed.",
    "annotations": { "readOnlyHint": true },
    "input_schema": { "type": "object", "properties": { "ticker": { "type": "string" } } }
  }],
  "backend": { "kind": "external-http", "endpoint": "https://api.acme.example/mcp" }
}
```

**3. Publish.** The host validates (schema + TDQS + SSRF guard on your endpoint), provisions your
`<id>.*` schema + RLS, mounts you, and seeds consumer entitlements — all in one call.
```bash
mcp-host publish ./provider.json --base-url https://<host> --api-key "$MCP_HOST_API_KEY"
# add --dry-run to print the request without sending
```

**4. Implement your endpoint.** For every `tools/call`, the host POSTs to your `backend.endpoint`:
```jsonc
// headers: X-MCP-Host-Signature (hex), X-MCP-Host-Timestamp, X-MCP-Host-Provider
{ "tool": "quotes.get", "arguments": { "ticker": "NVDA" },
  "provider": "acme-quotes",
  "principal": { "id": "usr_…", "plan": "pro", "scopes": ["acme:read"] } }
```
Verify the signature — it is `HMAC_SHA256(host_signing_key, "<timestamp>." + raw_body)` — to
trust the caller is the host, then return a **JSON object** (the tool result, e.g. the
`{payload, built_at, schema_version}` envelope). Rules the host enforces, all **fail-closed**:
your endpoint must be `https`, must not resolve to a private/loopback/link-local address
(re-checked at every call to defeat DNS rebinding), must answer within the timeout, with a 2xx
and a JSON object ≤ 256 KB — otherwise the caller gets `BACKEND_UNAVAILABLE` (502).

Auth, billing, metering, audit, scope/quota, and the storefront listing still run host-side
exactly as for a code provider — only the tool *body* lives on your service.

---

## 7. Definition of done (what "hosted" means)

Your MCP is hosted when, against a running MCP-Host:
1. `GET /mcp/<id>/.well-known/mcp.json` returns your spec-compliant server card.
2. `POST /mcp/<id>` answers `initialize` / `tools/list` / `tools/call` over Streamable HTTP with
   a resource-bound OAuth token.
3. Priced tools return `402` with an x402 challenge until paid; free tools just work.
4. Calls outside your plan's scope/quota return `403`/`429`.
5. Every call shows up in `GET /admin/usage` attributed to `(<id>, tool)`.
6. `mcp-host syndicate` produces a server.json that passes the official schema.

The three pilots in `providers/` are worked examples of all of the above.
