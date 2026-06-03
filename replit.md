# MCP-Host on Replit — first-boot deployment guide

Follow this top to bottom and the first deploy works. This is the infra + hosting layer; once
it's up, other agents publish their MCPs and data on top of it per `ONBOARDING.md`.

---

## 0. TL;DR ordered checklist

1. Import the GitHub repo `StanislavBG/MCP-Host` into a new Repl.
2. **Add Replit PostgreSQL** (Tools → Database). This auto-sets `DATABASE_URL`.
3. Set the **5 required Secrets** (§2).
4. **Deploy → choose "Reserved VM"** (NOT Autoscale). §3.
5. Open `/health` — `status` must be `"ok"` and `backend` must be `"postgres"`. §5.
6. Point client/agent tokens at your real URL and you're live. §6.

---

## 1. Deployment type — Reserved VM (required, not Autoscale)

Pick **Reserved VM** in the Deploy pane. `.replit` already sets `deploymentTarget = "gce"` to
match. Do **not** use Autoscale/Cloud Run: this is a single always-on stateful process — one
pooled DB connection, a local artifact directory, and (later) background workers — none of which
survive a scale-to-zero / multi-instance model. Smallest VM tier is fine to start.

- Run command and port are pinned in `.replit`: `uvicorn mcp_host.server:app` on `0.0.0.0:8080`,
  `--timeout-keep-alive 120`; `[[ports]]` maps 8080 → external 80. Don't change these.
- Build is `sh install.sh` (installs `deps.txt`, always exits 0). `pyproject.toml` keeps
  `dependencies = []` on purpose; runtime pins live in `deps.txt`.

---

## 2. Secrets (Deployments → Secrets)

`DATABASE_URL` is auto-provided by Replit Postgres — you do **not** set it. The production DB
starts empty; `PgStore` creates the `platform.*` schema automatically on first boot.

**Required (5) — the app warns at boot and on `/health` if any are missing/default:**

| Secret | What to set it to | Why it's required |
|---|---|---|
| `MCP_HOST_BASE_URL` | your real public URL, e.g. `https://<repl>.replit.app` (or your custom domain), **no trailing slash** | OAuth resource-indicator validation, `.well-known/mcp.json`, and the published `server.json` all derive from it. If wrong, agent tokens won't validate and install links point nowhere. |
| `MCP_HOST_SIGNING_KEY` | a long random secret | Signs/validates bearer tokens. The dev default makes tokens forgeable. |
| `WALLET_ADDRESS` | your shared receiving wallet (Base L2 / USDC) | The single platform wallet for all priced tools. |
| `UPLOAD_SECRET` | a long random secret | Bearer for the artifact upload API; also the admin/x402-bypass key. |
| `MCP_HOST_ARTIFACTS` | a persistent path on the VM, e.g. `/home/runner/workspace/objects` | Where uploaded vectors/blobs live. The default `/tmp` is ephemeral. (Object storage is a later swap.) |

**Per-provider third-party secrets (add the ones your mounted providers need):**
`SEC_USER_AGENT` (edgar-rag), `ALPACA_API_KEY` + `ANTHROPIC_API_KEY` (social-trader). These are
injected into each provider via `ctx.secret(...)`; providers never read env directly.

Never echo secrets in logs or build output.

---

## 3. Data

- **Postgres** (`DATABASE_URL`, auto-set): one DB, `platform.*` control plane + one
  `<provider>` schema per provider with Row-Level Security. The gateway sets `app.tenant_id`
  per request; a provider can only see its own rows. Backend is selected automatically —
  `postgres://…` → `PgStore`; unset → in-memory SQLite (dev only, resets on restart).
- **Artifacts**: large/vector/blob data is pushed via the authenticated upload API to
  `MCP_HOST_ARTIFACTS`, never committed to git, never the source-of-truth on `/tmp`.

---

## 4. Billing note for the first deploy (read this)

