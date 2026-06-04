# Enhancement 002 — Self-serve platform (registration → publish → declarative execution)

Turns MCP-Host from "operator deploys our own providers" into a real platform: any third party
can **register** as an owner and **publish** their own MCP without a host operator, while the
host runs **no guest code**.

## Decisions (set by the owner before build)

| Fork | Choice | Consequence |
|------|--------|-------------|
| Who registers | **Open self-serve** | `POST /register` is unauthenticated; rate-limited per masked IP. |
| Credential | **API key (hashed)** | Reuses the existing `api_keys` table + `x-api-key` auth path. Shown once, stored as SHA-256. |
| Deploy scope | **Registration + self-serve deploy** | A registered owner can publish + mount their own provider. |
| Execution model | **Declarative / proxied** | Owner runs tool logic on their own HTTPS endpoint; host proxies. No guest Python in-process. |

### Why declarative, not in-process guest code

The host is a single Python process holding the shared wallet key and every tenant's `<id>.*`
schema. In-process guest code = full compromise (wallet, RLS bypass, cross-tenant reads) and is
unsafe without a real sandbox. The declarative model keeps every host service (auth, billing,
metering, audit, RLS provisioning) on the normal `tools/call` hot path and forwards only the tool
**body** to the owner's external endpoint — so a stranger's code never executes next to the wallet.

## What already existed (reused, not rebuilt)

- `platform.principals` + `create_principal()`, `platform.api_keys` + `add_api_key()` / `principal_for_key()`.
- `x-api-key` authentication (`mcp_host/auth/principal.py`).
- The owner gate is already generic: `principal.id == provider.manifest["owner"]` works for any principal.
- `Gateway.mount()` already does `register_provider` + tenant `provision`.

## What this enhancement added

| PRD | Title | Ships |
|-----|-------|-------|
| 01 | Self-serve registration | `POST /register` (rate-limited) + `mcp-host register`; `auth/registration.py`. |
| 02 | Owner-authenticated deploy | `POST /providers` (x-api-key) + `mcp-host publish`; `gateway/deploy.py`; boot re-mount. |
| 03 | Declarative ProxyProvider | `sdk/proxy.py` (HMAC-signed, fail-closed, SSRF-guarded); schema `backend` + tool `input_schema`; `BACKEND_UNAVAILABLE` (502). |

Each PRD shipped with tests; the full suite stays green (`tests/test_m9_selfserve.py`, plus
HTTP/CLI cases). ONBOARDING §6a is the worked example.

## Out of scope (noted, not built)

- OAuth 2.1 dynamic client registration (the standards path) — API key is sufficient for alpha.
- Sandboxed in-process guest code — only declarative/proxied providers are self-servable.
- Per-owner billing payout split — all funds still land in the one shared wallet; usage is
  already metered per `(provider, tool, principal)` for P&L.
- A storefront "publish" UI — CLI + raw HTTP only for now.
