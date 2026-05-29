"""MCP content-envelope helpers.

A tool returns MCP `content` blocks. These helpers build the spec-shaped result so providers
never hand-assemble the wire format.
"""

from __future__ import annotations

import json
from typing import Any


def text(s: str) -> dict[str, Any]:
    """A single text content block result: {"content": [{"type": "text", "text": s}]}."""
    return {"content": [{"type": "text", "text": s}]}


def json_text(obj: Any) -> dict[str, Any]:
    """Serialize an object to a pretty JSON text block (the common case for data tools)."""
    return text(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def blocks(*items: dict[str, Any]) -> dict[str, Any]:
    """Multiple raw content blocks."""
    return {"content": list(items)}
