"""Shared test fixtures: a minimal in-code provider used across SDK/gateway tests."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ConfigDict, Field

from mcp_host.sdk import Principal, Provider, ToolContext, tool


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    message: str = Field(min_length=1, max_length=200)
    times: int = Field(default=1, ge=1, le=5)


HELLO_MANIFEST = {
    "id": "hello",
    "display_name": "Hello Provider",
    "discipline": "demo",
    "version": "0.1.0",
    "summary": "A minimal provider used by the test suite.",
    "transport": "streamable-http",
    "auth": {"modes": ["oauth2.1", "api_key"], "scopes": ["hello:read", "hello:write"]},
    "data": {"postgres_schema": "hello"},
    "tools": [
        {"name": "echo", "scope": "hello:read", "price_usdc": "0.00",
         "annotations": {"readOnlyHint": True}},
        {"name": "shout", "scope": "hello:write", "price_usdc": "0.01"},
    ],
    "limits": {"rate_per_min": 60, "max_request_kb": 50},
}


class HelloProvider(Provider):
    def __init__(self):
        super().__init__(manifest=HELLO_MANIFEST)

    @tool("echo", input_model=EchoInput)
    def echo(self, ctx, message: str, times: int = 1):
        return ctx.text(" ".join([message] * times))

    @tool("shout")
    def shout(self, ctx, message: str = "hi"):
        return ctx.text(str(message).upper())


@pytest.fixture
def hello() -> HelloProvider:
    return HelloProvider()


@pytest.fixture
def ctx() -> ToolContext:
    return ToolContext(
        provider_id="hello",
        principal=Principal(id="tester", kind="user", plan="free",
                            scopes=("hello:read", "hello:write")),
    )
