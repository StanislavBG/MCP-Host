"""Shared x402 billing — lifted from edgar-rag's pattern, generalized to per-tool pricing.

ONE shared wallet (WALLET_ADDRESS) for every provider; ONE shared facilitator (Base L2,
eip155:8453, USDC). Price is per-tool from provider.json. Rules, in order, for each call:

  1. free price -> proceed unpaid.
  2. admin bypass: x-admin-key == UPLOAD_SECRET (constant-time) -> proceed unpaid (testing/ops).
  3. priced + facilitator UNAVAILABLE -> FAIL CLOSED (503 FACILITATOR_UNAVAILABLE).
  4. priced + no x-payment header -> 402 PAYMENT_REQUIRED with a challenge (price/wallet/network).
  5. priced + x-payment header -> verify via facilitator -> tx_hash, or 402 on rejection.

The Facilitator is an interface so tests stub it; production wires the real x402 HTTP client.
Revenue accounting is decoupled from the single wallet via metering: each paid call is tagged
with (provider, tool) in platform.usage for per-product P&L.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from mcp_host.auth.principal import secure_eq
from mcp_host.sdk import ErrorCode, ToolError
from mcp_host.sdk.manifest import price_is_free

NETWORK = "eip155:8453"  # Base L2
ASSET = "USDC"


class FacilitatorUnavailable(Exception):
    pass


class Facilitator(Protocol):
    def available(self) -> bool: ...
    def verify(self, payment_header: str, price: str, pay_to: str) -> str:
        """Return a tx hash if the payment is valid; raise ToolError(PAYMENT_REQUIRED) if not.
        Raise FacilitatorUnavailable if the facilitator can't be reached."""
        ...


@dataclass
class BillingConfig:
    wallet_address: str
    admin_key: str = ""  # UPLOAD_SECRET; empty disables bypass


@dataclass
class ChargeResult:
    paid: bool
    tx_hash: str | None = None
    bypassed: bool = False


def payment_challenge(price: str, wallet: str) -> dict:
    """The body returned with a 402 so a client SDK knows how to pay."""
    return {
        "x402Version": 1,
        "accepts": [{
            "scheme": "exact", "network": NETWORK, "asset": ASSET,
            "price": price, "payTo": wallet,
        }],
    }


def charge(cfg: BillingConfig, facilitator: Facilitator | None, price: str,
           headers: dict[str, str]) -> ChargeResult:
    """Decide whether the call may proceed and return the billing outcome.
    Raises ToolError for 402 / 503 cases."""
    if price_is_free(price):
        return ChargeResult(paid=False)

    admin = headers.get("x-admin-key") or headers.get("X-Admin-Key") or ""
    if cfg.admin_key and secure_eq(admin, cfg.admin_key):
        return ChargeResult(paid=False, bypassed=True)

    # Fail closed: never serve a priced tool when we can't verify payment.
    if facilitator is None or not facilitator.available():
        raise ToolError(ErrorCode.FACILITATOR_UNAVAILABLE,
                        "Payment verification unavailable. Try again shortly.", retry=True)

    payment = headers.get("x-payment") or headers.get("X-Payment") or ""
    if not payment:
        raise ToolError(ErrorCode.PAYMENT_REQUIRED,
                        f"Payment of {price} {ASSET} required on {NETWORK}", retry=True)

    try:
        tx = facilitator.verify(payment, price, cfg.wallet_address)
    except FacilitatorUnavailable:
        raise ToolError(ErrorCode.FACILITATOR_UNAVAILABLE,
                        "Payment verification unavailable. Try again shortly.", retry=True)
    return ChargeResult(paid=True, tx_hash=tx)


class StubFacilitator:
    """Test/dev facilitator. Accepts a payment header that starts with 'paid:' and echoes a tx.
    Set `up=False` to simulate an outage (drives the fail-closed path)."""

    def __init__(self, up: bool = True) -> None:
        self.up = up

    def available(self) -> bool:
        return self.up

    def verify(self, payment_header: str, price: str, pay_to: str) -> str:
        if not self.up:
            raise FacilitatorUnavailable()
        if payment_header.startswith("paid:"):
            return "0x" + payment_header[5:][:16].ljust(16, "0")
        raise ToolError(ErrorCode.PAYMENT_REQUIRED, "Invalid payment", retry=True)
