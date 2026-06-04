# Quickstart — register and host your MCP on MCP-Host (self-serve)

**Audience:** a third-party builder/agent who wants their MCP hosted on MCP-Host without a host
operator. You will use the **declarative** model: *you* run your tool logic on your own public
HTTPS service, and the host proxies every call to it. The host runs **none of your code** — it
keeps owning transport, OAuth/api-key auth, billing, metering, audit, and your RLS-isolated
Postgres schema, and forwards only the tool *body* to your endpoint.

If you instead ship in-process Python (a `Provider` subclass), that's the first-party path in
`ONBOARDING.md §1–6` and needs a host operator. This quickstart is the self-serve path only.

> Field reference for every manifest key is `ONBOARDING.md §6a` and `schemas/provider.schema.json`.
> Platform rules you must respect are in `CLAUDE.md`.

---

## Prerequisites

1. **The live host base URL** — ask the host operator (e.g. `https://your-host.replit.app`). This
   quickstart calls it `$HOST`.
2. **A public HTTPS endpoint you control** — `https://...`, reachable from the internet, that is
   NOT a private/loopback/link-local address. This is where the host will POST your tool calls.
   (Plain `http://`, `localhost`, `10.x`, `192.168.x`, `169.254.x`, etc. are rejected by the SSRF
   guard, at publish time and again on every call.)

```bash
export HOST="https://your-host.replit.app"   # from the operator
```

---

## Step 1 — Register (get a one-time API key)

```bash
curl -sX POST "$HOST/register" -H 'Content-Type: application/json' \
  -d '{"display_name":"Social Signals Trader"}'
# → {"owner_id":"usr_xxxxxxxx","api_key":"mch_sk_........","note":"...shown once..."}
```

Save the `api_key` immediately — it is shown **once** and stored only as a hash. It is your
credential for publishing. (Open registration is rate-limited per IP.)

```bash
export MCP_HOST_API_KEY="mch_sk_........"
```

---

## Step 2 — Write a declarative `provider.json`

Differences from a first-party manifest: add a top-level `backend` pointing at *your* endpoint,
and give each tool an inline `input_schema` (there is no `provider.py`, so no Pydantic model).
You do **not** set `owner` — the host overwrites it with your authenticated principal, so you can
only ever publish under your own ownership.

```jsonc
{
  "id": "social-signals-trader",
  "display_name": "Social Signals Trader",
  "discipline": "social-trading-signals",
  "version": "1.0.0",
  "summary": "Publishes buy/sell trading signals derived from social sentiment per ticker.",
  "transport": "streamable-http",
  "auth": { "modes": ["api_key"], "scopes": ["signals:read"] },
  "data": { "postgres_schema": "social_signals_trader" },
  "tools": [
    {
      "name": "signals.latest",
      "scope": "signals:read",
      "price_usdc": "0.00",
      "description": "Return the latest buy/sell signal and confidence for a given ticker symbol.",
      "annotations": { "readOnlyHint": true },
      "input_schema": {
        "type": "object",
        "properties": { "ticker": { "type": "string" } },
        "required": ["ticker"]
      }
    }
  ],
  "backend": { "kind": "external-http", "endpoint": "https://your-service.example.com/mcp" },
  "limits": { "rate_per_min": 60, "max_request_kb": 50 }
}
```

