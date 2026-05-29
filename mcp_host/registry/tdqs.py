"""Tool Definition Quality Score (TDQS) — the validate-time quality gate.

Adapted from Glama's published TDQS idea: tools with weak descriptions get selected far less
often by LLMs, so a provider below threshold is NOT deployed/syndicated. We score each tool's
manifest entry on 5 cheap, deterministic dimensions (no LLM needed), 0..1 each:

  description_present   does it have a non-trivial description?
  description_length    is the description substantive (>= 40 chars)?
  param_schema          does the tool declare an input schema (beyond empty object)?
  annotations           does it declare behaviour hints (readOnlyHint/destructiveHint/...)?
  scope_clarity         is a scope set and namespaced (area:verb)?

Server score = 0.6 * mean(tool scores) + 0.4 * min(tool score)  (penalize the worst tool),
matching the "pull down the weakest tool" weighting. Default gate: 0.6.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

GATE = 0.6
SCOPE_RE = re.compile(r"^[a-z][a-z0-9-]*:[a-z][a-z0-9-]*$")


@dataclass
class ToolScore:
    name: str
    score: float
    reasons: list[str]


def score_tool(tool: dict[str, Any], provider) -> ToolScore:
    dims: dict[str, float] = {}
    reasons: list[str] = []

    desc = (tool.get("description") or "").strip()
    if not desc:
        # fall back to the live tools/list description (SDK may supply it)
        desc = ""
    dims["description_present"] = 1.0 if desc else 0.0
    if not desc:
        reasons.append("no description")
    dims["description_length"] = 1.0 if len(desc) >= 40 else (0.5 if desc else 0.0)
    if desc and len(desc) < 40:
        reasons.append("description too short (<40 chars)")

    # input schema beyond empty object?
    schema = {}
    try:
        schema = provider._input_schema(tool["name"])  # type: ignore[attr-defined]
    except Exception:
        pass
    has_props = bool(schema.get("properties"))
    dims["param_schema"] = 1.0 if has_props else 0.5  # a no-arg tool isn't penalized to zero
    if not has_props:
        reasons.append("no input parameters declared")

    ann = tool.get("annotations") or {}
    dims["annotations"] = 1.0 if ann else 0.0
    if not ann:
        reasons.append("no behaviour annotations (readOnlyHint/destructiveHint)")

    scope = tool.get("scope", "")
    dims["scope_clarity"] = 1.0 if SCOPE_RE.match(scope) else 0.0
    if not SCOPE_RE.match(scope):
        reasons.append("scope not namespaced as area:verb")

    return ToolScore(tool["name"], sum(dims.values()) / len(dims), reasons)


def score_provider(provider) -> tuple[float, list[ToolScore]]:
    # Use live tools/list so SDK-supplied descriptions count.
    listed = {t["name"]: t for t in provider.list_tools()["tools"]}
    scores: list[ToolScore] = []
    for t in provider.manifest["tools"]:
        merged = dict(t)
        merged.setdefault("description", listed.get(t["name"], {}).get("description", ""))
        merged.setdefault("annotations", listed.get(t["name"], {}).get("annotations", {}))
        scores.append(score_tool(merged, provider))
    if not scores:
        return 0.0, []
    vals = [s.score for s in scores]
    server = 0.6 * (sum(vals) / len(vals)) + 0.4 * min(vals)
    return round(server, 3), scores


def passes(provider, gate: float = GATE) -> tuple[bool, float, list[ToolScore]]:
    server, scores = score_provider(provider)
    return server >= gate, server, scores
