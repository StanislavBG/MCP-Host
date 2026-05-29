"""Authentication → a verified Principal.

Two modes (both yield the same Principal the gateway then trusts):

1. OAuth 2.1 bearer (the standard for human/agent clients). We model the *validation* contract
   the gateway must enforce: a signed token carrying sub/plan/scopes/resource/exp. Tokens are
   HMAC-signed here with the host signing key; on Replit the issuer is a real OAuth 2.1 AS, but
   the gateway-side checks (signature, expiry, and RFC 8707 resource-indicator match) are
   identical. The resource indicator MUST equal the canonical provider URI being called, so a
   token minted for /mcp/edgar-rag cannot be replayed against /mcp/social-trader.

2. API key (machine callers). Looked up in the store; yields principal + plan + scopes.

Providers NEVER call this — they receive the resulting Principal via ctx.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from mcp_host.sdk import ErrorCode, Principal, ToolError


def _b64u(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _b64u_dec(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def secure_eq(a: str, b: str) -> bool:
    """Constant-time compare; False if either side is empty (from edgar-rag's _secure_eq)."""
    if not a or not b:
        return False
    return hmac.compare_digest(a, b)


def mint_token(signing_key: str, principal_id: str, plan: str, scopes: list[str],
               resource: str, ttl_secs: int = 3600, kind: str = "user") -> str:
    """Issue a bearer token (test/dev issuer). `resource` is the canonical provider URI."""
    payload = {
        "sub": principal_id, "kind": kind, "plan": plan, "scopes": scopes,
        "resource": resource, "exp": int(time.time()) + ttl_secs,
    }
    body = _b64u(json.dumps(payload, separators=(",", ":")).encode())
    sig = _b64u(hmac.new(signing_key.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{sig}"


def verify_token(signing_key: str, token: str, expected_resource: str) -> Principal:
    """Validate signature, expiry, and resource indicator. Raise ToolError on any failure."""
    try:
        body, sig = token.split(".", 1)
    except ValueError:
        raise ToolError(ErrorCode.UNAUTHENTICATED, "Malformed bearer token")
    expected_sig = _b64u(hmac.new(signing_key.encode(), body.encode(), hashlib.sha256).digest())
    if not secure_eq(sig, expected_sig):
        raise ToolError(ErrorCode.UNAUTHENTICATED, "Bad token signature")
    payload = json.loads(_b64u_dec(body))
    if payload.get("exp", 0) < int(time.time()):
        raise ToolError(ErrorCode.UNAUTHENTICATED, "Token expired", retry=False)
    # RFC 8707 resource indicator: token must be bound to the provider being called.
    if payload.get("resource") != expected_resource:
        raise ToolError(ErrorCode.FORBIDDEN_SCOPE,
                        f"Token resource '{payload.get('resource')}' != '{expected_resource}'")
    return Principal(id=payload["sub"], kind=payload.get("kind", "user"),
                     plan=payload.get("plan", "free"), scopes=tuple(payload.get("scopes", [])))


def authenticate(headers: dict[str, str], store, signing_key: str, expected_resource: str) -> Principal:
    """Resolve a Principal from request headers. Tries bearer, then x-api-key."""
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return verify_token(signing_key, auth[7:].strip(), expected_resource)
    api_key = headers.get("x-api-key") or headers.get("X-Api-Key")
    if api_key:
        got = store.principal_for_key(api_key)
        if not got:
            raise ToolError(ErrorCode.UNAUTHENTICATED, "Invalid API key")
        pid, plan, scopes = got
        return Principal(id=pid, kind="agent", plan=plan, scopes=scopes)
    raise ToolError(ErrorCode.UNAUTHENTICATED, "Missing bearer token or x-api-key")
