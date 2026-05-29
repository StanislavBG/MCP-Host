"""M3 — Billing: free pass-through, 402 challenge, fail-closed, admin bypass, paid verify."""

from __future__ import annotations

import pytest

from mcp_host.billing.x402 import BillingConfig, ChargeResult, StubFacilitator, charge, payment_challenge
from mcp_host.sdk import ErrorCode, ToolError

CFG = BillingConfig(wallet_address="0xSHARED", admin_key="admin-secret")


def test_free_tool_proceeds_unpaid():
    res = charge(CFG, StubFacilitator(), "$0.00", {})
    assert res == ChargeResult(paid=False)


def test_priced_no_payment_returns_402():
    with pytest.raises(ToolError) as ei:
        charge(CFG, StubFacilitator(), "0.01", {})
    assert ei.value.code == ErrorCode.PAYMENT_REQUIRED
    assert ei.value.http_status == 402


def test_priced_facilitator_down_fails_closed():
    with pytest.raises(ToolError) as ei:
        charge(CFG, StubFacilitator(up=False), "0.01", {"x-payment": "paid:abc"})
    assert ei.value.code == ErrorCode.FACILITATOR_UNAVAILABLE
    assert ei.value.http_status == 503


def test_priced_no_facilitator_fails_closed():
    with pytest.raises(ToolError) as ei:
        charge(CFG, None, "0.01", {"x-payment": "paid:abc"})
    assert ei.value.code == ErrorCode.FACILITATOR_UNAVAILABLE


def test_admin_bypass():
    res = charge(CFG, StubFacilitator(up=False), "0.01", {"x-admin-key": "admin-secret"})
    assert res.bypassed and not res.paid


def test_admin_bypass_wrong_key_does_not_bypass():
    with pytest.raises(ToolError):
        charge(CFG, StubFacilitator(up=False), "0.01", {"x-admin-key": "nope"})


def test_valid_payment_returns_tx():
    res = charge(CFG, StubFacilitator(), "0.01", {"x-payment": "paid:deadbeef"})
    assert res.paid and res.tx_hash.startswith("0x")


def test_invalid_payment_402():
    with pytest.raises(ToolError) as ei:
        charge(CFG, StubFacilitator(), "0.01", {"x-payment": "garbage"})
    assert ei.value.code == ErrorCode.PAYMENT_REQUIRED


def test_payment_challenge_shape():
    ch = payment_challenge("0.01", "0xSHARED")
    assert ch["accepts"][0]["payTo"] == "0xSHARED"
    assert ch["accepts"][0]["network"] == "eip155:8453"
