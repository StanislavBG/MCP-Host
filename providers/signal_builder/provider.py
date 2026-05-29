"""Signal Builder provider — per-ticker social signals.

Demonstrates (a) the standard result envelope {payload, built_at, schema_version} the platform
standardizes, and (b) a WRITE tool persisting to the provider's RLS-isolated tenant schema via
ctx.tenant_db. The real project's 26 stdio tools wrap the same way; this ships a representative
subset that runs without the Reddit/Finra backends.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, ConfigDict, Field

from mcp_host.sdk import Provider, tool

SCHEMA_VERSION = 3

# Stand-in mention counts (real impl aggregates from the panel_history artifact / signal.* tables).
_MENTIONS = {"NVDA": 412, "TSLA": 388, "AAPL": 201, "GME": 173, "AMD": 96}


def _envelope(payload):
    return {"payload": payload, "built_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()),
            "schema_version": SCHEMA_VERSION}


class TrendingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    window_hours: int = Field(default=24, ge=1, le=168)
    limit: int = Field(default=10, ge=1, le=50)


class VelocityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(min_length=1, max_length=8)
    window_hours: int = Field(default=24, ge=1, le=168)


class TrackInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(min_length=1, max_length=8)


class SignalBuilderProvider(Provider):
    manifest_path = "provider.json"

    @tool("panels.trending", input_model=TrendingInput)
    def panels_trending(self, ctx, window_hours: int = 24, limit: int = 10):
        ranked = sorted(_MENTIONS.items(), key=lambda kv: -kv[1])[:limit]
        rows = [{"ticker": t, "mentions": n} for t, n in ranked]
        return ctx.json_text(_envelope({"window_hours": window_hours, "tickers": rows}))

    @tool("signal.ticker_velocity", input_model=VelocityInput)
    def ticker_velocity(self, ctx, ticker: str, window_hours: int = 24):
        cur = _MENTIONS.get(ticker.upper(), 0)
        prior = max(1, cur // 2)
        return ctx.json_text(_envelope({
            "ticker": ticker.upper(), "window_hours": window_hours,
            "current": cur, "prior": prior, "velocity": round(cur / prior, 2),
        }))

    @tool("signal.track_ticker", input_model=TrackInput)
    def track_ticker(self, ctx, ticker: str):
        # Persist to the provider's tenant schema (RLS-isolated). Idempotent per principal+ticker.
        if ctx.tenant_db is not None:
            ctx.tenant_db.create_table("tracked", "ticker TEXT, principal TEXT, ts TEXT")
            existing = ctx.tenant_db.query("tracked", "ticker=? AND principal=?",
                                           (ticker.upper(), ctx.principal.id))
            if not existing:
                ctx.tenant_db.insert("tracked", {"ticker": ticker.upper(),
                                                 "principal": ctx.principal.id,
                                                 "ts": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())})
        return ctx.json_text(_envelope({"tracked": ticker.upper(), "status": "queued"}))

    @tool("signal.list_tracked_tickers")
    def list_tracked(self, ctx):
        rows = []
        if ctx.tenant_db is not None:
            ctx.tenant_db.create_table("tracked", "ticker TEXT, principal TEXT, ts TEXT")
            rows = ctx.tenant_db.query("tracked", "principal=?", (ctx.principal.id,))
        return ctx.json_text(_envelope({"tracked": [r["ticker"] for r in rows]}))
