"""M1 — Auth & entitlements: token validation, resource indicators, scope/quota/rate gates."""

from __future__ import annotations

import pytest

from mcp_host.auth import entitlements
from mcp_host.auth.principal import authenticate, mint_token, verify_token
from mcp_host.data.store import Entitlement, SqliteStore
from mcp_host.sdk import ErrorCode, Principal, ToolError

KEY = "test-signing-key"
RES = "https://mcp-host/mcp/edgar-rag"


def test_token_roundtrip_ok():
    tok = mint_token(KEY, "alice", "pro", ["edgar:read"], RES)
    p = verify_token(KEY, tok, RES)
    assert p.id == "alice" and p.plan == "pro" and p.has_scope("edgar:read")


def test_token_bad_signature():
    tok = mint_token(KEY, "alice", "pro", ["edgar:read"], RES)
    with pytest.raises(ToolError) as ei:
        verify_token("wrong-key", tok, RES)
    assert ei.value.code == ErrorCode.UNAUTHENTICATED


def test_token_resource_indicator_mismatch():
    """A token minted for edgar-rag must not work against another provider URI (RFC 8707)."""
    tok = mint_token(KEY, "alice", "pro", ["edgar:read"], RES)
    with pytest.raises(ToolError) as ei:
        verify_token(KEY, tok, "https://mcp-host/mcp/social-trader")
    assert ei.value.code == ErrorCode.FORBIDDEN_SCOPE


def test_token_expired():
    tok = mint_token(KEY, "alice", "pro", ["edgar:read"], RES, ttl_secs=-1)
    with pytest.raises(ToolError) as ei:
        verify_token(KEY, tok, RES)
    assert ei.value.code == ErrorCode.UNAUTHENTICATED


def test_authenticate_api_key():
    s = SqliteStore()
    s.create_principal("svc", kind="agent", plan="enterprise")
    s.add_api_key("k1", "svc", "raw-key-xyz", ["edgar:read", "edgar:search"])
    p = authenticate({"x-api-key": "raw-key-xyz"}, s, KEY, RES)
    assert p.id == "svc" and p.kind == "agent" and p.has_scope("edgar:search")


def test_authenticate_missing_credentials():
    s = SqliteStore()
    with pytest.raises(ToolError) as ei:
        authenticate({}, s, KEY, RES)
    assert ei.value.code == ErrorCode.UNAUTHENTICATED


def _store_with_plan(quota=100, rate=60):
    s = SqliteStore()
    s.create_principal("u", plan="free")
    s.set_entitlement(Entitlement("free", "edgar-rag", "edgar:read", quota, rate))
    return s


def test_entitlement_allows_when_scoped_and_under_limits():
    s = _store_with_plan()
    p = Principal(id="u", plan="free", scopes=("edgar:read",))
    entitlements.check(s, p, "edgar-rag", "list_companies", "edgar:read")  # no raise


def test_entitlement_denies_missing_scope():
    s = _store_with_plan()
    p = Principal(id="u", plan="free", scopes=())
    with pytest.raises(ToolError) as ei:
        entitlements.check(s, p, "edgar-rag", "search_filings", "edgar:read")
    assert ei.value.code == ErrorCode.FORBIDDEN_SCOPE


def test_entitlement_denies_plan_without_grant():
    s = SqliteStore()
    p = Principal(id="u", plan="free", scopes=("edgar:read",))
    with pytest.raises(ToolError) as ei:
        entitlements.check(s, p, "edgar-rag", "list_companies", "edgar:read")
    assert ei.value.code == ErrorCode.FORBIDDEN_SCOPE  # default-deny: no entitlement row


def test_entitlement_quota_exceeded():
    s = _store_with_plan(quota=1, rate=1000)
    p = Principal(id="u", plan="free", scopes=("edgar:read",))
    s.record_usage("edgar-rag", "x", "u", 1, paid=False, status_code=200)
    with pytest.raises(ToolError) as ei:
        entitlements.check(s, p, "edgar-rag", "x", "edgar:read")
    assert ei.value.code == ErrorCode.QUOTA_EXCEEDED


def test_entitlement_rate_limited():
    s = _store_with_plan(quota=10000, rate=2)
    p = Principal(id="u", plan="free", scopes=("edgar:read",))
    for _ in range(2):
        s.record_usage("edgar-rag", "x", "u", 1, paid=False, status_code=200)
    with pytest.raises(ToolError) as ei:
        entitlements.check(s, p, "edgar-rag", "x", "edgar:read")
    assert ei.value.code == ErrorCode.RATE_LIMIT_EXCEEDED
