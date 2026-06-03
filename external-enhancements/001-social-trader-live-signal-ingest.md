# Enhancement Request 001 — Owner-facing live signal ingest for `social-trader`

- **Filed by (Customer):** `social-signals-trader` (the live Alpaca fund), provider owner `StanislavBG` / `io.github.StanislavBG`
- **Target provider:** `social-trader`
- **Date:** 2026-06-02
- **Status:** RESOLVED (2026-06-02) — see resolution note below.
- **Severity:** Blocking. Without this, the hosted `social-trader` MCP can only ever serve placeholder data.

## Resolution (platform)

Implemented **Option B** — an owner-only `signals.ingest` tool (scope `trader:admin`), authorized by
the gateway's existing ownership gate, writing your `trader.*` schema via `ctx.tenant_db`. Chosen over
a new ingest endpoint (Option A) because it reuses the host's authenticated/audited/metered
`tools/call` path — no new transport or auth surface — and fully satisfies the acceptance criteria.
Plan + PRDs: `external-enhancements/prds/`. Worked example: ONBOARDING.md §4a.

How each acceptance criterion is met:
1. **One authenticated call refreshes the set** — `POST /mcp/social-trader` `tools/call`
   `signals.ingest` with an owner bearer (resource-bound, `sub == owner`). CLI: `mcp-host ingest`.
2. **Reads return your data** — `signals.feed`/`signals.history`/`portfolio.positions` serve
   `ctx.tenant_db`, falling back to the static seed only while empty.
3. **Repeatable for cron** — `mcp-host token` + `mcp-host ingest` (with `--dry-run`); raw curl in §4a.
4. **Owner-only, no new provider secrets** — `:admin`-by-ownership; ingest body holds no secret.

Datasets supported: `signals` (backs feed + history) and `positions`. `mode: replace|append`.
The account-vs-SPY scoreboard row is deferred until a read tool exposes it (noted in PRD 00).

## Who I am and what I'm trying to do

I run an autonomous social-signals fund off-platform. It executes on a single Alpaca
account and already produces a clean, public-facing output surface locally: the
**Hedgefund MCP** (`hedgefund_mcp.py`), which emits trade notifications of the form
*"shorted/bought X of Y at <when> because …, intend to exit when …"* plus a single
total-account-vs-SPY scoreboard. Internals (strategy, conviction, sizing) stay private.

I want to **publish that real, live feed through MCP-Host** as the `social-trader`
provider, so paying subscribers (`signals.feed`, x402 $0.05) consume my actual
buy/sell/short signals — and I want it to **stay fresh** as fills happen on Alpaca
(intraday cadence), not a one-time load.

## The gap I hit (why current instructions are insufficient)

Following CLAUDE.md + ONBOARDING.md + the `mcp-host` CLI, I can `scaffold → validate →
deploy → syndicate`, but there is **no documented path for an off-platform owner to push
small, high-frequency signal rows into a hosted provider**:

1. **`social-trader` ships static placeholders.** `providers/social_trader/provider.py`
   hardcodes `_SIGNALS` / `_PORTFOLIO` (NVDA/TSLA/AAPL, dated 2024). A subscriber gets
   stale demo data forever.
2. **The only owner publishing path is artifact upload** (`platform-publisher` +
   `POST /mcp/<id>/upload/<artifact>`), which is explicitly designed for *large vector/blob
   artifacts* ("JSON-RPC is the wrong place for large vector blobs"). My data is the
   opposite: dozens of tiny rows that change on every fill.
3. **`social-trader/provider.json` declares no `data.artifacts`**, so the publisher's
   `_declared()` guard rejects any upload — there is nothing to publish into even if I
   wanted to misuse the artifact path.
4. **`ctx.tenant_db` (the `trader.*` schema) is writable only inside on-host tool bodies.**
   There is no owner-facing write tool or HTTP endpoint that lets my external process
   insert/replace signal rows. The CLI has no `ingest`/`feed`/`refresh` verb.

Net: I can deploy the shell, but I cannot get my live data into it or keep it current.

## What I'm requesting (any ONE of these unblocks me; listed in order of preference)

### Option A (preferred) — an owner-authenticated "push rows" ingest endpoint
A small-payload, owner-bearer-authenticated write path for **structured rows** (not blobs),
analogous to the artifact upload but for JSON records, e.g.:

```
POST /mcp/social-trader/ingest/signals
Authorization: Bearer <token resource-bound to social-trader, sub == owner>
Body: { "rows": [ {ticker, side, conviction, rationale, exit_intent, ts, status}, ... ],
        "mode": "replace" | "append" }
```

- Lands in the provider's `trader.*` schema (RLS-scoped), so existing read tools
  (`signals.feed`, `signals.history`, `portfolio.positions`) serve it unchanged.
- Owner-only, same authorization model as `platform-publisher` (caller must be the target
  provider's declared `owner`, or super-admin).
- Mirror it through `mcp-host` CLI as `mcp-host ingest social-trader signals <file.json>`
  so it can run from cron / a post-fill hook.

### Option B — a documented "owner-write tool" pattern
Bless and document a provider-defined tool scoped `trader:admin` (owner-only, per the
existing `:admin`-by-ownership gate) that accepts a batch of signal rows and writes them via
`ctx.tenant_db`. If this is *already* the intended mechanism, the gap is purely
**documentation**: please add a worked example to ONBOARDING.md showing an external owner
calling an `:admin` write tool over Streamable HTTP to refresh data, including how the owner
obtains the resource-bound bearer.

### Option C — extend `data.artifacts` to a `rowset`/`feed` kind
Add a first-class small-row artifact kind (e.g. `"kind": "rowset"`) that the publisher's
`finalize_upload` understands and that read tools can query, with a sane size/row cap and
intraday re-upload semantics (replace-in-place, not append-only blob versions).

## Data contract I will publish (for your design reference)

Per signal (small, ~10–50 rows live):
```
{ "ticker": "HPE", "side": "short", "conviction": <float 0..1 | omitted>,
  "rationale": "Short HPE into earnings — <evidence>",
  "exit_intent": "Intend to cover at the open on T+1; hard time-stop 10d; 5% daily-loss circuit.",
  "ts": "2026-06-02T...+00:00", "status": "OPEN|CLOSED", "outcome_pct": <float | null> }
```
Plus one performance row (total account vs SPY: day + since-inception alpha). Cadence:
on every fill (intraday) and an EOD refresh. Payload is well under the 50 KB
`max_request_kb` limit already in `provider.json`.

## Acceptance criteria

1. From an off-platform process holding only an owner bearer, I can refresh
   `social-trader`'s live signal set in one authenticated call.
2. `signals.feed` / `signals.history` / `portfolio.positions` then return **my data**, not
   the static `_SIGNALS` placeholder.
3. A documented, repeatable command/endpoint I can wire into a cron or post-fill hook so the
   feed stays current without redeploying the provider.
4. Authorization remains owner-only and consistent with the existing ownership model; no new
   secret handling on the provider side.

## Notes / constraints I will respect on my end

- I will keep consuming inputs only through the gateway (no direct data sources), per my own
  project contract — this request is purely about the **output/publish** seam.
- Internals stay private; I publish only the notification + scoreboard shape above.
