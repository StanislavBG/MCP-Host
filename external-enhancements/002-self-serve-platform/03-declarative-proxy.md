# PRD 03 — Declarative `ProxyProvider` + SSRF guard

## Problem
Self-serve deploy must NOT run guest Python in the shared host process. We need a host-owned
provider that executes a third party's tools by forwarding to their external service, with the
host still owning auth/billing/metering/audit on the normal hot path.

## Change
- `schemas/provider.schema.json`:
  - top-level optional `backend` `{kind: "external-http", endpoint: https-uri}`,
  - per-tool optional `input_schema` (inline JSON Schema) so declarative tools expose real params
    in `tools/list` and to TDQS without a Pydantic model.
- `mcp_host/sdk/errors.py`: new `BACKEND_UNAVAILABLE` → JSON-RPC `-32003`, HTTP `502`.
- `mcp_host/sdk/proxy.py`:
  - `validate_external_endpoint(url, resolver)` — SSRF guard: https only; reject the blocked
    hostname set (localhost, metadata.*) and any IP literal or resolved address that is
    private/loopback/link-local/multicast/reserved/unspecified. **Caveat:** `ManifestError`
    subclasses `ValueError`, so the IP-literal check uses an explicit `_as_ip()` helper rather
    than try/except (a `try` that catches `ValueError` would swallow the block — this was a real
    bug caught in test).
  - `ProxyProvider(Provider)` — built from a manifest + signing key; bypasses the base
    `@tool` reconciliation (no methods) and drives dispatch from the manifest. `call_tool`:
    1. re-validate the endpoint (defeat DNS rebinding after deploy-time check),
    2. POST `{tool, arguments, provider, principal}` to the endpoint,
    3. sign with `HMAC_SHA256(signing_key, "<ts>." + body)` in `X-MCP-Host-Signature`
       (+ timestamp/provider headers) so the owner can trust the host,
    4. **fail closed**: transport error / non-2xx / >256 KB / non-JSON / non-object →
       `BACKEND_UNAVAILABLE`.
  - Transport and resolver are injectable, so tests never touch the network.

## Acceptance criteria
1. Endpoint guard rejects `http://`, `localhost`/metadata, IP-literal private/loopback/link-local
   (incl. `169.254.169.254`, `::1`), and hostnames resolving to a private address; accepts public.
2. A successful call forwards the signed request and returns the endpoint's JSON object verbatim;
   the body carries the calling principal.
3. Every failure mode (raise, 500, non-JSON, non-object) surfaces as `BACKEND_UNAVAILABLE`.
4. SSRF is re-checked at call time: an endpoint that flips to loopback after deploy is rejected.

## Tests
`test_m9_selfserve.py`: `test_endpoint_guard_*` (parametrized), `test_proxy_forwards_signed_request_*`,
`test_proxy_fails_closed` (parametrized), `test_proxy_rechecks_ssrf_at_call_time`.

## Risk
Contained. The proxy is host-owned code; the only outbound surface is the SSRF-guarded HTTPS call,
fail-closed on every anomaly. Residual: full SSRF safety depends on the resolver reflecting real
DNS at call time; we re-resolve per call but cannot defend against an in-flight TOCTOU at the
socket layer (acceptable for alpha; documented).
