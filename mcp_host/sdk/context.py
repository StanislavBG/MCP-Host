"""ToolContext — the single object handed to every tool body.

By the time a tool body runs, the gateway has ALREADY authenticated, authorized (scope +
quota), and (if priced) billed the call. The tool just does its work using:

  ctx.principal     -> who is calling (verified by the gateway; never trust client claims)
  ctx.provider_id   -> the provider this tool belongs to
  ctx.tenant_db     -> a DB handle already RLS-scoped to <provider>.* (set app.tenant_id)
  ctx.artifacts     -> read-only access to uploaded artifacts (vectors/blobs)
  ctx.secret(name)  -> a provider third-party secret injected by the host secret store
  ctx.text/json_text/blocks -> content-envelope helpers

Providers MUST NOT reach around this object to env vars, other schemas, or the wallet.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from mcp_host.sdk import content


@dataclass
class Principal:
    """The verified caller. Produced by the gateway auth layer, never by a provider."""

    id: str
    kind: str = "user"  # user | agent | service
    plan: str = "free"
    scopes: tuple[str, ...] = ()

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


@dataclass
class ToolContext:
    provider_id: str
    principal: Principal
    tenant_db: Any = None  # data.tenant.TenantDB (RLS-scoped); None in pure-unit tests
    artifacts: Any = None  # artifacts.store.ArtifactView; None if provider declares none
    secrets: dict[str, str] = field(default_factory=dict)

    def secret(self, name: str) -> str | None:
        """A provider third-party secret (e.g. SEC_USER_AGENT, ALPACA_API_KEY)."""
        return self.secrets.get(name)

    # Content helpers (thin pass-throughs so tool bodies read cleanly).
    def text(self, s: str) -> dict[str, Any]:
        return content.text(s)

    def json_text(self, obj: Any) -> dict[str, Any]:
        return content.json_text(obj)

    def blocks(self, *items: dict[str, Any]) -> dict[str, Any]:
        return content.blocks(*items)


# Type alias for a tool body: (ctx, **arguments) -> content dict
ToolFn = Callable[..., dict[str, Any]]
