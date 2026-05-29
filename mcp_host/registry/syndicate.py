"""Syndication — publish a provider to external registries from the single source of truth.

`syndicate(..., dry_run=True)` produces the artifacts (server.json + install snippets + the
target list) WITHOUT network calls, which is what CI and `mcp-host syndicate --dry-run` use.
A live run would shell out to `mcp-publisher publish` for the official registry (the canonical
feed Glama/mcp.so/PulseMCP ingest). Namespace ownership must be verified once beforehand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from mcp_host.registry.serverjson import install_snippets, to_server_json


@dataclass
class SyndicationPlan:
    server_json: dict[str, Any]
    targets: list[str]
    snippets: dict[str, str]
    warnings: list[str] = field(default_factory=list)


def plan_syndication(manifest: dict[str, Any], base_url: str) -> SyndicationPlan:
    syn = manifest.get("syndication", {})
    targets = [k for k, v in (
        ("official_registry", syn.get("official_registry")),
        ("glama", syn.get("glama")),
        ("mcp_so", syn.get("mcp_so")),
        ("pulsemcp", syn.get("pulsemcp")),
    ) if v]
    warnings = []
    if "owner_namespace" not in manifest and "official_registry" in targets:
        warnings.append("owner_namespace missing — official registry publish will fail namespace check")
    if "glama" in targets and "official_registry" not in targets:
        warnings.append("glama ingests from the official registry; enable official_registry too")
    return SyndicationPlan(
        server_json=to_server_json(manifest, base_url),
        targets=targets,
        snippets=install_snippets(manifest, base_url),
        warnings=warnings,
    )


def syndicate(manifest: dict[str, Any], base_url: str, dry_run: bool = True) -> SyndicationPlan:
    plan = plan_syndication(manifest, base_url)
    if dry_run:
        return plan
    raise NotImplementedError(
        "Live publish shells out to `mcp-publisher publish` once GitHub/DNS namespace "
        "ownership is verified; offline planning only here."
    )
