"""Pilot provider registry. The host loads these and mounts them into the gateway.

Each entry is (Provider subclass, secrets dict). Secrets are read from the host secret store
(env here) and injected into the provider's ctx — providers never read env directly.
"""

from __future__ import annotations

import os

from providers.edgar_rag.provider import EdgarRagProvider
from providers.signal_builder.provider import SignalBuilderProvider
from providers.social_trader.provider import SocialTraderProvider


def load_pilots():
    """Return [(provider_instance, secrets), ...] for every pilot."""
    return [
        (EdgarRagProvider(), {"SEC_USER_AGENT": os.environ.get("SEC_USER_AGENT", "")}),
        (SignalBuilderProvider(), {}),
        (SocialTraderProvider(), {
            "ALPACA_API_KEY": os.environ.get("ALPACA_API_KEY", ""),
            "ALPACA_SECRET_KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
        }),
    ]
