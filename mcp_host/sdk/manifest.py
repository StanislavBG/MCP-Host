"""provider.json loading + validation against schemas/provider.schema.json.

We validate with a small, dependency-free JSON-Schema subset checker covering exactly the
constructs our schema uses (type, required, additionalProperties, enum, pattern, minItems,
items, nested objects). This keeps the runtime lean (no jsonschema dep) while still failing
loudly on a malformed manifest. The price-is-free rule lives here too.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

SCHEMA_PATH = Path(__file__).resolve().parents[2] / "schemas" / "provider.schema.json"

FREE_PRICES = {"$0.00", "$0", "0", "$0.0", "0.00", ""}


def price_is_free(price: str | None) -> bool:
    return (price or "").strip() in FREE_PRICES


class ManifestError(ValueError):
    pass


def _validate(node: Any, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    t = schema.get("type")
    if t == "object":
        if not isinstance(node, dict):
            errors.append(f"{path}: expected object")
            return
        props = schema.get("properties", {})
        for req in schema.get("required", []):
            if req not in node:
                errors.append(f"{path}: missing required '{req}'")
        if schema.get("additionalProperties") is False:
            for key in node:
                if key not in props:
                    errors.append(f"{path}: unknown field '{key}'")
        for key, val in node.items():
            if key in props:
                _validate(val, props[key], f"{path}.{key}", errors)
    elif t == "array":
        if not isinstance(node, list):
            errors.append(f"{path}: expected array")
            return
        if "minItems" in schema and len(node) < schema["minItems"]:
            errors.append(f"{path}: needs >= {schema['minItems']} items")
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(node):
                _validate(item, item_schema, f"{path}[{i}]", errors)
    elif t == "string":
        if not isinstance(node, str):
            errors.append(f"{path}: expected string")
            return
        _check_string(node, schema, path, errors)
    elif t == "integer":
        if not isinstance(node, int) or isinstance(node, bool):
            errors.append(f"{path}: expected integer")
    elif t == "number":
        if not isinstance(node, (int, float)) or isinstance(node, bool):
            errors.append(f"{path}: expected number")
    elif t == "boolean":
        if not isinstance(node, bool):
            errors.append(f"{path}: expected boolean")

    # enum applies regardless of declared type
    if "enum" in schema and node not in schema["enum"]:
        errors.append(f"{path}: '{node}' not in {schema['enum']}")


def _check_string(node: str, schema: dict[str, Any], path: str, errors: list[str]) -> None:
    if "minLength" in schema and len(node) < schema["minLength"]:
        errors.append(f"{path}: too short")
    if "maxLength" in schema and len(node) > schema["maxLength"]:
        errors.append(f"{path}: too long")
    pat = schema.get("pattern")
    if pat and not re.search(pat, node):
        errors.append(f"{path}: '{node}' fails pattern {pat}")


def load_schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text())


def validate_manifest(manifest: dict[str, Any], schema: dict[str, Any] | None = None) -> None:
    """Raise ManifestError with all problems, or return None if valid.

    Beyond the schema, enforces cross-field invariants the JSON Schema can't express:
    every tool.scope must be declared in auth.scopes.
    """
    schema = schema or load_schema()
    errors: list[str] = []
    _validate(manifest, schema, "$", errors)

    declared = set(manifest.get("auth", {}).get("scopes", []))
    for i, tool in enumerate(manifest.get("tools", [])):
        scope = tool.get("scope")
        if scope and scope not in declared:
            errors.append(f"$.tools[{i}].scope '{scope}' not declared in auth.scopes")

    if errors:
        raise ManifestError("Invalid provider.json:\n  - " + "\n  - ".join(errors))


def load_manifest(path: str | Path) -> dict[str, Any]:
    manifest = json.loads(Path(path).read_text())
    validate_manifest(manifest)
    return manifest
