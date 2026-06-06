"""Pure, backend-agnostic normalization for managed-dataset query/write.

Managed-dataset rows are free-form JSON documents; query filters and sorts reference fields by
name *inside* the document. This module validates and normalizes the untrusted query/write inputs
ONCE — field names, operators, limits, cursor — so the SQLite and Postgres tenant layers only have
to render already-safe pieces (field names are regex-validated; all values stay parameterized).

No SQL here, no I/O — just validation + small encoders. Raises DatasetError (a ValueError) on any
malformed input, which callers map to a VALIDATION_ERROR envelope.
"""

from __future__ import annotations

import base64
import re
from typing import Any

# Single-level document field name. Deliberately strict: it is the only user-supplied token that
# reaches a JSON path, so it must never carry anything but identifier characters.
FIELD_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# op -> SQL comparison operator. 'in' is handled separately (variadic).
OPS = {"eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<="}

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
MAX_ROWS = 1000           # rows accepted per publish call
MAX_DOC_BYTES = 64 * 1024  # serialized size cap per row
WRITE_MODES = ("replace", "append", "upsert")


class DatasetError(ValueError):
    """Malformed dataset query/write input. Maps to VALIDATION_ERROR upstream."""


def validate_field(field: str) -> str:
    if not isinstance(field, str) or not FIELD_RE.match(field):
        raise DatasetError(f"illegal field name: {field!r}")
    return field


def validate_dataset_name(name: str) -> str:
    if not isinstance(name, str) or not re.match(r"^[a-z][a-z0-9_]{0,39}$", name):
        raise DatasetError(f"illegal dataset name: {name!r}")
    return name


def _scalar(v: Any) -> Any:
    if v is not None and not isinstance(v, (str, int, float, bool)):
        raise DatasetError("filter value must be a string/number/bool/null")
    return v


def normalize_filters(filters: Any) -> list[tuple[str, str, Any]]:
    """Validate `filters` into a list of (field, op, value). op ∈ OPS ∪ {"in"}.

    Accepts `{field: value}` (eq) or `{field: {op: value}}`. O(#filters); tiny.
    """
    if filters is None:
        return []
    if not isinstance(filters, dict):
        raise DatasetError("filters must be an object")
    out: list[tuple[str, str, Any]] = []
    for field, cond in filters.items():
        validate_field(field)
        if isinstance(cond, dict):
            if len(cond) != 1:
                raise DatasetError(f"filter for '{field}' must have exactly one operator")
            op, val = next(iter(cond.items()))
            if op == "in":
                if not isinstance(val, list) or not val:
                    raise DatasetError(f"'in' filter for '{field}' needs a non-empty array")
                for item in val:
                    _scalar(item)
                out.append((field, "in", val))
            elif op in OPS:
                _scalar(val)
                out.append((field, op, val))
            else:
                raise DatasetError(f"unknown operator '{op}' for '{field}'")
        else:
            _scalar(cond)
            out.append((field, "eq", cond))
    return out


def parse_sort(sort: Any) -> tuple[str, str] | None:
    """`"field"` -> (field, "ASC"); `"-field"` -> (field, "DESC"); None/"" -> None."""
    if not sort:
        return None
    if not isinstance(sort, str):
        raise DatasetError("sort must be a string")
    desc = sort.startswith("-")
    field = sort[1:] if desc else sort
    validate_field(field)
    return field, "DESC" if desc else "ASC"


def clamp_limit(limit: Any) -> int:
    if limit is None:
        return DEFAULT_LIMIT
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise DatasetError("limit must be a positive integer")
    return min(limit, MAX_LIMIT)


def decode_cursor(cursor: Any) -> int:
    """Opaque offset cursor. None/"" -> 0. Invalid -> DatasetError."""
    if not cursor:
        return 0
    if not isinstance(cursor, str):
        raise DatasetError("cursor must be a string")
    try:
        return max(0, int(base64.urlsafe_b64decode(cursor.encode()).decode()))
    except Exception:
        raise DatasetError("invalid cursor")


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(str(offset).encode()).decode()


def validate_mode(mode: Any) -> str:
    if mode not in WRITE_MODES:
        raise DatasetError(f"mode must be one of {WRITE_MODES}")
    return mode
