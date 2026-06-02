"""platform-health — the first first-party provider. Exercise its tools via the SDK surface."""

from __future__ import annotations

import json
import time

from mcp_host.sdk import Principal, ToolContext
from providers.platform_health.provider import PlatformHealthProvider


class _FakeGateway:
    """Minimal stand-in for the host view bind_host() receives."""

    def __init__(self, providers):
        self._providers = providers

    def providers(self):
        return self._providers

    def provider(self, pid):
        return next((p for p in self._providers if p.id == pid), None)


def _ctx():
    return ToolContext("platform-health", Principal(id="t", scopes=("platform:read",)))


def _payload(res):
    return json.loads(res["content"][0]["text"])


def _bound():
    p = PlatformHealthProvider()
    gw = _FakeGateway([p])
    p.bind_host(gw, {"version": "0.1.0", "backend": "postgres",
                     "started_at": time.time() - 5, "config_warnings": []})
    return p


def test_platform_status_reports_live_state():
    p = _bound()
    out = _payload(p.call_tool(_ctx(), "platform_status", {}))
    assert out["status"] == "ok"
    assert out["service"] == "mcp-host"
    assert out["backend"] == "postgres"
    assert out["providers_mounted"] == 1
    assert out["uptime_s"] >= 5


def test_platform_status_degrades_on_warnings():
    p = PlatformHealthProvider()
    p.bind_host(_FakeGateway([p]), {"backend": "sqlite-memory (postgres unreachable)",
                                    "started_at": time.time(), "config_warnings": []})
    out = _payload(p.call_tool(_ctx(), "platform_status", {}))
    assert out["status"] == "degraded"  # backend says unreachable


def test_list_providers_includes_demo_flag():
    p = _bound()
    out = _payload(p.call_tool(_ctx(), "list_providers", {}))
    assert out["count"] == 1
    row = out["providers"][0]
    assert row["id"] == "platform-health" and row["demo"] is False and row["tdqs_pass"]


def test_check_provider_ok_and_missing():
    p = _bound()
    ok = _payload(p.call_tool(_ctx(), "check_provider", {"provider_id": "platform-health"}))
    assert ok["health"]["status"] == "ok"
    from mcp_host.sdk import ToolError
    try:
        p.call_tool(_ctx(), "check_provider", {"provider_id": "nope"})
        assert False, "expected ToolError"
    except ToolError:
        pass


def test_ping_echoes():
    p = _bound()
    out = _payload(p.call_tool(_ctx(), "ping", {"message": "hi"}))
    assert out["pong"] is True and out["echo"] == "hi" and out["ts"]
