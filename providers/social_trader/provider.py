"""Social Signals Trader provider — the PUBLISHER role.

The local project is an MCP *client* (it consumes signal-builder + executes on Alpaca). On
MCP-Host it flips to a *publisher*: consumers subscribe to its buy/sell feed. The feed tool is
both subscription-scoped (trader:subscribe) and x402-priced, exercising the platform's
entitlement + billing gates together. Live trading remains off-platform behind SST_LIVE_TRADE.

Internally this provider would consume signal-builder THROUGH the gateway with a service
principal (not a direct stdio spawn); that wiring is a deployment concern, so here we ship a
representative static feed derived from a sample portfolio.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mcp_host.sdk import Provider, tool

_PORTFOLIO = [
    {"ticker": "NVDA", "side": "buy", "weight": 0.18, "entry": "2024-02-22"},
    {"ticker": "TSLA", "side": "sell", "weight": 0.05, "entry": "2024-01-25"},
    {"ticker": "AAPL", "side": "buy", "weight": 0.12, "entry": "2023-11-06"},
]
_SIGNALS = [
    {"ticker": "NVDA", "side": "buy", "conviction": 0.81,
     "rationale": "Mention velocity + 8-K tone positive", "ts": "2024-02-22T14:30:00+00:00",
     "outcome_pct": 6.4},
    {"ticker": "TSLA", "side": "sell", "conviction": 0.62,
     "rationale": "Deliveries miss + bearish sentiment shift", "ts": "2024-01-25T14:30:00+00:00",
     "outcome_pct": -3.1},
]


class FeedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=10, ge=1, le=50)


class SocialTraderProvider(Provider):
    manifest_path = "provider.json"

    @tool("signals.feed", input_model=FeedInput)
    def signals_feed(self, ctx, limit: int = 10):
        live = [{k: v for k, v in s.items() if k != "outcome_pct"} for s in _SIGNALS[:limit]]
        return ctx.json_text({"subscriber": ctx.principal.id, "signals": live, "count": len(live)})

    @tool("signals.history")
    def signals_history(self, ctx):
        return ctx.json_text({"signals": _SIGNALS, "count": len(_SIGNALS)})

    @tool("portfolio.positions")
    def portfolio_positions(self, ctx):
        return ctx.json_text({"positions": _PORTFOLIO, "count": len(_PORTFOLIO)})
