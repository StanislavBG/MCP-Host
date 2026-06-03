# PRD 03 — `mcp-host token` + `mcp-host ingest` (the cron seam)

## Problem
The request wants a "documented, repeatable command I can wire into cron." Today the CLI has no
way to (a) obtain a resource-bound owner bearer, or (b) push rows to a running host. An owner with
only credentials can't refresh from a script.

## Change (`cli/main.py`, stdlib only — `urllib`, no new deps)

### `mcp-host token`
`--provider <id> --sub <owner> [--scopes s1 s2] [--base-url U] [--ttl N] [--signing-key K]`
Mints a bearer via `mcp_host.auth.principal.mint_token`, resource-bound to
`<base-url>/mcp/<provider>`. Signing key from `--signing-key` or `$MCP_HOST_SIGNING_KEY`
(default `dev-signing-key`). This is the **self-host/dev issuer**; in production the real OAuth
2.1 AS issues the token — same gateway-side checks. Prints the raw token.

### `mcp-host ingest`
`<provider> <dataset> <file.json> [--base-url U] [--mode replace|append]
 [--token T] [--signing-key K --sub S] [--dry-run]`
- `file.json` is a JSON array of rows, or `{"rows": [...]}`.
- Builds the `tools/call` body for `signals.ingest` and POSTs to `<base-url>/mcp/<provider>`.
- Token from `--token`, else minted from `--signing-key`/`--sub` (cron convenience).
- `--dry-run` prints the request (method/url/headers-redacted/body) instead of sending — keeps
  the command unit-testable and gives a safe preview.

Factor a pure `build_ingest_request(provider, dataset, rows, mode, base_url, token)` →
`(url, headers, body)` so payload shaping is tested without a network or a live host.

## Acceptance criteria
1. `token` prints a bearer that `verify_token` accepts for the provider's canonical URI.
2. `ingest --dry-run` emits a well-formed JSON-RPC `tools/call` for `signals.ingest` with the
   file's rows and chosen mode, Authorization redacted in the printed preview.
3. A live (non-dry) ingest POSTs to `/mcp/<provider>` and prints the host's JSON response.
4. Bad file (not a list / not `{"rows": [...]}`) → non-zero exit with a clear message.

## Tests (`tests/test_cli.py`)
- `test_cli_token_mints_verifiable_bearer` — capture stdout, `verify_token` succeeds.
- `test_cli_ingest_dry_run_builds_tools_call` — `--dry-run` body has
  `method=tools/call`, `params.name=signals.ingest`, rows + mode; exit 0.
- `test_cli_ingest_rejects_bad_file` — exit non-zero.

## Risk
Low. New subcommands; existing CLI commands and their tests untouched. Real network send only on
the non-dry path.
