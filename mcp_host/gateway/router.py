"""Gateway orchestrator — the control-plane hot path.

For one JSON-RPC request against /mcp/<provider> it runs, in order:
  auth (resource-bound) -> entitlement (scope+quota+rate) -> billing (x402) -> provider
  dispatch -> metering -> (audit happens in the HTTP layer). Providers run only at the
  dispatch step and only after every gate has passed.

This class is transport-agnostic (no FastAPI here) so it's unit-testable in-process; the
FastAPI app in mcp_host/server.py adapts HTTP <-> handle().
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from mcp_host.auth import entitlements
from mcp_host.auth.principal import authenticate
from mcp_host.billing.x402 import BillingConfig, Facilitator, charge, payment_challenge
from mcp_host.data.tenant import SqliteTenantManager
from mcp_host.sdk import ErrorCode, Provider, ToolContext, ToolError
from mcp_host.sdk.errors import JSONRPC_CODE
from mcp_host.sdk.manifest import price_is_free


@dataclass
class GatewayConfig:
    base_url: str = "https://mcp-host"
    signing_key: str = "dev-signing-key"
    wallet_address: str = "0xSHARED"
    admin_key: str = ""
    platform_owner: str = ""  # principal id of the platform super-admin (may wield any :admin scope)


@dataclass
class Mounted:
    provider: Provider
    secrets: dict[str, str] = field(default_factory=dict)


@dataclass
class HandleResult:
    status: int
    body: dict[str, Any]
    headers: dict[str, str] = field(default_factory=dict)
    principal_id: str | None = None


class Gateway:
    def __init__(self, store, cfg: GatewayConfig, facilitator: Facilitator | None,
                 tenant_conn=None, tenant=None) -> None:
        self.store = store
        self.cfg = cfg
        self.facilitator = facilitator
        # `tenant` is a TenantManager; `tenant_conn` is the legacy SQLite shortcut.
        if tenant is not None:
            self.tenant = tenant
        elif tenant_conn is not None:
            self.tenant = SqliteTenantManager(tenant_conn)
        else:
            self.tenant = None
        self._mounts: dict[str, Mounted] = {}

    # ---- mounting --------------------------------------------------------
    def mount(self, provider: Provider, secrets: dict[str, str] | None = None) -> None:
        self.store.register_provider(provider.manifest)
        if self.tenant is not None:
            self.tenant.provision(provider.id, provider.manifest["data"]["postgres_schema"])
        self._mounts[provider.id] = Mounted(provider, secrets or {})

    def provider(self, provider_id: str) -> Provider | None:
        m = self._mounts.get(provider_id)
        return m.provider if m else None

    def providers(self) -> list[Provider]:
        return [m.provider for m in self._mounts.values()]

    def canonical_uri(self, provider_id: str) -> str:
        return f"{self.cfg.base_url}/mcp/{provider_id}"

    # ---- the hot path ----------------------------------------------------
    def handle(self, provider_id: str, body: dict[str, Any], headers: dict[str, str]) -> HandleResult:
        req_id = body.get("id")
        mount = self._mounts.get(provider_id)
        if mount is None:
            return self._err(req_id, ToolError(ErrorCode.PROVIDER_NOT_FOUND,
                                                f"No provider '{provider_id}'"))
        provider = mount.provider
        method = body.get("method", "")
        params = body.get("params", {}) or {}

        # 1. Authenticate (token bound to this provider's canonical URI).
        try:
            principal = authenticate(headers, self.store, self.cfg.signing_key,
                                     self.canonical_uri(provider_id))
        except ToolError as e:
            return self._err(req_id, e)

        resp_headers: dict[str, str] = {}

        # initialize: open a session, return server info.
        if method == "initialize":
            sid = headers.get("mcp-session-id") or uuid.uuid4().hex
            self.store.create_session(sid, provider_id, principal.id)
            resp_headers["Mcp-Session-Id"] = sid
            return HandleResult(200, self._result(req_id, provider.server_info()),
                                resp_headers, principal.id)

        if method == "tools/list":
            return HandleResult(200, self._result(req_id, provider.list_tools()),
                                resp_headers, principal.id)

        if method != "tools/call":
            return self._err(req_id, ToolError(ErrorCode.METHOD_NOT_FOUND,
                                               f"Unknown method '{method}'"), principal.id)

        # tools/call: full gate chain.
        tool_name = params.get("name", "")
        scope_map = provider.scope_map()
        if tool_name not in scope_map:
            return self._err(req_id, ToolError(ErrorCode.TOOL_NOT_FOUND,
                                               f"Unknown tool '{tool_name}'"), principal.id)
        scope = scope_map[tool_name]
        price = provider.price_map().get(tool_name, "0.00")

        # 2. Entitlement — OR, for :admin-scoped tools, owner-only authorization.
        # An :admin scope is the owner's private control surface for their OWN MCP: only the
        # provider's declared owner (or the platform super-admin) may call it. Ownership is the
        # grant, so we bypass the plan-entitlement table for these (and never seed them).
        if scope.endswith(":admin"):
            owner = provider.manifest.get("owner")
            if not (owner and principal.id == owner) and principal.id != self.cfg.platform_owner:
                e = ToolError(ErrorCode.FORBIDDEN_SCOPE,
                              f"Tool '{tool_name}' is owner-only; principal does not own '{provider_id}'")
                self.store.record_usage(provider_id, tool_name, principal.id, 0, False, e.http_status)
                return self._err(req_id, e, principal.id)
        else:
            try:
                entitlements.check(self.store, principal, provider_id, tool_name, scope)
            except ToolError as e:
                self.store.record_usage(provider_id, tool_name, principal.id, 0, False, e.http_status)
                return self._err(req_id, e, principal.id)

        # 3. Billing.
        try:
            billing_cfg = BillingConfig(self.cfg.wallet_address, self.cfg.admin_key)
            charge_res = charge(billing_cfg, self.facilitator, price, headers)
        except ToolError as e:
            self.store.record_usage(provider_id, tool_name, principal.id, 0, False, e.http_status)
            res = self._err(req_id, e, principal.id)
            if e.code == ErrorCode.PAYMENT_REQUIRED:
                res.body["error"]["data"]["challenge"] = payment_challenge(price, self.cfg.wallet_address)
            return res

        # 4. Dispatch the provider tool body.
        ctx = ToolContext(
            provider_id=provider_id,
            principal=principal,
            tenant_db=self.tenant.handle(provider_id) if self.tenant else None,
            secrets=mount.secrets,
        )
        t0 = time.perf_counter()
        try:
            result = provider.call_tool(ctx, tool_name, params.get("arguments", {}))
        except ToolError as e:
            self.store.record_usage(provider_id, tool_name, principal.id,
                                    int((time.perf_counter() - t0) * 1000), charge_res.paid,
                                    e.http_status, charge_res.tx_hash)
            return self._err(req_id, e, principal.id)
        except Exception as e:  # provider bug -> contained, logged as INTERNAL_ERROR
            self.store.record_usage(provider_id, tool_name, principal.id,
                                    int((time.perf_counter() - t0) * 1000), charge_res.paid, 500)
            return self._err(req_id, ToolError(ErrorCode.INTERNAL_ERROR, f"Tool failed: {e}"),
                             principal.id)

        # 5. Meter the successful call.
        self.store.record_usage(provider_id, tool_name, principal.id,
                                int((time.perf_counter() - t0) * 1000),
                                charge_res.paid, 200, charge_res.tx_hash)
        if charge_res.tx_hash:
            resp_headers["X-Payment-Response"] = charge_res.tx_hash
        return HandleResult(200, self._result(req_id, result), resp_headers, principal.id)

    # ---- envelope helpers ------------------------------------------------
    @staticmethod
    def _result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "result": result}

    def _err(self, req_id: Any, e: ToolError, principal_id: str | None = None) -> HandleResult:
        body = {
            "jsonrpc": "2.0", "id": req_id,
            "error": {
                "code": JSONRPC_CODE.get(e.code, -32603),
                "message": e.message,
                "data": {"code": e.code.value, "retry": e.retry, "field": e.field},
            },
        }
        return HandleResult(e.http_status, body, {}, principal_id)


def price_label(price: str) -> str:
    return "free" if price_is_free(price) else f"{price} USDC"
