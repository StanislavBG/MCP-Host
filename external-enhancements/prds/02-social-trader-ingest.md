# PRD 02 — `social-trader` owner ingest tool + live-backed reads

## Problem
`providers/social_trader/provider.py` hardcodes `_SIGNALS` / `_PORTFOLIO` (2024 NVDA/TSLA/AAPL).
There is no owner path to push live rows, and the read tools can only ever serve placeholders.

## Change

### `provider.json`
- `version` → `0.2.0`.
- `auth.scopes` += `"trader:admin"` (owner-only; never seeded; gateway authorizes by ownership).
- New tool `signals.ingest` (scope `trader:admin`, price `0.00`, `idempotentHint`), description
  ≥ 40 chars so TDQS stays ≥ 0.6.

### `provider.py`
- Pydantic models (`extra="forbid"`, bounded strings):
  - `SignalRow{ticker, side, conviction?, rationale, exit_intent, ts, status, outcome_pct?}`
  - `PositionRow{ticker, side, weight, entry}`
  - `IngestInput{dataset: "signals"|"positions", mode: "replace"|"append"="replace",
    rows: list[dict] (1..200)}`
- `signals.ingest(ctx, dataset, mode, rows)`:
  - Owner enforcement is the gateway's `:admin` gate — the body trusts `ctx.principal`.
  - Re-parse each row through the dataset's row model (precise `VALIDATION_ERROR` per bad row).
  - `create_table(dataset, …)`; `replace` → `delete(dataset)` then insert; `append` → insert.
  - Return `{dataset, mode, ingested, total}`.
- Reads serve `ctx.tenant_db` when populated, else fall back to the static seed (storefront/demo
  stays honest). Internal columns (`tenant_id`) projected out:
  - `signals.feed` → live signals minus `outcome_pct`, newest-first, `limit`.
  - `signals.history` → signals incl. outcomes.
  - `portfolio.positions` → positions.

## Acceptance criteria
1. Owner (`sub == owner` / super-admin) can call `signals.ingest`; a non-owner gets
   `FORBIDDEN_SCOPE` (403) — no data written.
2. After `replace` ingest, `signals.feed`/`history`/`positions` return the ingested rows, not the
   static seed; a second `replace` overwrites (no accumulation).
3. `append` adds without clearing.
4. `signals.feed` stays priced ($0.05) + `trader:subscribe`-gated (unchanged); `feed` omits
   `outcome_pct`.
5. Empty tenant → reads return the static fallback (existing `test_trader_*` keep passing).
6. TDQS for `social-trader` stays ≥ gate; provider count unchanged (still 5).

## Tests (`tests/test_m4_6_providers.py`, owner via `mint_token(sub="StanislavBG")`)
- `test_trader_ingest_owner_then_reads_live` — ingest replace → feed/history/positions reflect it.
- `test_trader_ingest_denies_non_owner` — sub `u` → 403, reads still static.
- `test_trader_ingest_replace_overwrites` — two replaces; total == last batch size.
- `test_trader_feed_hides_outcome` — feed rows have no `outcome_pct`; history does.

## Risk
Low/medium. Read-tool fallback preserves all current assertions. New scope is `:admin` so it is
never seeded and can't widen any consumer plan.
