"""M0 — Protocol & SDK acceptance tests.

Acceptance criterion from the plan: a hello-world provider validates + serves
initialize / tools/list / tools/call locally.
"""

from __future__ import annotations

import pytest

from mcp_host.sdk import ErrorCode, ToolError
from mcp_host.sdk.manifest import ManifestError, price_is_free, validate_manifest
from mcp_host.sdk.provider import PROTOCOL_VERSION


def test_manifest_valid(hello):
    assert hello.id == "hello"
    assert hello.price_map() == {"echo": "0.00", "shout": "0.01"}
    assert hello.scope_map() == {"echo": "hello:read", "shout": "hello:write"}


def test_initialize(hello, ctx):
    info = hello.dispatch(ctx, "initialize", {})
    assert info["protocolVersion"] == PROTOCOL_VERSION
    assert info["serverInfo"]["name"] == "hello"


def test_tools_list(hello, ctx):
    res = hello.dispatch(ctx, "tools/list", {})
    names = {t["name"] for t in res["tools"]}
    assert names == {"echo", "shout"}
    echo = next(t for t in res["tools"] if t["name"] == "echo")
    assert echo["inputSchema"]["properties"]["message"]["maxLength"] == 200
    assert echo["annotations"]["readOnlyHint"] is True


def test_tools_call_ok(hello, ctx):
    res = hello.dispatch(ctx, "tools/call", {"name": "echo", "arguments": {"message": "hi", "times": 3}})
    assert res["content"][0]["text"] == "hi hi hi"


def test_tools_call_validation_rejects_extra(hello, ctx):
    with pytest.raises(ToolError) as ei:
        hello.call_tool(ctx, "echo", {"message": "x", "bogus": 1})
    assert ei.value.code == ErrorCode.UNKNOWN_FIELDS


def test_tools_call_validation_rejects_bad_value(hello, ctx):
    with pytest.raises(ToolError) as ei:
        hello.call_tool(ctx, "echo", {"message": "x", "times": 99})
    assert ei.value.code == ErrorCode.VALIDATION_ERROR
    assert ei.value.field == "times"


def test_unknown_tool(hello, ctx):
    with pytest.raises(ToolError) as ei:
        hello.call_tool(ctx, "nope", {})
    assert ei.value.code == ErrorCode.TOOL_NOT_FOUND


def test_unknown_method(hello, ctx):
    with pytest.raises(ToolError) as ei:
        hello.dispatch(ctx, "resources/list", {})
    assert ei.value.code == ErrorCode.METHOD_NOT_FOUND


def test_price_is_free():
    assert price_is_free("$0.00") and price_is_free("0") and price_is_free("")
    assert not price_is_free("0.01") and not price_is_free("$1.00")


def test_manifest_rejects_unknown_field():
    bad = {"id": "x", "bogus": 1}
    with pytest.raises(ManifestError):
        validate_manifest(bad)


def test_manifest_rejects_undeclared_scope():
    bad = {
        "id": "x", "display_name": "X", "discipline": "demo", "version": "0.1.0",
        "summary": "s", "transport": "streamable-http",
        "auth": {"modes": ["api_key"], "scopes": ["x:read"]},
        "data": {"postgres_schema": "x"},
        "tools": [{"name": "t", "scope": "x:write", "price_usdc": "0.00"}],
    }
    with pytest.raises(ManifestError) as ei:
        validate_manifest(bad)
    assert "not declared in auth.scopes" in str(ei.value)


def test_tool_code_manifest_mismatch_fails_boot():
    from tests.conftest import HELLO_MANIFEST
    from mcp_host.sdk import Provider, tool

    m = dict(HELLO_MANIFEST)
    m["tools"] = HELLO_MANIFEST["tools"] + [{"name": "ghost", "scope": "hello:read", "price_usdc": "0.00"}]

    class Broken(Provider):
        def __init__(self):
            super().__init__(manifest=m)

        @tool("echo")
        def echo(self, ctx, message: str = ""):
            return ctx.text(message)

        @tool("shout")
        def shout(self, ctx, message: str = ""):
            return ctx.text(message)

    with pytest.raises(ManifestError) as ei:
        Broken()
    assert "ghost" in str(ei.value)
