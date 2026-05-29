"""Shared error model for the whole platform.

Generalizes edgar-rag's src/errors.py. Every provider and the gateway raise/return errors
through this single envelope so clients see one consistent shape and one set of codes.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class ErrorCode(str, Enum):
    # Request / validation
    PARSE_ERROR = "PARSE_ERROR"
    INVALID_REQUEST = "INVALID_REQUEST"
    METHOD_NOT_FOUND = "METHOD_NOT_FOUND"
    UNKNOWN_FIELDS = "UNKNOWN_FIELDS"
    VALIDATION_ERROR = "VALIDATION_ERROR"
    QUERY_TOO_LONG = "QUERY_TOO_LONG"
    # Routing / discovery
    PROVIDER_NOT_FOUND = "PROVIDER_NOT_FOUND"
    TOOL_NOT_FOUND = "TOOL_NOT_FOUND"
    # Auth / entitlements
    UNAUTHENTICATED = "UNAUTHENTICATED"
    FORBIDDEN_SCOPE = "FORBIDDEN_SCOPE"
    RATE_LIMIT_EXCEEDED = "RATE_LIMIT_EXCEEDED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    # Billing
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    FACILITATOR_UNAVAILABLE = "FACILITATOR_UNAVAILABLE"
    # Generic
    INTERNAL_ERROR = "INTERNAL_ERROR"


# JSON-RPC 2.0 numeric codes mapped from our domain codes (for the wire layer).
JSONRPC_CODE = {
    ErrorCode.PARSE_ERROR: -32700,
    ErrorCode.INVALID_REQUEST: -32600,
    ErrorCode.METHOD_NOT_FOUND: -32601,
    ErrorCode.VALIDATION_ERROR: -32602,
    ErrorCode.UNKNOWN_FIELDS: -32602,
    ErrorCode.QUERY_TOO_LONG: -32602,
    ErrorCode.PROVIDER_NOT_FOUND: -32004,
    ErrorCode.TOOL_NOT_FOUND: -32601,
    ErrorCode.UNAUTHENTICATED: -32001,
    ErrorCode.FORBIDDEN_SCOPE: -32002,
    ErrorCode.RATE_LIMIT_EXCEEDED: -32005,
    ErrorCode.QUOTA_EXCEEDED: -32005,
    ErrorCode.PAYMENT_REQUIRED: -32003,
    ErrorCode.FACILITATOR_UNAVAILABLE: -32003,
    ErrorCode.INTERNAL_ERROR: -32603,
}

# Suggested HTTP status for REST-style surfaces.
HTTP_STATUS = {
    ErrorCode.PARSE_ERROR: 400,
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.METHOD_NOT_FOUND: 404,
    ErrorCode.UNKNOWN_FIELDS: 400,
    ErrorCode.VALIDATION_ERROR: 400,
    ErrorCode.QUERY_TOO_LONG: 400,
    ErrorCode.PROVIDER_NOT_FOUND: 404,
    ErrorCode.TOOL_NOT_FOUND: 404,
    ErrorCode.UNAUTHENTICATED: 401,
    ErrorCode.FORBIDDEN_SCOPE: 403,
    ErrorCode.RATE_LIMIT_EXCEEDED: 429,
    ErrorCode.QUOTA_EXCEEDED: 429,
    ErrorCode.PAYMENT_REQUIRED: 402,
    ErrorCode.FACILITATOR_UNAVAILABLE: 503,
    ErrorCode.INTERNAL_ERROR: 500,
}


class ErrorDetail(BaseModel):
    code: ErrorCode
    message: str
    retry: bool = False
    field: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorDetail


class ToolError(Exception):
    """Raised inside a tool body (or the gateway) to return a structured error.

    The SDK/gateway catches this and renders the standard envelope. Providers should raise
    this instead of returning ad-hoc error strings.
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        retry: bool = False,
        field: str | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry = retry
        self.field = field

    def detail(self) -> ErrorDetail:
        return ErrorDetail(code=self.code, message=self.message, retry=self.retry, field=self.field)

    @property
    def http_status(self) -> int:
        return HTTP_STATUS.get(self.code, 500)

    @property
    def jsonrpc_code(self) -> int:
        return JSONRPC_CODE.get(self.code, -32603)
