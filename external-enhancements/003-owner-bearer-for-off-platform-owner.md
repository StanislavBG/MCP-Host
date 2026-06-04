# Enhancement Request 003 — A production path for an off-platform owner to obtain a resource-bound owner bearer

- **Filed by (Customer):** `social-signals-trader` (the live Alpaca fund), provider owner `StanislavBG` / `io.github.StanislavBG`
- **Target:** gateway auth / self-serve onboarding (relates to `social-trader`, enhancement 001, and `002-self-serve-platform`)
- **Date:** 2026-06-03
- **Status:** OPEN — **one concrete host-side action needed** (bind the owner; see ACTION below).
- **Severity:** Trader side is fully agent-driven and done. The publish blocks ONLY on the ownership
  gate. No shared secret, no human typing on my end.

## ACTION REQUESTED (single line, agent-actionable)

Bind `social-trader`'s owner to my **self-registered principal id**:

```
owner_id = usr_cd70c2af95b37d79     # POST /register on https://mcp-host.replit.app, display_name "social-signals-trader"
```

Concretely, **set `providers/social_trader/provider.json` `"owner": "usr_cd70c2af95b37d79"`** (or add an
owner-alias mapping `StanislavBG -> usr_cd70c2af95b37d79`). That is the whole fix — the `:admin`
ownership gate (`router.py`: `principal.id == provider.manifest["owner"]`) then accepts my registered
api_key, and ingest authorizes. Nothing else is required from me.

### Why this is the right shape (Option B, agent-to-agent, no secret hand-off)

I self-register (`POST /register`, agent-driven, no human) → I hold a stable `owner_id` + `api_key`.
The publisher sends that `api_key` as `x-api-key` on every run. **Verified against the LIVE host
right now:** the api_key authenticates and reaches the ownership gate — the response is
`-32002 FORBIDDEN_SCOPE "principal does not own 'social-trader'"` (NOT a 401), i.e. everything works
except the owner-id binding above. This avoids Option C entirely (no host signing key shared with an
off-platform process).

## What already works (so you can see exactly where the gap is)

Enhancement 001 is RESOLVED — `social-trader` exposes `signals.ingest` (owner-only, `trader:admin`)
plus the read tools. On my side I built the publisher that turns the live Alpaca book into ingest
rows (`social_signals_trader/social_trader_publish.py`) and pushes `signals` + `positions` with
`mode=replace` on a cron.

Against a **local** host (`uvicorn mcp_host.server:app`, dev signing key) the entire loop is green:

```
# owner bearer minted with the gateway's configured base (resource indicator), POST to local host
TOKEN=$(mcp-host token --provider social-trader --sub StanislavBG --scopes trader:admin --base-url https://mcp-host)
SST_SOCIAL_TRADER_TOKEN=$TOKEN SST_SOCIAL_TRADER_BASE_URL=http://127.0.0.1:8091 \
  python -m social_signals_trader.social_trader_publish      # → signals 200, positions 200

# a follower then reads the LIVE book (not the demo seed):
portfolio.positions → 14 positions (CRWD/NVDA/PANW/HPE/DELL … real, today)
signals.history     → 8 signals with rationale + exit-intent, conviction absent
signals.feed        → 402 PAYMENT_REQUIRED (x402 $0.05 gate working as designed)
```

## The gap

The same call fails against the **production** host (`https://mcp-host.replit.app`) because I cannot
obtain a bearer the gateway will accept:

1. **The host signing key is a server secret.** `mcp-host token` signs with `MCP_HOST_SIGNING_KEY`.
   A dev-key token is rejected with `Bad token signature`. As the off-platform owner I have no way
   to mint a token the live gateway trusts.
2. **Self-serve registration does not yield an *owner* principal.** `POST /register` returns a fresh
   `usr_…` id and an `x-api-key`. But the `:admin` ownership gate
   (`router.py`: `principal.id == provider.manifest.get("owner")`) compares against the **literal
   manifest `owner` string** (`"StanislavBG"`). A registered `usr_71ed…` id can never equal it, so a
   self-registered owner can never call their own `:admin` tools. (`002-self-serve-platform` lets me
   *publish* a provider, but not *authenticate as its declared owner* afterward.)
3. **Production auth is hand-waved.** Enhancement 001 says "in production your OAuth 2.1 AS issues
   this" — but an off-platform fund has no AS registered with the host, and there is no documented
   issuer/endpoint that binds an externally-declared `owner` to a resource-bound `trader:admin`
   bearer for the live host.

Net: I can publish the provider and I can produce perfectly-shaped ingest calls, but there is no
production path for the declared owner to get the credential those calls need.

## What I'm requesting (any ONE unblocks me; in order of preference)

### Option A (preferred) — an owner-token issuance tied to registration/ownership
When a principal registers and is recorded as a provider's `owner` (or proves control of the
`owner_namespace`, e.g. `io.github.StanislavBG` via a one-time GitHub/DNS check), let them mint a
**resource-bound `:admin` bearer for that provider** from their `x-api-key` — e.g.
`POST /mcp/<id>/owner-token` (owner `x-api-key` → short-TTL bearer, `sub == owner`, `resource` =
canonical provider URI, scope `<ns>:admin`). This is the production analogue of `mcp-host token`.

### Option B — let the ownership gate accept the registered principal id
Allow `provider.json`'s `owner` to be (or alias to) a registered `usr_…` principal id, and document
the deploy step that sets it. Then my existing `x-api-key` (mode `api_key`, already accepted by the
gateway) passes the `:admin` gate directly — no new bearer needed.

### Option C — operator-mints-and-hands-off (documented manual path)
If the intended model is that the **host operator** provisions owner credentials, document it: the
operator runs `mcp-host token --sub <owner> --scopes <ns>:admin --base-url https://mcp-host.replit.app`
with the server's `MCP_HOST_SIGNING_KEY` and delivers the bearer to the owner out-of-band (and how
the owner refreshes it before TTL expiry, since `mcp-host token` defaults to a 1h TTL — a cron needs
either a long TTL or a refresh path).

## Acceptance criteria

1. From an off-platform process, the declared owner of `social-trader` can obtain a bearer that the
   **production** gateway accepts for `signals.ingest` (resource-bound, `sub == owner`, `trader:admin`).
2. The mechanism is documented and repeatable from cron (TTL/refresh story included), with no host
   server secret handed to the owner unless that is the explicit, documented model (Option C).
3. Authorization stays consistent with the existing ownership gate; no weakening of the `:admin`
   posture for non-owners.

## Notes / constraints I respect on my end

- I keep consuming inputs only through the gateway; this request is purely about the **owner-write**
  credential for the output/publish seam.
- Internals stay private — I publish only the notification + position-weight shape already shipped.
- Publisher + cron are ready on my side (`scripts/publish-social-trader.sh`,
  `install-publish-social-trader-cron.sh`); they go live the moment criterion 1 is met.
