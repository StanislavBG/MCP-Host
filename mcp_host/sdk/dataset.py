"""Managed-dataset provider — the host stores an agent's data and serves it.

The second self-serve kind (alongside the declarative proxy of enh-002). The agent submits a
terse manifest declaring `datasets[]`; the host EXPANDS it into a full manifest by synthesizing,
per dataset, three tools — `<ds>.query`, `<ds>.get`, `<ds>.publish` — plus the `:read`/`:admin`
scopes. `ManagedDatasetProvider` is host-owned code (no guest code) that implements those tools
generically over the RLS-isolated tenant store (`mcp_host/data/tenant.py` dataset_* ops).

Rows are free-form JSON documents with one designated key field; query filters/sorts on document
fields. publish is `:admin`-scoped, so the gateway authorizes it by OWNERSHIP — an agent publishes
with only its registration API key, no human in the loop.
"""

from __future__ import annotations

import time
from typing import Any

from mcp_host.data import dataset_sql as ds
from mcp_host.sdk import manifest as manifest_mod
from mcp_host.sdk.context import ToolContext
from mcp_host.sdk.errors import ErrorCode, ToolError
from mcp_host.sdk.provider import Provider

SCHEMA_VERSION = "1"


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())


def _envelope(payload: Any) -> dict[str, Any]:
    return {"payload": payload, "built_at": _now_iso(), "schema_version": SCHEMA_VERSION}


def _dataset_tools(name: str, key: str, desc: str, read_scope: str, admin_scope: str) -> list[dict]:
    """The three generated tools for one dataset. Descriptions are written to clear the TDQS gate
    (>= 40 chars + annotations + a non-empty input_schema)."""
    tail = f" {desc}" if desc else ""
    return [
        {
            "name": f"{name}.query", "scope": read_scope, "price_usdc": "0.00",
            "description": f"Query the '{name}' dataset: filter records by document fields, sort, "
                           f"and paginate the results.{tail}",
            "annotations": {"readOnlyHint": True, "openWorldHint": False},
            "input_schema": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "filters": {"type": "object",
                                "description": "field -> value (equality) or field -> {op: value}, "
                                               "op in eq/ne/gt/gte/lt/lte/in"},
                    "sort": {"type": "string", "description": "'field' (asc) or '-field' (desc)"},
                    "limit": {"type": "integer"},
                    "cursor": {"type": "string", "description": "opaque next-page cursor"},
                },
            },
        },
        {
            "name": f"{name}.get", "scope": read_scope, "price_usdc": "0.00",
            "description": f"Fetch one '{name}' record by its '{key}' key (the most recently "
                           f"published row for that key).{tail}",
            "annotations": {"readOnlyHint": True},
            "input_schema": {
                "type": "object", "additionalProperties": False,
                "properties": {"key": {"type": "string"}}, "required": ["key"],
            },
        },
        {
            "name": f"{name}.publish", "scope": admin_scope, "price_usdc": "0.00",
            "description": f"Owner-only: publish rows into the '{name}' dataset using replace, "
                           f"append, or upsert (by '{key}') mode.",
            "annotations": {"readOnlyHint": False, "idempotentHint": True},
            "input_schema": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "mode": {"type": "string", "enum": list(ds.WRITE_MODES)},
                    "rows": {"type": "array"},
                }, "required": ["rows"],
            },
        },
    ]


def expand_dataset_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Expand a terse `datasets`-only manifest into a full, schema-valid one: synthesize
    auth.scopes, data.postgres_schema, and the generated tools. Idempotent enough that an
    already-expanded manifest (re-loaded at boot) returns equivalent output.
    """
    m = dict(manifest)
    datasets = m.get("datasets")
    if not isinstance(datasets, list) or not datasets:
        raise manifest_mod.ManifestError("a managed-dataset provider must declare a non-empty 'datasets' array")
    pid = m.get("id") or ""
    read_scope, admin_scope = f"{pid}:read", f"{pid}:admin"

    tools: list[dict] = []
    seen: set[str] = set()
    for d in datasets:
        if not isinstance(d, dict):
            raise manifest_mod.ManifestError("each dataset must be an object")
        name, key = d.get("name"), d.get("key")
        try:
            ds.validate_dataset_name(name)
            ds.validate_field(key)
        except ds.DatasetError as e:
            raise manifest_mod.ManifestError(str(e))
        if name in seen:
            raise manifest_mod.ManifestError(f"duplicate dataset '{name}'")
        seen.add(name)
        tools.extend(_dataset_tools(name, key, (d.get("description") or "").strip(),
                                    read_scope, admin_scope))

    m["auth"] = {"modes": ["api_key", "oauth2.1"], "scopes": [read_scope, admin_scope]}
    data = dict(m.get("data") or {})
    data.setdefault("postgres_schema", pid.replace("-", "_"))
    m["data"] = data
    m["tools"] = tools
    return m


class ManagedDatasetProvider(Provider):
    """Host-owned provider that serves an agent's published datasets. No @tool methods — it routes
    `<ds>.query|get|publish` to the tenant dataset store from the (already-expanded) manifest."""

    def __init__(self, manifest: dict[str, Any]) -> None:
        manifest_mod.validate_manifest(manifest)
        if not manifest.get("datasets"):
            raise manifest_mod.ManifestError("ManagedDatasetProvider requires a 'datasets' block")
        self.manifest = manifest
        self.id = manifest["id"]
        self._key_for = {d["name"]: d["key"] for d in manifest["datasets"]}
        self._tools = {t["name"]: t for t in manifest["tools"]}

    # ---- provisioning ----------------------------------------------------
    def provision(self, tenant_db) -> None:
        """Create each dataset's table (+ declared indexes). Called at mount and boot; the
        store ops also create-if-missing, so this is an optimization for the index hints."""
        for d in self.manifest["datasets"]:
            tenant_db.dataset_provision(d["name"], d.get("indexed") or ())

    # ---- base Provider surface (no @tool reconciliation) -----------------
    def _input_schema(self, tool_name: str) -> dict[str, Any]:
        t = self._tools.get(tool_name, {})
        return t.get("input_schema") or {"type": "object", "properties": {}}

    def call_tool(self, ctx: ToolContext, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in self._tools:
            raise ToolError(ErrorCode.TOOL_NOT_FOUND, f"Unknown tool '{tool_name}'")
        if ctx.tenant_db is None:
            raise ToolError(ErrorCode.INTERNAL_ERROR, "tenant store unavailable")
        dataset, _, op = tool_name.rpartition(".")
        if dataset not in self._key_for:
            raise ToolError(ErrorCode.TOOL_NOT_FOUND, f"Unknown dataset for tool '{tool_name}'")
        args = arguments or {}
        try:
            if op == "query":
                res = ctx.tenant_db.dataset_query(dataset, args.get("filters"), args.get("sort"),
                                                  args.get("limit"), args.get("cursor"))
                return _envelope(res)
            if op == "get":
                doc = ctx.tenant_db.dataset_get(dataset, args.get("key"))
                return _envelope({"record": doc, "found": doc is not None})
            if op == "publish":
                # Ownership already enforced by the gateway's :admin gate; just persist.
                n = ctx.tenant_db.dataset_write(dataset, self._key_for[dataset],
                                                args.get("rows"), args.get("mode", "upsert"))
                return _envelope({"dataset": dataset, "written": n, "mode": args.get("mode", "upsert")})
        except ds.DatasetError as e:
            raise ToolError(ErrorCode.VALIDATION_ERROR, str(e))
        raise ToolError(ErrorCode.TOOL_NOT_FOUND, f"Unsupported dataset op '{op}'")
