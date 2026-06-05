# Request 001 — Durable registration / API-key store (production runs in-memory)

- **Filed by (Customer):** `social-signals-trader` (the live Alpaca fund), provider owner `StanislavBG` / `io.github.StanislavBG`
- **Target:** MCP-Host platform (gateway storage backend), affecting provider `social-trader`
- **Date:** 2026-06-04
- **Status:** RESOLVED (v0.4.2) — pending production redeploy
- **Severity:** Blocking. The hosted `social-trader` feed cannot be kept fresh by an unattended cron — every push 401s — because registered API keys do not survive a redeploy.

## Symptom

My `*/10` publish cron (`scripts/publish-social-trader.sh` → `social_trader_publish.py`)
gathers the live book and POSTs `signals.ingest` (+ `positions`) with the API key persisted
at registration (`~/.config/social-signals-trader/mcp-host.json`, sent as `x-api-key`).
Both datasets come back:

```
401  {"error":{"code":-32001,"message":"Invalid API key","data":{"code":"UNAUTHENTICATED"}}}
```

The key is well-formed and was issued by `POST /register` on this same host. The host simply
no longer recognizes it.

## Root cause (confirmed against production)

`GET https://mcp-host.replit.app/health` (v0.4.1):

```json
{
  "status": "degraded",
  "backend": "sqlite-memory (postgres unreachable)",
  "config_warnings": ["MCP_HOST_ARTIFACTS points at /tmp (ephemeral) — set a persistent path on the VM"]
}
```

Per `mcp_host/data/store.py`, the store falls back to `:memory:` SQLite when `DATABASE_URL`
is not a reachable `postgres://` and `MCP_HOST_DB` is unset. The registration store
(`mcp_host/auth/registration.py` — "There is no recovery — losing it means re-registering")
therefore lives entirely in process memory. **Every redeploy / restart wipes all issued API
keys**, so any credential an off-platform customer persists is dead on the next bounce.
Re-registering only mints another key that dies on the following restart — there is no
durable credential a cron can rely on.

This is a host-side persistence problem, not a customer-side bug. My trader-side pipeline is
verified working: with the API key fresh it shapes and reaches the host correctly; the gather
step returns real data (most recent run: 50 signal rows + 17 position rows).

## Request / acceptance criteria

Make registration credentials durable across redeploys:

1. **Persistent store in production** — provision Postgres (set a reachable `DATABASE_URL`),
   **or** point `MCP_HOST_DB` at a persistent VM path (e.g. `/home/runner/workspace/mcp-host.db`),
   then redeploy so `GET /health` reports `"backend": "postgres"` (or a file-backed sqlite) and
   `"status": "ok"`.
2. **Survives redeploy** — an API key issued by `POST /register` still authenticates
   `tools/call signals.ingest` after a redeploy/restart, with no re-registration.
3. **Also set `MCP_HOST_ARTIFACTS`** to a persistent path while in there, to clear the
   companion `/tmp` ephemeral-artifacts warning.
4. (Follow-on, already tracked) once a key is durable, complete the owner-binding so the
   self-registered `owner_id` passes the `:admin` ownership gate on `signals.ingest`
   — see `external-enhancements/003-owner-bearer-for-off-platform-owner.md`. Steps 1–3 here
   are the prerequisite; without durable storage, 003 cannot be exercised at all.

## Verification I will run once resolved

```bash
curl -s https://mcp-host.replit.app/health | jq '.status, .backend'   # expect "ok", non-memory
scripts/bootstrap-social-trader-publish.sh                            # re-register once, persist key
scripts/publish-social-trader.sh                                      # expect signals/positions ok:true (200)
# then redeploy the host and re-run the publish WITHOUT re-registering → must still be 200
```

## Resolution (v0.4.2)

Host-side code fix: `mcp_host/data/factory.py` no longer defaults the SQLite fallback to
`:memory:`. When a persistent Replit workspace is present (or `MCP_HOST_DB` is set to a file),
the control-plane store is file-backed (`backend: "sqlite-file"`), so API keys issued by
`POST /register` survive a redeploy. `:memory:` remains only for local dev / tests.

- `make_backends()` falls back to a durable file (not memory) even when a configured Postgres is
  unreachable — the documented Reserved-VM case.
- `MCP_HOST_DB`, `MCP_HOST_TENANT_DB`, and `MCP_HOST_ARTIFACTS` are set on the deployment run
  command (`.replit`) to persistent workspace paths; the `/tmp` artifact warning clears.
- Preflight no longer warns when storage is durable; `/health` reports `status: ok`,
  `backend: "sqlite-file"`.

Operator step to close: redeploy the host, then run the verification in this ticket. Postgres is
NOT provisioned (unreachable from the Reserved VM); durable file-backed SQLite is the intended
production backend.
