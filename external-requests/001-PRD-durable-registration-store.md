# PRD — Durable registration / API-key store

- **Request:** `external-requests/001-durable-registration-store.md`
- **Customer:** `social-signals-trader` (live Alpaca fund), owner `StanislavBG` / `io.github.StanislavBG`
- **Affects:** MCP-Host gateway storage backend; provider `social-trader`
- **Author:** platform (Bilko)
- **Date:** 2026-06-04
- **Ships in:** v0.4.2
- **Status:** Implemented & tested (working tree); pending commit + production redeploy

---

## 1. Problem

An off-platform customer registers once via `POST /register`, persists the issued API key
(`~/.config/social-signals-trader/mcp-host.json`), and drives an unattended `*/10` cron that
POSTs `signals.ingest` / `positions` with `x-api-key`. Every push returns:

```
401  {"error":{"code":-32001,"message":"Invalid API key","data":{"code":"UNAUTHENTICATED"}}}
```

The key is well-formed and was issued by this host, but the host no longer recognizes it after
a redeploy/restart.

### Root cause

`mcp_host/data/factory.make_backends()` falls back to **in-memory** SQLite (`:memory:`) whenever
`DATABASE_URL` is not a reachable `postgres://` and `MCP_HOST_DB` is unset. The control-plane
store (`platform.api_keys`, `platform.principals`) therefore lives entirely in process memory, so
**every redeploy wipes all issued credentials**. Re-registering only mints another key that dies
on the next bounce — there is no durable credential a cron can rely on.

Production `GET /health` (v0.4.1) confirmed it:

```json
{ "status": "degraded",
  "backend": "sqlite-memory (postgres unreachable)",
  "config_warnings": ["MCP_HOST_ARTIFACTS points at /tmp (ephemeral) — set a persistent path on the VM"] }
```

### Constraint (why not "just provision Postgres")

Replit's auto-provisioned `DATABASE_URL` points at the workspace-local `helium` host, which is
**unreachable from the Reserved VM deployment** (separate network). So Postgres is not a viable
production backend here. The durable backend must be **file-backed SQLite on the persistent
workspace** (`/home/runner/workspace`), which survives redeploys on the Reserved VM.

## 2. Goals / Non-goals

**Goals**
- An API key issued by `POST /register` keeps authenticating `tools/call` after a redeploy, with
  no re-registration.
- `/health` reports `status: ok` and a durable, non-memory `backend`.
- A misconfigured / unreachable Postgres degrades gracefully to a durable file (never a crash
  loop, never silent ephemeral memory in production).
- Companion `/tmp` ephemeral-artifacts warning cleared.

**Non-goals**
- Provisioning a real managed Postgres (explicitly out — unreachable from the Reserved VM).
- Owner-binding of the self-registered `owner_id` against the `:admin` gate (criterion #4 of the
  request) — already shipped in commits `9c6dfe4` / `ee385f7`; this PRD is its prerequisite only.
- Multi-instance / horizontal scale (the host is a single stateful Reserved VM by design).

## 3. Acceptance criteria

1. **Durable by default in production.** With a persistent workspace present (or `MCP_HOST_DB` set
   to a file), the control-plane store is file-backed; `/health.backend == "sqlite-file"`,
   `status == "ok"`.
2. **Survives redeploy.** A key issued by `POST /register` still authenticates `signals.ingest`
   after a redeploy/restart, no re-registration.
3. **Persistent artifacts.** `MCP_HOST_ARTIFACTS` resolves to a workspace path; the `/tmp` warning
   is gone.
4. **Graceful Postgres degrade.** If `DATABASE_URL=postgres://…` is set but unreachable, the host
   falls back to a **durable file** (not memory, not crash) and labels the backend
   `sqlite-file (postgres unreachable)` (still surfaced as degraded so the operator notices).
5. **No fresh-VM crash.** A durable path whose parent dir does not yet exist must not crash boot —
   the dir is created on open.
6. **Dev/test unaffected.** With no workspace and no `MCP_HOST_DB`, the backend is `:memory:`
   (`durable=False`); existing suite stays green.

## 4. Design

Single source of truth for SQLite path selection in `mcp_host/data/factory.py`:

```
resolve_sqlite_path(env_var, filename) -> (path, durable)
  explicit env  -> (value, value != ":memory:")          # ":memory:" honored, non-durable
  REPLIT_DEPLOYMENT set OR /home/runner/workspace exists
                -> (workspace/<filename>, True)           # durable file
  otherwise     -> (":memory:", False)                    # local dev / tests
```

