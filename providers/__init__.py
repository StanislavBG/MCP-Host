"""Provider registry. The host loads these and mounts them into the gateway.

Each entry is (Provider subclass, secrets dict). Secrets are read from the host secret store
(env here) and injected into the provider's ctx — providers never read env directly.

`platform-health` is the first real first-party provider (serves live host state, no external
data). The edgar-rag / signal-builder / social-trader entries are demo references — flagged
`"demo": true` in their manifests and badged as such on the storefront.
"""

from __future__ import annotations

import os

from providers.edgar_rag.provider import EdgarRagProvider
from providers.platform_health.provider import PlatformHealthProvider
from providers.platform_publisher.provider import PlatformPublisherProvider
from providers.signal_builder.provider import SignalBuilderProvider
from providers.social_trader.provider import SocialTraderProvider


def load_pilots():
    """Return [(provider_instance, secrets), ...] for every mounted provider."""
    return [
        (PlatformHealthProvider(), {}),
        (PlatformPublisherProvider(), {}),
        (EdgarRagProvider(), {"SEC_USER_AGENT": os.environ.get("SEC_USER_AGENT", "")}),
        (SignalBuilderProvider(), {}),
        (SocialTraderProvider(), {
            "ALPACA_API_KEY": os.environ.get("ALPACA_API_KEY", ""),
            "ALPACA_SECRET_KEY": os.environ.get("ALPACA_SECRET_KEY", ""),
            "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", ""),
        }),
    ]