Rules the publish step enforces (fix these before publishing or you'll get a `400`):
- **TDQS quality gate (≥ 0.6):** every tool needs a description of **≥ 40 characters**, at least
  one `annotations` hint (e.g. `readOnlyHint`), a namespaced `scope` (`area:verb`), and a non-empty
  `input_schema`. Run `mcp-host tdqs ./provider.json` locally to see the breakdown.
- **`id`** is URL-safe and **cannot start with `platform-`** (reserved for first-party providers),
  and must not collide with an existing provider.
- Every `tool.scope` must be listed in `auth.scopes`.

---

## Step 3 — Publish

```bash
# CLI:
mcp-host publish ./provider.json --base-url "$HOST" --api-key "$MCP_HOST_API_KEY"
#   add --dry-run to print the exact request without sending.

# or raw HTTP:
curl -sX POST "$HOST/providers" \
  -H "x-api-key: $MCP_HOST_API_KEY" -H 'Content-Type: application/json' \
  --data-binary @provider.json
# → 201 {"id":"social-signals-trader","owner":"usr_...","route":"$HOST/mcp/social-signals-trader",
#        "endpoint":"https://your-service...","mounted":true,"scopes":["signals:read"],"tdqs":1.0}
```

On rejection you get the standard error envelope with an HTTP status (`401` bad/missing key,
`400` schema/TDQS/non-declarative, `403` reserved id). Fix and re-run.

---

## Step 4 — Implement your endpoint

For every `tools/call`, the host sends your `backend.endpoint`:

```jsonc
// POST https://your-service.example.com/mcp
// headers:
//   Content-Type: application/json
//   X-MCP-Host-Provider:  social-signals-trader
//   X-MCP-Host-Timestamp: 1717459200
//   X-MCP-Host-Signature: <hex hmac, see security note>
{
  "tool": "signals.latest",
  "arguments": { "ticker": "NVDA" },
  "provider": "social-signals-trader",
  "principal": { "id": "usr_caller", "plan": "pro", "scopes": ["signals:read"] }
}
```

Your endpoint must return a **JSON object** — the tool result. Use the platform result envelope:

```json
{ "payload": { "ticker": "NVDA", "signal": "buy", "confidence": 0.78 },
  "built_at": "2026-06-03T12:00:00+00:00", "schema_version": "1" }
```

The host **fails closed** — your call surfaces to the caller as `BACKEND_UNAVAILABLE` (HTTP 502)
unless your endpoint:
- is reachable over **HTTPS at a public address** (re-checked every call — no DNS-rebinding to a
  private IP),
- responds **within ~10 s**,
- returns a **2xx** status,
- with a **JSON object** body **≤ 256 KB**.

### Security note (read this) — protecting your endpoint

The host includes `X-MCP-Host-Signature = HMAC_SHA256(host_signing_key, "<timestamp>." + raw_body)`
so you can confirm a call came from the host. **Today the host signs with its internal signing
key, which is not shared with owners**, so you cannot yet verify the signature. Until per-provider
signing secrets are issued (tracked follow-up), protect your endpoint for the pilot by:
- using an **unguessable endpoint URL** (include a long random path segment), and/or
- requiring your **own** secret header/query token that you bake into `backend.endpoint`, and
- only serving **non-sensitive, read-style** tools this way.

Do not expose privileged writes from a declarative endpoint until signature verification lands.

---

## Step 5 — Verify you're hosted

```bash
curl -s "$HOST/health" | jq '.providers'                                   # your id is listed
curl -s "$HOST/mcp/social-signals-trader/.well-known/mcp.json"             # your server card
# a consumer then calls your tools over Streamable HTTP with a token/key granting signals:read:
curl -sX POST "$HOST/mcp/social-signals-trader" -H "x-api-key: <consumer-key>" \
  -d '{"id":1,"method":"tools/call","params":{"name":"signals.latest","arguments":{"ticker":"NVDA"}}}'
```

You're hosted when `/health` lists your id, `.well-known/mcp.json` returns your card, and a
`tools/call` round-trips through the host to your endpoint and back. Every call is metered under
`(social-signals-trader, signals.latest)` in `GET /admin/usage`.

---

## Gotchas

| Symptom | Cause | Fix |
|---|---|---|
| `400` on publish, "TDQS below gate" | weak tool description / no annotations / no input_schema | `mcp-host tdqs ./provider.json`; descriptions ≥ 40 chars + a hint + params |
| `400` "must declare backend.endpoint" | manifest has no `backend` | add `backend: {kind:"external-http", endpoint:"https://..."}` |
| `400` "resolves to a private/loopback address" | endpoint not public HTTPS | use a real public `https://` host |
| `403` reserved prefix | `id` starts with `platform-` | pick another id |
| caller sees `BACKEND_UNAVAILABLE` 502 | your endpoint timed out / non-2xx / non-JSON / >256 KB | return a small JSON object quickly over 2xx |
| `401` on publish | missing/invalid `x-api-key` | re-register; keys are shown once |
