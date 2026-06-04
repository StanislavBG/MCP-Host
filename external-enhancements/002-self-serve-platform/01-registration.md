# PRD 01 — Self-serve registration

## Problem
Every `owner` today is `StanislavBG` plus the super-admin env var. The principal/api-key tables
and `x-api-key` auth already exist, but nothing public mints them — so no third party can exist
as an owner. Missing piece: a front door.

## Change
- `mcp_host/auth/registration.py` — `register_owner(store, display_name="")` creates a fresh
  `usr_<hex>` principal (`create_principal`) and issues one API key `mch_sk_<urlsafe>`
  (`add_api_key`, stored SHA-256 only). Returns the raw key **once**. Key carries no scopes:
  a declarative owner's authority is by ownership, not seeded scopes.
- `POST /register` (open) — rate-limited per masked IP (`_allow_register`: 5/hour, in-memory
  sliding window; a flood brake, not billing-grade). Body `{display_name?}`. Returns
  `{owner_id, api_key, note}` with 201.
- `mcp-host register --base-url [--display-name]` — CLI wrapper that prints the JSON.

## Acceptance criteria
1. `register_owner` → `principal_for_key(api_key)` resolves to the new owner id.
2. Two registrations yield distinct owner ids and keys.
3. `POST /register` returns 201 with `owner_id` (`usr_`) + `api_key` (`mch_sk_`); 6th call from
   one IP within the window returns 429.
4. The raw key is never persisted (only its hash) and never logged.

## Tests
- `test_m9_selfserve.py::test_register_owner_issues_usable_api_key`, `…_keys_are_unique`.
- `test_server_http.py::test_register_returns_owner_and_one_time_key`, `…_rate_limited`.

## Risk
Low. Open endpoint is the only new attack surface; mitigated by per-IP rate limit + the fact a
bare principal can do nothing until it passes the §02 publish gate.
