# MCP-Host on Replit — Deployment Contract

This file is the runtime/deployment contract for MCP-Host on Replit. It is authoritative for
how the platform boots, where data lives, and how providers are brought up. The Replit
environment and the GitHub repos are created AFTER the design is approved; this file tells the
agent that provisions them exactly what to build.

## Target

- **Reserved VM** deployment (always-on; no cold starts). A single Python 3.11 process serves
  the gateway + all mounted providers.
- Upgrade path (when one VM saturates): multiple Reserved VMs behind a load balancer +
  externalized session cache (Redis). The gateway is written stateless with session state in
  Postgres so this is a config change, not a rewrite.

## Run

- Listen on port **8080**, host **0.0.0.0**.
- Command:
  ```
  uvicorn mcp_host.server:app --host 0.0.0.0 --port 8080 \
    --timeout-keep-alive 120 --timeout-graceful-shutdown 30
  ```
- `PYTHONPATH` includes `.pythonlibs` (Replit's uv install dir) + workspace.
- A FastAPI lifespan hook warms embedding models and opens the Postgres pool before the first
  request, so cold first-calls are fast.

## Build (`install.sh` — always `exit 0`)

- `pip install --no-cache-dir -r deps.txt`; retry via `python3 -m pip` if an import fails.
- Download required models from the public HuggingFace CDN at build time.
- Pull provider artifacts (vectors/blobs) from object store / GitHub Releases (`GITHUB_TOKEN`)
  into each provider's read-only artifact mount.
- `pyproject.toml` keeps `dependencies = []` intentionally (tool config only — avoids uv
  auto-resolution conflicts). Runtime pins live in `deps.txt`; dev-only pins in `deps-dev.txt`;
  `replit.nix` pins `python311`.

## Data

- **Postgres** via `DATABASE_URL` (Replit Postgres or Neon). RLS enabled. The gateway sets
  `app.tenant_id=<provider>` per request so a provider sees only its own rows.
  Schemas: `platform.*` (control-plane) + one `<provider>.*` per provider.
- **Artifacts**: object storage, mounted read-only into providers. NEVER use the ephemeral
  container filesystem for source-of-truth data. Atomic swap + cache reload on new upload.

## Secrets (Replit Deployment Secrets → GCP Secret Manager)

- `WALLET_ADDRESS`, `X402_FACILITATOR_URL`, admin/upload key (`UPLOAD_SECRET`), `DATABASE_URL`,
  OAuth signing keys, and per-provider third-party secrets (`SEC_USER_AGENT`, `ALPACA_*`,
  `ANTHROPIC_API_KEY`, ...). Secrets are injected into the provider context at call time —
  providers never read raw env for secrets.
- Never echo secrets in logs or build output.

## Health / domains

- `GET /health` — liveness + per-provider readiness (incl. artifact/data presence).
- Custom domain + automatic TLS on a paid plan.
- Background workers (signal-builder curator, trader snapshot loop) run in-VM, each scoped to
  its own `<provider>.*` schema.

## Bring-up ordering (after GitHub repos exist)

1. Provision Postgres + object store.
2. Deploy the gateway.
3. `mcp-host deploy <id>` for each provider (mounts, migrates, provisions schema + RLS + bucket).
4. `mcp-host upload <id>` artifacts.
5. Smoke-test every provider via `/inspector`.
6. `mcp-host syndicate <id>` to the registries.
