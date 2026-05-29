"""Generate the official-registry server.json from our provider.json.

provider.json is the single source of truth; this derives the spec-compliant server.json the
`mcp-publisher` CLI publishes to registry.modelcontextprotocol.io. Downstream registries
(Glama, mcp.so, PulseMCP) auto-ingest from there, so we publish once.
"""

from __future__ import annotations

from typing import Any

SCHEMA_URL = "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json"


def to_server_json(manifest: dict[str, Any], base_url: str) -> dict[str, Any]:
    ns = manifest.get("owner_namespace", "io.github.unknown")
    name = f"{ns}/{manifest['id']}"
    canonical = f"{base_url}/mcp/{manifest['id']}"
    return {
        "$schema": SCHEMA_URL,
        "name": name,
        "description": manifest["summary"],
        "version": manifest["version"],
        "repository": {"url": manifest.get("homepage", canonical), "source": "github"},
        "remotes": [{"type": "streamable-http", "url": canonical}],
        "_meta": {
            "io.mcp-host/discipline": manifest["discipline"],
            "io.mcp-host/tools": [t["name"] for t in manifest["tools"]],
        },
    }


def install_snippets(manifest: dict[str, Any], base_url: str) -> dict[str, str]:
    """Ready-to-paste client config for one-click install (Claude/Cursor/VS Code)."""
    canonical = f"{base_url}/mcp/{manifest['id']}"
    pid = manifest["id"]
    claude = f'{{"mcpServers": {{"{pid}": {{"url": "{canonical}"}}}}}}'
    cursor = claude  # cursor uses ~/.cursor/mcp.json with the same shape
    vscode = f'{{"servers": {{"{pid}": {{"url": "{canonical}"}}}}}}'
    return {"claude": claude, "cursor": cursor, "vscode": vscode}
