# MCP-Host — Operating Rules (authoritative)

MCP-Host is the control plane + runtime + storefront for a fleet of MCP servers ("the
iStore for MCPs"). Every MCP is a **provider**: a guest that MUST conform to the Provider
Protocol in exchange for shared services (auth, billing/wallet, database, syndication,
audit, discovery). The host owns auth, billing, data isolation, metering, and syndication.
Providers own only their tools, their `<provider>.*` Postgres schema, and their artifacts.

Pilot providers: `edgar-rag` (SEC EDGAR RAG — the reference implementation), `signal-builder`
(per-ticker social metrics/sentiment), `social-trader` (publishes buy/sell signals).

## Architecture in one breath

One Replit Reserved VM runs a single Python process: a **gateway** (control plane) that
mounts each provider as a sub-app at `/mcp/<provider>`. Shared Postgres (`platform.*`
control-plane + `<provider>.*` RLS-isolated schemas), shared object store for artifacts,
one shared x402 wallet. Transport is MCP Streamable HTTP + OAuth 2.1.

```
Internet ──HTTPS(Streamable HTTP + OAuth2.1)──► Gateway ──► /mcp/<provider> (mounted)
                                                  │
                                       Postgres (platform.* + <provider>.*)  +  Object store
                                       shared x402 facilitator (Base L2 / USDC)
```

## Non-negotiable rules for providers

- A provider is defined by ONE `provider.json` (validated against `schemas/provider.schema.json`).
  Routes, scopes, prices, the Postgres schema name, and rate limits are DERIVED from it —
  never hardcoded anywhere else.
- Subclass `mcp_host.sdk.Provider`. Implement tools as `@tool` methods. Do NOT implement your
  own transport, OAuth, billing, audit, or registry calls — the host provides them.
- Transport is MCP Streamable HTTP. JSON-RPC methods: `initialize` / `tools/list` / `tools/call`
  only. Long/streamed results go over SSE.
- All tool inputs are Pydantic models with `extra="forbid"` and a `max_length` on every string.
- Never validate tokens, set entitlements, or hold the wallet key. Trust `ctx.principal`; by
  the time your tool body runs, the gateway has already authenticated, authorized, and (if the
  tool is priced) billed the call.
- DB access only via `ctx.tenant_db` (RLS-scoped to your schema). Never reach into `platform.*`
  or another provider's schema. No string interpolation in SQL — parameterize.
- Large/vector data are **artifacts** pushed via the upload API — never write source-of-truth
  files to the VM disk outside your artifact mount. Never commit vectors, models, or secrets.

## Security (hard fails in review)

- No secrets in code or git. No `eval`/`exec`/`pickle`/`shell=True`.
- Never log query bodies (50-char preview max), wallet addresses, or payment headers. Mask IPs
  (first two octets).
- Paid tools fail CLOSED: if the x402 facilitator is unavailable and the price ≠ free, return 503.
- Constant-time compare (`hmac.compare_digest`) for all secret/HMAC checks.

## Billing

- SINGLE shared wallet (`WALLET_ADDRESS`) for ALL providers; shared x402 facilitator (Base L2,
  `eip155:8453`, USDC).
- Price is PER-TOOL, taken from `provider.json`. Default free-during-alpha: a price in
  `{"$0.00", "$0", "0", ""}` means free.
- Every `tools/call` is metered to `platform.usage(provider, tool, principal, ...)` so we get
  per-product P&L even though all funds land in one wallet.

## Lifecycle (the `mcp-host` CLI)

```
scaffold  →  validate  →  deploy  →  upload  →  syndicate
```
- `validate` runs the JSON-schema check + a TDQS tool-description quality gate. A provider that
  fails validate/TDQS is NOT deployed or listed.
- `deploy` mounts the provider, runs its migrations, provisions `<id>.*` schema + RLS policy,
  and allocates its artifact bucket.
- `syndicate` generates a spec-compliant `server.json` and publishes to the official MCP
  Registry (which Glama / mcp.so / PulseMCP auto-ingest), then emits client-install snippets.

## Conventions

- Python 3.11, FastAPI/Starlette, env-var config, structured JSON logs, enum `ErrorCode` + a
  consistent error envelope, ISO-8601 UTC timestamps with `+00:00`, full type hints,
  one-file-per-concern, YAGNI (abstract only on the 2nd reuse).
- Shared domain types every provider reuses: `ticker` (UPPERCASE symbol), `CIK`, the result
  envelope `{payload, built_at, schema_version}`, and the sentiment score shape.

## Boundaries (what each side owns)

| Concern | Owner |
|---|---|
| Transport, session, routing, OAuth, entitlements, wallet, metering, audit, registry | **Host** |
| Tool bodies, `<provider>.*` schema + migrations, artifacts, `provider.json` | **Provider** |

If you are an agent onboarding a new MCP: conform to this protocol (subclass `Provider`, write
`provider.json`), then run `mcp-host validate && mcp-host deploy <id> && mcp-host syndicate <id>`.
