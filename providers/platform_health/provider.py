"""Platform Health — the first real first-party MCP hosted on MCP-Host.

Unlike the demo pilots, this provider serves NO external data: it reports the live state of the
host itself (mounted providers, storage backend, uptime, config warnings) and offers a ping for
end-to-end reachability. That makes it the natural first production provider — it dogfoods the
platform and needs no Postgres/artifact persistence to be useful.

It is a "platform" provider: the server injects a live view of the host via `bind_host()` at
boot, so its tool bodies can read the gateway's mounted providers and store. Third-party
providers never get this — they only ever see their own RLS-scoped `ctx`.
"""

from __future__ import annotations

import time

from pydantic import BaseModel, ConfigDict, Field

from mcp_host.sdk import ErrorCode, Provider, ToolError, tool


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


class CheckProviderInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(min_length=1, max_length=40)


class PingInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str | None = Field(default=None, max_length=200)


class PlatformHealthProvider(Provider):
    manifest_path = "provider.json"

    def bind_host(self, gw, meta: dict) -> None:
        """Injected by the server at boot — a live handle on the host (gateway + boot metadata)."""
        self._gw = gw
        self._meta = dict(meta)

    # ---- internal helpers ------------------------------------------------
    def _providers(self):
        gw = getattr(self, "_gw", None)
        return gw.providers() if gw else []

    @tool("platform_status")
    def platform_status(self, ctx):
        meta = getattr(self, "_meta", {})
        started = meta.get("started_at")
        warnings = meta.get("config_warnings", [])
        backend = meta.get("backend", "unknown")
        return ctx.json_text({
            "status": "degraded" if (warnings or "unreachable" in str(backend)) else "ok",
            "service": "mcp-host",
            "version": meta.get("version", "0"),
            "backend": backend,
            "providers_mounted": len(self._providers()),
            "uptime_s": round(time.time() - started, 1) if started else None,
            "config_warnings": warnings,
            "built_at": _now_iso(),
        })

    @tool("list_providers")
    def list_providers(self, ctx):
        from mcp_host.registry.tdqs import passes

        out = []
        for p in self._providers():
            try:
                ok, score, _ = passes(p)
            except Exception:
                ok, score = False, 0.0
            out.append({
                "id": p.id,
                "display_name": p.manifest["display_name"],
                "discipline": p.manifest["discipline"],
                "demo": bool(p.manifest.get("demo", False)),
                "tools": [t["name"] for t in p.manifest["tools"]],
                "tdqs": score,
                "tdqs_pass": ok,
            })
        out.sort(key=lambda r: (r["demo"], r["id"]))
        return ctx.json_text({"providers": out, "count": len(out)})

    @tool("check_provider", input_model=CheckProviderInput)
    def check_provider(self, ctx, provider_id: str):
        gw = getattr(self, "_gw", None)
        p = gw.provider(provider_id) if gw else None
        if not p:
            raise ToolError(ErrorCode.TOOL_NOT_FOUND, f"No provider '{provider_id}' mounted")
        try:
            health = p.health(ctx)
        except Exception as e:  # a provider's health hook should never take the platform down
            health = {"status": "error", "detail": f"health hook raised: {type(e).__name__}"}
        return ctx.json_text({"provider": provider_id, "health": health})

    @tool("ping", input_model=PingInput)
    def ping(self, ctx, message: str | None = None):
        return ctx.json_text({"pong": True, "echo": message, "ts": _now_iso()})

    # ---- provider hooks --------------------------------------------------
    def health(self, ctx):
        return {"status": "ok", "provider": self.id,
                "providers_mounted": len(self._providers())}

    def catalog(self, ctx):
        return {"provider": self.id,
                "describes": "the MCP-Host platform itself",
                "tools": [t["name"] for t in self.manifest["tools"]]}