Billing runs on a **stub x402 facilitator** until the real x402 client is wired (a documented
later swap). Consequence: any tool priced `> $0.00` returns HTTP **402** and only accepts a
stubbed payment header — real USDC will not settle yet. For a clean alpha launch, keep all tool
prices at `"0.00"` in each `provider.json` (the pilots are free except `social-trader`'s
`signals.feed` at `$0.05` — set it to `0.00` for now unless you're demoing the 402 flow).
Metering still records every call to `platform.usage` regardless.

---

## 5. Verify the deploy

- `GET /health` → expect `{"status":"ok", "backend":"postgres", "config_warnings":[], "providers":[...]}`.
  The `providers` list MUST include the first-party `platform-health` and `platform-publisher`
  (plus the demo pilots). If it shows only `edgar-rag/signal-builder/social-trader`, the
  deployment is running STALE code — see §8.
  - `backend` of `"sqlite-memory (postgres unreachable)"` means the app fell back to EPHEMERAL
    storage (the host no longer crash-loops on a bad DB) — connect the production database (§3) and
    redeploy to get `"postgres"`. `status` is `"degraded"` whenever the backend is unreachable or
    any `config_warnings` are present.
- `GET /mcp/platform-health/health` → `{"status":"ok",...}`; this is our own monitoring MCP and the
  quickest end-to-end "is a hosted MCP serving?" check.
- `GET /` → storefront lists the mounted providers with TDQS scores; demos are badged "demo".
- `GET /mcp/platform-health/.well-known/mcp.json` → server card; confirm `remotes[].url` is your real domain.
- Optional, validate the live DB once from the Repl shell:
  `MCP_HOST_TEST_PG="$DATABASE_URL" python -m pytest tests/test_pg_backend.py`
  (runs the gated control-plane + cross-tenant RLS isolation tests against your real database).

---

## 6. Health / domains / workers

- Custom domain + automatic TLS: add it in the Deploy pane (paid plan), then set
  `MCP_HOST_BASE_URL` to that domain and redeploy.
- Background workers (signal-builder curator, trader snapshot) are not yet implemented; when they
  are, they run in this same Reserved VM scoped to their own `<provider>` schema.

---

## 7. After the host is up — onboarding other agents/MCPs

Tell each provider dev-agent: *"Conform to the MCP-Host Provider Protocol in `ONBOARDING.md`
(subclass `Provider`, write `provider.json` — including an `owner` principal id), pass
`mcp-host validate`, then deploy + upload + syndicate against this host."* The host gives them
transport, OAuth, the shared wallet, RLS data isolation, metering, and registry syndication for
free. Their independent data lives in their own `<provider>` Postgres schema (relational) and
their own artifact bucket (vectors/blobs), uploaded owner-only via `platform-publisher` +
`POST /mcp/<id>/upload/<artifact>`.

---

## 8. Updating a running deployment (IMPORTANT — the workspace is not a git checkout)

The Repl workspace was seeded from a one-time GitHub zip (Replit blocks `git clone` over HTTPS),
so it is **loose files, not a clone of `main`**. Pushing to GitHub does NOT update the
deployment — a redeploy just rebuilds whatever is in the workspace. To ship new commits you must
first sync the workspace to `main`, then redeploy. In the Repl **Shell**:

```sh
git -C ~/workspace fetch origin && git -C ~/workspace reset --hard origin/main 2>/dev/null \
 || { curl -fsSL https://codeload.github.com/StanislavBG/MCP-Host/zip/refs/heads/main -o /tmp/mh.zip \
      && unzip -oq /tmp/mh.zip -d /tmp/mh \
      && cp -rf /tmp/mh/MCP-Host-main/. ~/workspace/ && echo "synced to main"; }
```

Then click **Redeploy** and re-run the §5 checks (the `providers` list must include
`platform-health` and `platform-publisher`). This also replaces any ad-hoc in-workspace edits with
the repo version, keeping git the source of truth.
