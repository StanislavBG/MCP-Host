# PRD 02 — Owner-authenticated self-serve deploy

## Problem
`Gateway.mount()` is only called at boot by a host operator (`load_pilots`). A registered owner
has no way to deploy their own provider, and nothing binds a submitted manifest's `owner` to the
principal actually submitting it.

## Change
`mcp_host/gateway/deploy.py`:
- `normalize_submitted_manifest(manifest, owner_id)` — returns a copy with `owner` **overwritten**
  by the authenticated principal (integrity guarantee: you can only publish under your own
  ownership) and `postgres_schema` derived from `id` (`-`→`_`) if absent.
- `publish_declarative_provider(gw, manifest, owner_id, signing_key, *, default_plans, resolver,
  gate, reserved_ids)` — the deploy pipeline, raising `ToolError` on any rejection:
  1. reject reserved id prefixes (`platform-`) → `FORBIDDEN_SCOPE`,
  2. reject id collisions → `INVALID_REQUEST`,
  3. require `backend.endpoint` (declarative only; no in-process guest code) → `INVALID_REQUEST`,
  4. build `ProxyProvider` (runs manifest validation + SSRF guard) → `VALIDATION_ERROR` on fail,
  5. enforce the TDQS gate → `VALIDATION_ERROR`,
  6. `gw.mount()` + `seed_entitlements()`.
- `seed_entitlements(store, provider, default_plans, owner_managed=False)` — factored out of
  `server.build_gateway` and reused there, so boot and self-serve seed identically.
- `load_declarative_providers(gw, signing_key, code_ids, resolver)` — at boot, re-mount every
  persisted provider that has a `backend.endpoint` and isn't a code provider. Fails closed: a
  now-invalid endpoint is skipped, not mounted.

`POST /providers` (server) — `x-api-key` → principal via `principal_for_key`; 401 if missing/invalid.
Calls `publish_declarative_provider`, maps `ToolError.http_status` to the response, 201 on success.

`mcp-host publish <manifest> --api-key [--dry-run]` + `build_publish_request` (pure, testable).

`server.build_gateway` now calls `load_declarative_providers` after `load_pilots` so self-served
providers survive restarts (they persist in `platform.providers.manifest_json`).

## Acceptance criteria
1. Publishing with a manifest claiming someone else's `owner` mounts it under the **authenticated**
   owner; `get_provider(id).manifest["owner"]` equals the caller.
2. Reserved-prefix id → 403; duplicate id → 400; non-declarative manifest → 400; weak TDQS → 400.
3. After publish, the provider appears in `/health` and consumer read scope is seeded for `free`.
4. Persisted declarative providers re-mount at boot; code providers are not double-mounted.

## Tests
- `test_m9_selfserve.py`: `test_publish_binds_owner_and_mounts_proxy`, `…_rejects_reserved_prefix`,
  `…_rejects_duplicate_id`, `…_rejects_non_declarative`, `…_enforces_tdqs_gate`,
  `test_load_declarative_providers_remounts_persisted_only`.
- `test_server_http.py`: `test_publish_requires_api_key`, `test_register_then_publish_lists_provider`,
  `test_publish_rejects_reserved_id`.
- `test_cli.py`: `test_cli_publish_dry_run_redacts_key`, `…_needs_api_key`, `test_build_publish_request_shape`.

## Risk
Medium. New authenticated write path that mounts providers at runtime. Bounded by: declarative-only
(no code), owner binding, reserved-id + collision guards, and the SSRF guard inherited from §03.
