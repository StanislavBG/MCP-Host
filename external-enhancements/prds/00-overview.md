# Enhancement 001 — Implementation plan (PRD series)

Resolves `external-enhancements/001-social-trader-live-signal-ingest.md`.

## Decision: Option B (owner-gated `:admin` write tool)

The request offered three options. We implement **Option B** as the canonical mechanism:
a provider-defined tool scoped `trader:admin`, owner-only via the gateway's *existing*
`:admin`-by-ownership gate (`mcp_host/gateway/router.py:132`), that writes signal rows to the
provider's RLS-isolated `trader.*` tenant schema via `ctx.tenant_db`.

Why B over A (new `POST /ingest` endpoint) or C (`rowset` artifact kind):

- **Reuses the host's auth/transport.** CLAUDE.md forbids providers reimplementing transport,
  OAuth, billing, audit. Option A adds a second owner-auth HTTP path to security-review; B flows
  through the one audited+metered `tools/call` hot path that already authenticates, authorizes
  by ownership, and meters every call.
- **No new primitives the schema can't express.** B is just a tool in `provider.json` + a tenant
  write. The `:admin` gate, ownership model, and `tenant_db` already exist and are tested.
- **Satisfies every acceptance criterion.** One authenticated `tools/call` from an off-platform
  process (owner bearer, resource-bound to `/mcp/social-trader`) refreshes the live set; reads
  then serve that data; repeatable from cron; authorization stays owner-only.

The request itself notes B "may already be the intended mechanism" and that the gap would then be
"purely documentation." It was *almost* the intended mechanism — the missing pieces are: a tenant
**replace** primitive, the **tool itself** + live-backed reads, a **CLI seam** for cron, and the
**docs**. Those are the four PRDs below.

## Sequence

| PRD | Title | Why it's first/next |
|-----|-------|---------------------|
| 01 | `TenantDB` replace/delete primitive | Foundation: `mode:"replace"` needs a tenant-scoped delete. Pure data layer, independently testable. |
| 02 | `social-trader` owner ingest tool + live-backed reads | The feature. Depends on 01 for replace. |
| 03 | `mcp-host token` + `mcp-host ingest` CLI | The cron/post-fill seam. Depends on 02's tool contract. |
| 04 | Docs (ONBOARDING worked example) + close request | Depends on 02+03 being final. |

Each PRD ships with tests and leaves `pytest` green before the next begins.

## Out of scope (noted, not built)

- Flipping `social-trader` off `"demo": true` — a storefront-labeling decision; the provider keeps
  a static fallback so the demo badge stays honest until the owner is live. One-line change later.
- A `scoreboard` (account-vs-SPY) dataset — no read tool exposes it yet; YAGNI until a tool needs it.
- Streaming/SSE push — intraday `tools/call` refresh is well under the cadence this fund needs.
