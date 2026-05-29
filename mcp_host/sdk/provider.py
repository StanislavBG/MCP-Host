"""Provider base class + @tool decorator — the contract every MCP subclasses.

A provider implements tool bodies as @tool methods and ships a provider.json. The SDK gives
it MCP JSON-RPC dispatch (initialize / tools/list / tools/call), input validation, the error
envelope, and the content helpers — so the provider writes ONLY business logic.

The gateway owns auth/billing/metering and calls `dispatch()` after those pass. At boot the
SDK reconciles the @tool methods against provider.json and fails loudly on any mismatch, so a
manifest can never drift from the code.
"""

from __future__ import annotations

import inspect
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from mcp_host.sdk import manifest as manifest_mod
from mcp_host.sdk.context import ToolContext
from mcp_host.sdk.errors import ErrorCode, ToolError

PROTOCOL_VERSION = "2025-11-25"  # MCP spec revision we target


def tool(name: str, *, input_model: type[BaseModel] | None = None) -> Callable:
    """Mark a method as an MCP tool.

    `name` must match a tools[].name in provider.json. If `input_model` (a Pydantic model) is
    given, arguments are validated through it (extra="forbid" recommended) before the body runs.
    """

    def deco(fn: Callable) -> Callable:
        fn.__mcp_tool__ = {"name": name, "input_model": input_model}  # type: ignore[attr-defined]
        return fn

    return deco


class Provider:
    """Subclass this. Set `manifest_path` (relative to the subclass's module) and add @tool methods."""

    manifest_path: str = "provider.json"

    def __init__(self, manifest: dict[str, Any] | None = None) -> None:
        if manifest is None:
            base = Path(inspect.getfile(self.__class__)).resolve().parent
            manifest = manifest_mod.load_manifest(base / self.manifest_path)
        else:
            manifest_mod.validate_manifest(manifest)
        self.manifest = manifest
        self.id: str = manifest["id"]
        self._tools: dict[str, dict[str, Any]] = self._collect_tools()
        self._reconcile()

    # ---- boot-time wiring ------------------------------------------------
    def _collect_tools(self) -> dict[str, dict[str, Any]]:
        found: dict[str, dict[str, Any]] = {}
        for _, member in inspect.getmembers(self, predicate=callable):
            meta = getattr(member, "__mcp_tool__", None)
            if meta:
                found[meta["name"]] = {"fn": member, "input_model": meta["input_model"]}
        return found

    def _reconcile(self) -> None:
        declared = {t["name"] for t in self.manifest["tools"]}
        implemented = set(self._tools)
        missing = declared - implemented
        extra = implemented - declared
        problems = []
        if missing:
            problems.append(f"declared in provider.json but not implemented: {sorted(missing)}")
        if extra:
            problems.append(f"implemented but not declared in provider.json: {sorted(extra)}")
        if problems:
            raise manifest_mod.ManifestError(
                f"Provider '{self.id}' tool mismatch: " + "; ".join(problems)
            )

    # ---- MCP JSON-RPC surface -------------------------------------------
    def server_info(self) -> dict[str, Any]:
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "serverInfo": {"name": self.id, "version": self.manifest["version"]},
            "capabilities": {"tools": {}},
            "instructions": self.manifest.get("summary", ""),
        }

    def list_tools(self) -> dict[str, Any]:
        out = []
        for t in self.manifest["tools"]:
            out.append(
                {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "inputSchema": self._input_schema(t["name"]),
                    "annotations": t.get("annotations", {}),
                }
            )
        return {"tools": out}

    def _input_schema(self, tool_name: str) -> dict[str, Any]:
        model = self._tools[tool_name]["input_model"]
        if model is not None:
            return model.model_json_schema()
        return {"type": "object", "properties": {}}

    def call_tool(self, ctx: ToolContext, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in self._tools:
            raise ToolError(ErrorCode.TOOL_NOT_FOUND, f"Unknown tool '{tool_name}'")
        spec = self._tools[tool_name]
        model = spec["input_model"]
        if model is not None:
            try:
                parsed = model(**(arguments or {}))
            except ValidationError as e:
                first = e.errors()[0]
                field = ".".join(str(p) for p in first.get("loc", ()) if isinstance(p, str))
                etype = first.get("type", "")
                code = ErrorCode.UNKNOWN_FIELDS if "extra" in etype else ErrorCode.VALIDATION_ERROR
                raise ToolError(code, first.get("msg", "Invalid input"), field=field or None)
            kwargs = parsed.model_dump()
        else:
            kwargs = dict(arguments or {})
        return spec["fn"](ctx, **kwargs)

    def dispatch(self, ctx: ToolContext, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Handle one JSON-RPC method body (result only; the wire layer wraps the envelope)."""
        if method == "initialize":
            return self.server_info()
        if method == "tools/list":
            return self.list_tools()
        if method == "tools/call":
            return self.call_tool(ctx, params.get("name", ""), params.get("arguments", {}))
        raise ToolError(ErrorCode.METHOD_NOT_FOUND, f"Unknown method '{method}'")

    # ---- provider-overridable hooks -------------------------------------
    def health(self, ctx: ToolContext) -> dict[str, Any]:
        return {"status": "ok", "provider": self.id, "version": self.manifest["version"]}

    def catalog(self, ctx: ToolContext) -> dict[str, Any]:
        """Self-describe the data this provider serves (generalizes edgar-rag /data)."""
        return {"provider": self.id, "tools": [t["name"] for t in self.manifest["tools"]]}

    # ---- manifest-derived config (read by the host, not the provider) ----
    def price_map(self) -> dict[str, str]:
        return {t["name"]: t.get("price_usdc", "0.00") for t in self.manifest["tools"]}

    def scope_map(self) -> dict[str, str]:
        return {t["name"]: t["scope"] for t in self.manifest["tools"]}
