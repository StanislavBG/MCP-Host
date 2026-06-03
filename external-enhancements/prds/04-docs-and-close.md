# PRD 04 — Docs (ONBOARDING worked example) + close the request

## Problem
The mechanism only counts as delivered when an external owner can find and follow it. The request
explicitly asks for "a worked example in ONBOARDING.md showing an external owner calling an
`:admin` write tool over Streamable HTTP to refresh data, including how the owner obtains the
resource-bound bearer."

## Change
- `ONBOARDING.md`: new section **"Keeping a hosted provider's data fresh (owner ingest)"**:
  - the owner-write-tool pattern (declare an `:admin`-scoped tool; gateway gates by ownership);
  - obtaining the bearer (`mcp-host token …`, and that prod uses the OAuth AS);
  - `mcp-host ingest social-trader signals signals.json` + the equivalent raw `curl` JSON-RPC;
  - a cron one-liner for the post-fill hook;
  - the `replace` vs `append` semantics and the row contract.
- `external-enhancements/001-...md`: flip **Status** to `RESOLVED`, with a short resolution note
  pointing at the PRDs and the worked example.

## Acceptance criteria
1. ONBOARDING shows a copy-pasteable owner-ingest example (token → ingest → read reflects it).
2. The request file records RESOLVED + how each acceptance criterion is met.
3. `pytest` green; `mcp-host validate providers/social_trader` passes (schema + TDQS).

## Risk
None (docs + status). Validate-gate check guards the manifest edit from PRD 02.