- `make_backends()` selects PgStore for `postgres://`; on connect failure (after PgStore retry)
  it degrades via `_sqlite_backends("sqlite-file (postgres unreachable)", "sqlite-memory (postgres unreachable)")`
  instead of crashing. With no `postgres://`, it uses `_sqlite_backends("sqlite-file", "sqlite-memory")`.
- `_sqlite_backends(durable_label, ephemeral_label)` resolves both `MCP_HOST_DB` and
  `MCP_HOST_TENANT_DB` and labels `store.backend` per durability.
- **Crash-safety:** `SqliteStore.__init__` and `open_tenant_conn` call `_ensure_parent_dir(path)`
  (`os.makedirs(parent, exist_ok=True)`, no-op for `:memory:`) so a durable path on a fresh VM
  can't fail boot with "unable to open database file".
- `server.py`: `_default_artifact_root()` mirrors the same workspace-vs-`/tmp` logic; preflight
  warns about ephemeral storage only when storage is genuinely non-durable (reuses
  `resolve_sqlite_path`), and `_health_payload` keeps marking any `"...unreachable"` backend as
  degraded.
- Removed dead `store.make_store()` (no callers) that still defaulted to `:memory:`, to prevent
  silently reintroducing the ephemeral default.

### Ops / config

`.replit` deployment run command and `.env.example` set durable workspace paths explicitly
(belt-and-suspenders with the code default), and `DATABASE_URL` is left **unset** in production:

```
MCP_HOST_DB=/home/runner/workspace/mcp-host.db
MCP_HOST_TENANT_DB=/home/runner/workspace/mcp-host-tenant.db
MCP_HOST_ARTIFACTS=/home/runner/workspace/objects
```

## 5. Files touched

| File | Change |
|---|---|
| `mcp_host/data/factory.py` | `resolve_sqlite_path`, durable-default `_sqlite_backends`, durable Postgres-unreachable fallback |
| `mcp_host/data/store.py` | `_ensure_parent_dir`, call it in `SqliteStore.__init__`; remove dead `make_store()`; docstring |
| `mcp_host/data/tenant.py` | `open_tenant_conn` creates parent dir |
| `mcp_host/server.py` | `_default_artifact_root()`; durability-aware preflight |
| `mcp_host/__init__.py` | version `0.4.1` → `0.4.2` |
| `.replit`, `.env.example` | durable workspace env defaults; `DATABASE_URL` unset guidance |
| `tests/test_durable_store.py` | new: path resolution, survive-reopen, missing-parent-dir, file backend label |
| `external-requests/001-…md` | status → RESOLVED + resolution notes |

## 6. Test plan

Automated (`.venv/bin/python3 -m pytest`): full suite **178 passed, 2 skipped** (pg-live), incl.
`tests/test_durable_store.py`:
- `test_resolve_path_prefers_workspace` — `REPLIT_DEPLOYMENT` → durable file path.
- `test_explicit_memory_is_not_durable` — explicit `:memory:` reported non-durable.
- `test_api_key_survives_reopen` — key issued into a file-backed store still authenticates after
  a fresh `SqliteStore` on the same file (the core guarantee).
- `test_durable_path_with_missing_parent_dir_does_not_crash` — fresh-VM path is created, not fatal.
- `test_make_backends_uses_file_when_configured` — `backend == "sqlite-file"`.

Production (operator, post-redeploy — from the request ticket):
```bash
curl -s https://mcp-host.replit.app/health | jq '.status, .backend'   # expect "ok", "sqlite-file"
scripts/bootstrap-social-trader-publish.sh                            # re-register once, persist key
scripts/publish-social-trader.sh                                      # signals/positions ok:true (200)
# redeploy, then re-run publish WITHOUT re-registering → must still be 200
```

## 7. Rollout

1. Commit on `enhancement-003-owner-bearer`.
2. Redeploy the Reserved VM (env now set in `.replit`; `DATABASE_URL` unset).
3. Run the production verification above; flip the ticket to fully closed once a key survives a
   real redeploy.

## 8. Risks

- **Workspace not actually persistent** for the chosen deploy target → keys still reset. Mitigation:
  `/health.backend` must read `sqlite-file` and the survive-redeploy step must pass before closing.
- **Single-file SQLite** is a single-VM scaling ceiling — acceptable; the host is intentionally one
  stateful Reserved VM. Revisit only if/when a reachable managed Postgres becomes available.
