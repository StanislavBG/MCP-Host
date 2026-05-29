"""MCP-Host Provider SDK — the public surface a provider imports.

    from mcp_host.sdk import Provider, tool, ToolError, ErrorCode, ToolContext, Principal
"""

from mcp_host.sdk.context import Principal, ToolContext
from mcp_host.sdk.errors import ErrorCode, ErrorDetail, ErrorResponse, ToolError
from mcp_host.sdk.provider import PROTOCOL_VERSION, Provider, tool

__all__ = [
    "Provider",
    "tool",
    "ToolError",
    "ErrorCode",
    "ErrorDetail",
    "ErrorResponse",
    "ToolContext",
    "Principal",
    "PROTOCOL_VERSION",
]
