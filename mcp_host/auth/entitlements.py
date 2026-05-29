"""Entitlement engine — the per-tool ACL + quota gate, enforced BEFORE dispatch.

Decision for (principal, provider, tool):
  1. Map tool -> required scope (from the provider's scope_map / provider.json).
  2. Principal must carry that scope (token/api-key scope) -> else FORBIDDEN_SCOPE (403).
  3. Look up entitlement (plan, provider, scope):
       - rate_per_min: recent calls in the last 60s must be < rate_per_min -> else RATE_LIMIT (429)
       - monthly_quota (-1 = unlimited): calls this calendar-ish window < quota -> else QUOTA (429)
  4. If no entitlement row exists for the plan, deny by default (FORBIDDEN_SCOPE) — plans must be
     explicitly granted, matching Composio/Glama per-tool ACL posture.

This is host-owned; providers trust the allow decision and never re-check.
"""

from __future__ import annotations

import time

from mcp_host.sdk import ErrorCode, Principal, ToolError

MONTH_SECS = 30 * 24 * 3600


def check(store, principal: Principal, provider_id: str, tool: str, scope: str) -> None:
    """Raise ToolError if the call is not allowed; return None if allowed."""
    if not principal.has_scope(scope):
        raise ToolError(ErrorCode.FORBIDDEN_SCOPE,
                        f"Principal lacks scope '{scope}' for {provider_id}/{tool}")

    ent = store.get_entitlement(principal.plan, provider_id, scope)
    if ent is None:
        raise ToolError(ErrorCode.FORBIDDEN_SCOPE,
                        f"Plan '{principal.plan}' has no entitlement for {provider_id}:{scope}")

    if ent.rate_per_min > 0:
        recent = store.recent_call_count(principal.id, provider_id, window_secs=60)
        if recent >= ent.rate_per_min:
            raise ToolError(ErrorCode.RATE_LIMIT_EXCEEDED,
                            f"Rate limit {ent.rate_per_min}/min exceeded", retry=True)

    if ent.monthly_quota >= 0:
        since = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(time.time() - MONTH_SECS))
        used = store.usage_count_since(principal.id, provider_id, since)
        if used >= ent.monthly_quota:
            raise ToolError(ErrorCode.QUOTA_EXCEEDED,
                            f"Monthly quota {ent.monthly_quota} exhausted for {provider_id}", retry=False)
