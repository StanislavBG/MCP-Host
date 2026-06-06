---
name: ship-release
description: >-
  Ship a new MCP-Host version to the production Replit Reserved VM. Bumps the
  version, publishes to GitHub from local, syncs the Replit workspace via the
  Replit MCP, prompts the one manual Redeploy click, then verifies the live
  /health endpoint flipped to the new version. Use when asked to "ship",
  "release", "deploy", "publish a new version", "bump and deploy", or "push to
  prod" for MCP-Host. Project-specific to MCP-Host (Replit-hosted).
---

# ship-release — MCP-Host build & deploy pipeline

The validated pipeline for getting code live on the production Reserved VM
(`https://mcp-host.replit.app`). First proven on 2026-06-05: `0.4.1 → 0.4.3`,
backend confirmed `sqlite-file`.

## Hard facts about this deployment (why the steps are what they are)

- **GitHub is published from LOCAL only.** Replit's workspace **cannot push to
  GitHub** (HTTPS push is blocked from the Agent — it times out). So every
  `git push` happens from this local repo, never from the Replit side.
- **Replit zip-deploys the workspace as-is.** A push to GitHub has *zero* effect
  on the running deployment. The workspace files must be synced to the new code
  *before* deploying. `git fetch`/read from GitHub works in the workspace; only
  push is blocked.
- **Redeploy is a mandatory human click.** The Replit Agent can only *surface*
  the Publish button (`suggestDeploy()`); it cannot initiate or complete a
  deploy. Building → pushing to the VM → health-check is always user-initiated
  in the Deploy pane. There is no agent-callable deploy.
- **App identity (stable):** title `MCP Host`, `replId
  fc7604c9-477e-4138-a998-3a2aa24370f1`, `https://replit.com/@stanislavbg/MCP-Host`.
- **Prereq:** the Replit MCP must be authenticated (`/mcp` → replit →
  Authenticate). If `resolve_app_by_name` returns `Could not extract user ID
  from authorization token`, ask the user to re-auth before proceeding.

## Steps

### 1. Bump the version (local)
Edit `mcp_host/__init__.py` → `__version__`. This is the single source of truth;
`/health.version` and `build` both read it, so it's also the deploy's success
signal. Use semver: patch for fixes, minor for features.

### 2. Publish to GitHub (local)
```bash
git add mcp_host/__init__.py            # + any other changed files
git commit -m "…; vX.Y.Z"
git push origin enhancement-003-owner-bearer   # the branch the workspace deploys from
```
Note the new commit hash — you'll pass it to the Agent so it syncs to the right tip.

### 3. Resolve the app (Replit MCP)
```
mcp__replit__resolve_app_by_name("MCP Host")  → replId
```
(Skip if you already have `fc7604c9-477e-4138-a998-3a2aa24370f1`.)

### 4. Sync the workspace to the new code (Replit MCP)
`mcp__replit__update_app_using_prompt` with a **pure git-sync** instruction —
no code changes. Template:
> Pure git sync, do NOT write or modify any source code. Run `git fetch origin`,
> then make the workspace's working files exactly match the latest commit of
> GitHub branch `enhancement-003-owner-bearer` (tip is now commit `<HASH>`, which
> sets the version to `X.Y.Z`). Do not delete any local branches or commits —
> preserve history. Confirm `mcp_host/__init__.py` reads `__version__ = "X.Y.Z"`,
> then surface the Publish/Redeploy button (`suggestDeploy`). Report the
> workspace `__version__` and confirm the deploy button is ready.

> ⚠️ After calling `update_app_using_prompt`, the MCP forces your chat reply to be
> EXACTLY one sentence — so don't bundle other explanation into that turn.

The sync is a file-copy + Replit auto-checkpoint (it commits onto the workspace's
`main` rather than doing a clean checkout). That's fine — the **deploy uses files,
not git state.** The workspace git history drifts from GitHub over time; harmless
for deploying.

### 5. Manual redeploy (USER)
Tell the user: click **Publish / Redeploy** in the Replit Deploy pane. You cannot
do this step. Wait for them.

### 6. Verify via our own endpoint (the dogfood check)
Poll `/health` until `version` matches, in the background so the user can deploy
on their own clock:
```bash
for i in $(seq 1 80); do
  body=$(curl -s -m 10 https://mcp-host.replit.app/health 2>/dev/null)
  v=$(printf '%s' "$body" | python3 -c "import sys,json
try: print(json.load(sys.stdin).get('version',''))
except Exception: print('')" 2>/dev/null)
  if [ "$v" = "X.Y.Z" ]; then echo "DEPLOY OK vX.Y.Z"; printf '%s' "$body" | python3 -m json.tool; exit 0; fi
  sleep 30
done
echo "TIMEOUT (last: ${v:-unreachable})"; exit 1
```
Run with `run_in_background: true` — you're re-invoked on exit. Success criteria:
- `version` / `build` == the new version.
- `backend` starts with `sqlite-file` (durable; keys survive redeploy). If it
  reads `sqlite-memory`, the durable-storage config regressed — do NOT call it a
  success.

## Known follow-up: getting `status: "ok"`

As of the first run, `/health.status` is `"degraded"` even on a healthy deploy,
because Replit still injects a `DATABASE_URL` deployment secret → the host
attempts Postgres → fails (the `helium` host is unreachable from the Reserved VM)
→ falls back to a durable file labelled `sqlite-file (postgres unreachable)`, and
`_health_payload` flags any `"unreachable"` backend as degraded.

Durability is unaffected, but to flip `status` to `ok`: **remove the
`DATABASE_URL` secret from the deployment** (Deploy → Secrets) so the host never
attempts Postgres → backend becomes plain `sqlite-file` → not degraded. This is a
one-time deployment-secret change in the Replit UI (user action). See the
`replit-deploy-db-helium-gotcha` auto-memory.

## Gotchas log

- A local-only workspace commit `ae5c499` ("Published your App", an earlier
  DB-resilience patch) exists in the workspace and was never pushed to GitHub
  (push blocked). It's preserved in workspace history; don't `reset --hard` it
  away without first capturing its diff.
- Don't trust the Agent's "deployed!" language — only `/health` showing the new
  version proves the Reserved VM actually rebuilt.
