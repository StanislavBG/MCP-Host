"""Social Signals Trader provider — the PUBLISHER role.

The local project is an MCP *client* (it consumes signal-builder + executes on Alpaca). On
MCP-Host it flips to a *publisher*: consumers subscribe to its buy/sell feed. The feed tool is
both subscription-scoped (trader:subscribe) and x402-priced, exercising the platform's
entitlement + billing gates together. Live trading remains off-platform behind SST_LIVE_TRADE.

Owner ingest (enhancement 001): the off-platform fund keeps the feed fresh by calling the
owner-only `signals.ingest` tool (scope `trader:admin`, gated by OWNERSHIP at the gateway — see
mcp_host/gateway/router.py). Rows land in the provider's RLS-isolated `trader.*` schema via
ctx.tenant_db; the read tools serve that live data and fall back to the static seed below only
while no rows have been ingested (so the storefront/demo stays honest).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from mcp_host.sdk import ErrorCode, Provider, ToolError, tool

# Static seed — served only when the owner has not yet ingested live rows (demo/storefront).
_PORTFOLIO = [
    {"ticker": "NVDA", "side": "buy", "weight": 0.18, "entry": "2024-02-22"},
    {"ticker": "TSLA", "side": "sell", "weight": 0.05, "entry": "2024-01-25"},
    {"ticker": "AAPL", "side": "buy", "weight": 0.12, "entry": "2023-11-06"},
]
_SIGNALS = [
    {"ticker": "NVDA", "side": "buy", "conviction": 0.81,
     "rationale": "Mention velocity + 8-K tone positive", "exit_intent": "",
     "ts": "2024-02-22T14:30:00+00:00", "status": "CLOSED", "outcome_pct": 6.4},
    {"ticker": "TSLA", "side": "sell", "conviction": 0.62,
     "rationale": "Deliveries miss + bearish sentiment shift", "exit_intent": "",
     "ts": "2024-01-25T14:30:00+00:00", "status": "CLOSED", "outcome_pct": -3.1},
]

# Field projections — keep internal columns (tenant_id) out of tool output, fix column order.
_SIGNAL_FIELDS = ("ticker", "side", "conviction", "rationale", "exit_intent", "ts", "status", "outcome_pct")
_POSITION_FIELDS = ("ticker", "side", "weight", "entry")
_TABLE_FIELDS = {"signals": _SIGNAL_FIELDS, "positions": _POSITION_FIELDS}
_SIGNAL_DDL = ("ticker TEXT, side TEXT, conviction REAL, rationale TEXT, "
               "exit_intent TEXT, ts TEXT, status TEXT, outcome_pct REAL")
_POSITION_DDL = "ticker TEXT, side TEXT, weight REAL, entry TEXT"
_TABLE_DDL = {"signals": _SIGNAL_DDL, "positions": _POSITION_DDL}


class FeedInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    limit: int = Field(default=10, ge=1, le=50)


class SignalRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(min_length=1, max_length=8)
    side: str = Field(min_length=1, max_length=8)              # buy | sell | short | cover
    conviction: float | None = Field(default=None, ge=0, le=1)
    rationale: str = Field(default="", max_length=500)
    exit_intent: str = Field(default="", max_length=500)
    ts: str = Field(default="", max_length=40)
    status: str = Field(default="OPEN", max_length=8)          # OPEN | CLOSED
    outcome_pct: float | None = Field(default=None, ge=-100, le=10000)


class PositionRow(BaseModel):
    model_config = ConfigDict(extra="forbid")
    ticker: str = Field(min_length=1, max_length=8)
    side: str = Field(min_length=1, max_length=8)
    weight: float = Field(ge=0, le=1)
    entry: str = Field(default="", max_length=40)


_ROW_MODEL = {"signals": SignalRow, "positions": PositionRow}


class IngestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dataset: Literal["signals", "positions"]
    mode: Literal["replace", "append"] = "replace"
    rows: list[dict] = Field(min_length=1, max_length=200)


def _project(row: dict, fields: tuple[str, ...]) -> dict:
    """Strip internal columns (tenant_id) and normalize order for output."""
    return {f: row.get(f) for f in fields}


class SocialTraderProvider(Provider):
    manifest_path = "provider.json"

    # ---- owner ingest (trader:admin; owner-gated at the gateway) ----------
    @tool("signals.ingest", input_model=IngestInput)
    def signals_ingest(self, ctx, dataset: str, mode: str = "replace", rows: list[dict] | None = None):
        # Ownership is already enforced by the gateway's :admin gate; the body just persists.
        if ctx.tenant_db is None:
            raise ToolError(ErrorCode.INTERNAL_ERROR, "Tenant storage unavailable; cannot ingest")
        model = _ROW_MODEL[dataset]
        try:
            parsed = [model(**r).model_dump() for r in (rows or [])]
        except ValidationError as e:
            first = e.errors()[0]
            field = ".".join(str(p) for p in first.get("loc", ()) if isinstance(p, str))
            raise ToolError(ErrorCode.VALIDATION_ERROR, first.get("msg", "Invalid row"),
                            field=field or None)
        db = ctx.tenant_db
        db.create_table(dataset, _TABLE_DDL[dataset])
        if mode == "replace":
            db.delete(dataset)
        for row in parsed:
            db.insert(dataset, row)
        total = len(db.query(dataset))
        return ctx.json_text({"dataset": dataset, "mode": mode,
                              "ingested": len(parsed), "total": total})

    # ---- live-backed reads (fall back to the static seed when empty) ------
    def _rows(self, ctx, dataset: str, fallback: list[dict]) -> list[dict]:
        db = ctx.tenant_db
        if db is None:
            return [dict(r) for r in fallback]
        db.create_table(dataset, _TABLE_DDL[dataset])
        live = db.query(dataset)
        if not live:
            return [dict(r) for r in fallback]
        return [_project(r, _TABLE_FIELDS[dataset]) for r in live]

    @tool("signals.feed", input_model=FeedInput)
    def signals_feed(self, ctx, limit: int = 10):
        signals = sorted(self._rows(ctx, "signals", _SIGNALS),
                         key=lambda s: s.get("ts") or "", reverse=True)
        live = [{k: v for k, v in s.items() if k != "outcome_pct"} for s in signals[:limit]]
        return ctx.json_text({"subscriber": ctx.principal.id, "signals": live, "count": len(live)})

    @tool("signals.history")
    def signals_history(self, ctx):
        signals = self._rows(ctx, "signals", _SIGNALS)
        return ctx.json_text({"signals": signals, "count": len(signals)})

    @tool("portfolio.positions")
    def portfolio_positions(self, ctx):
        positions = self._rows(ctx, "positions", _PORTFOLIO)
        return ctx.json_text({"positions": positions, "count": len(positions)})
