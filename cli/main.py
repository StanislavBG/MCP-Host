"""`mcp-host` CLI — the provider lifecycle commands an onboarding dev-agent runs.

    mcp-host scaffold <id>            generate a provider skeleton (provider.json + provider.py)
    mcp-host validate <path>         schema-check provider.json + TDQS quality gate
    mcp-host tdqs <path>             show the per-tool quality breakdown
    mcp-host syndicate <path>        emit server.json + registry targets + install snippets (dry-run)
    mcp-host token --provider <id>   mint a resource-bound owner bearer (self-host/dev issuer)
    mcp-host ingest <id> <ds> <file> push owner data rows into a hosted provider (cron seam)

`deploy` and `upload` target the live host (gateway mount + artifact upload API) and are run
against a running MCP-Host; they are documented in ONBOARDING.md. `token`/`ingest` are the
owner-side seam for keeping a hosted provider's data fresh (enhancement 001): `ingest` POSTs a
`tools/call` for an owner-only `:admin` ingest tool, authenticated by the resource-bound bearer.
The validate/tdqs/syndicate commands are the offline, pre-deploy steps a provider repo passes in CI.
"""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
import os
import sys
import urllib.request
from pathlib import Path
from typing import Any

from mcp_host.auth.principal import mint_token
from mcp_host.registry.syndicate import plan_syndication
from mcp_host.registry.tdqs import GATE, score_provider
from mcp_host.sdk import Provider
from mcp_host.sdk.manifest import ManifestError, load_manifest, validate_manifest

BASE_URL = "https://mcp-host"
INGEST_TOOL = "signals.ingest"

SCAFFOLD_MANIFEST = {
    "$schema": "https://mcp-host/schemas/provider.schema.json",
    "id": "PROVIDER_ID",
    "display_name": "Display Name",
    "discipline": "your-discipline",
    "version": "0.1.0",
    "summary": "One sentence describing what this MCP provides (>= 40 chars recommended).",
    "owner_namespace": "io.github.YOURNAME",
    "transport": "streamable-http",
    "auth": {"modes": ["oauth2.1", "api_key"], "scopes": ["PROVIDER_ID:read"]},
    "data": {"postgres_schema": "PROVIDER_ID"},
    "tools": [
        {"name": "example_tool", "scope": "PROVIDER_ID:read", "price_usdc": "0.00",
         "description": "Describe what this tool does and any usage constraints in at least forty characters.",
         "annotations": {"readOnlyHint": True}}
    ],
    "limits": {"rate_per_min": 60, "max_request_kb": 50},
    "syndication": {"official_registry": True, "glama": True, "mcp_so": True, "pulsemcp": True},
    "health": "/mcp/PROVIDER_ID/health",
}

SCAFFOLD_PY = '''"""{id} provider — implements the MCP-Host Provider Protocol."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mcp_host.sdk import Provider, tool


class ExampleInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    arg: str = Field(min_length=1, max_length=200)


class {cls}(Provider):
    manifest_path = "provider.json"

    @tool("example_tool", input_model=ExampleInput)
    def example_tool(self, ctx, arg: str):
        # ctx.principal / ctx.tenant_db / ctx.artifacts / ctx.secret(...) available here.
        return ctx.json_text({{"echo": arg}})
'''


def _load_provider_from_path(manifest_path: Path) -> Provider | None:
    """Import a sibling provider.py and instantiate the Provider subclass, if present."""
    py = manifest_path.parent / "provider.py"
    if not py.exists():
        return None
    spec = importlib.util.spec_from_file_location(f"_prov_{manifest_path.parent.name}", py)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # so inspect.getfile() can resolve the class's source file
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    for _, obj in inspect.getmembers(mod, inspect.isclass):
        if issubclass(obj, Provider) and obj is not Provider:
            return obj()
    return None


def cmd_scaffold(args) -> int:
    pid = args.id
    cls = "".join(p.capitalize() for p in pid.replace("-", "_").split("_")) + "Provider"
    out = Path(args.dir or pid.replace("-", "_"))
    out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(json.dumps(SCAFFOLD_MANIFEST).replace("PROVIDER_ID", pid))
    # Postgres schema identifiers can't contain hyphens; normalize from the (hyphen-allowed) id.
    manifest["data"]["postgres_schema"] = pid.replace("-", "_")
    (out / "provider.json").write_text(json.dumps(manifest, indent=2) + "\n")
    (out / "provider.py").write_text(SCAFFOLD_PY.format(id=pid, cls=cls))
    (out / "__init__.py").write_text("")
    print(f"scaffolded {pid} in {out}/  (edit provider.json + provider.py, then `mcp-host validate {out}`)")
    return 0


def validate_path(manifest_path: Path) -> dict[str, Any]:
    """Returns a report dict; raises ManifestError on schema failure."""
    manifest = load_manifest(manifest_path)
    report: dict[str, Any] = {"id": manifest["id"], "schema": "ok"}
    provider = _load_provider_from_path(manifest_path)
    if provider is not None:
        score, scores = score_provider(provider)
        report["tdqs"] = score
        report["tdqs_pass"] = score >= GATE
        report["tool_scores"] = [{"name": s.name, "score": s.score, "reasons": s.reasons} for s in scores]
        report["reconciled"] = "ok"  # instantiation already reconciled @tool vs manifest
    else:
        report["tdqs"] = None
        report["note"] = "no provider.py found; schema validated only"
    return report


def cmd_validate(args) -> int:
    path = Path(args.path)
    mp = path / "provider.json" if path.is_dir() else path
    try:
        report = validate_path(mp)
    except ManifestError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(json.dumps(report, indent=2))
    if report.get("tdqs_pass") is False:
        print(f"TDQS {report['tdqs']} below gate {GATE} — not deployable", file=sys.stderr)
        return 1
    return 0


def cmd_tdqs(args) -> int:
    path = Path(args.path)
    mp = path / "provider.json" if path.is_dir() else path
    provider = _load_provider_from_path(mp)
    if provider is None:
        print("no provider.py to score", file=sys.stderr)
        return 1
    score, scores = score_provider(provider)
    print(f"server TDQS: {score} (gate {GATE})")
    for s in scores:
        flag = "ok" if s.score >= GATE else "LOW"
        print(f"  [{flag}] {s.name}: {s.score}  {('— ' + '; '.join(s.reasons)) if s.reasons else ''}")
    return 0 if score >= GATE else 1


def cmd_syndicate(args) -> int:
    path = Path(args.path)
    mp = path / "provider.json" if path.is_dir() else path
    manifest = load_manifest(mp)
    plan = plan_syndication(manifest, args.base_url)
    print(json.dumps({
        "targets": plan.targets,
        "warnings": plan.warnings,
        "server_json": plan.server_json,
        "install_snippets": plan.snippets,
    }, indent=2))
    return 0


def _canonical_uri(base_url: str, provider: str) -> str:
    return f"{base_url.rstrip('/')}/mcp/{provider}"


def cmd_token(args) -> int:
    """Mint a bearer resource-bound to <base-url>/mcp/<provider>. Self-host/dev issuer; in
    production the OAuth 2.1 AS issues this with identical gateway-side checks."""
    key = args.signing_key or os.environ.get("MCP_HOST_SIGNING_KEY", "dev-signing-key")
    token = mint_token(key, args.sub, "pro", list(args.scopes or []),
                       _canonical_uri(args.base_url, args.provider), ttl_secs=args.ttl)
    print(token)
    return 0


def _load_rows(path: Path) -> list[dict]:
    """Read a rows file: a JSON array, or an object {"rows": [...]}. Raises ValueError otherwise."""
    doc = json.loads(path.read_text())
    rows = doc.get("rows") if isinstance(doc, dict) else doc
    if not isinstance(rows, list):
        raise ValueError("rows file must be a JSON array or an object with a 'rows' array")
    return rows


def build_ingest_request(provider: str, dataset: str, rows: list[dict], mode: str,
                         base_url: str, token: str) -> tuple[str, dict[str, str], dict[str, Any]]:
    """Pure: shape the JSON-RPC tools/call request for an owner ingest. Unit-testable, no network."""
    url = _canonical_uri(base_url, provider)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"id": 1, "method": "tools/call",
            "params": {"name": INGEST_TOOL,
                       "arguments": {"dataset": dataset, "mode": mode, "rows": rows}}}
    return url, headers, body


def cmd_ingest(args) -> int:
    try:
        rows = _load_rows(Path(args.file))
    except (OSError, ValueError, json.JSONDecodeError) as e:
        print(f"cannot read rows file: {e}", file=sys.stderr)
        return 1
    token = args.token
    if not token:
        if not args.sub:
            print("provide --token, or --sub (+ optional --signing-key) to mint one", file=sys.stderr)
            return 1
        key = args.signing_key or os.environ.get("MCP_HOST_SIGNING_KEY", "dev-signing-key")
        token = mint_token(key, args.sub, "pro", ["trader:admin"],
                           _canonical_uri(args.base_url, args.provider), ttl_secs=args.ttl)
    url, headers, body = build_ingest_request(args.provider, args.dataset, rows, args.mode,
                                              args.base_url, token)
    if args.dry_run:
        preview = {"method": "POST", "url": url,
                   "headers": {**headers, "Authorization": "Bearer <redacted>"}, "body": body}
        print(json.dumps(preview, indent=2))
        return 0
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:  # noqa: S310 (host URL, owner-authenticated)
            print(resp.read().decode())
    except urllib.error.HTTPError as e:  # surface the host's error envelope
        print(e.read().decode(), file=sys.stderr)
        return 1
    except urllib.error.URLError as e:
        print(f"request failed: {e}", file=sys.stderr)
        return 1
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="mcp-host", description="MCP-Host provider lifecycle CLI")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scaffold", help="generate a provider skeleton")
    s.add_argument("id")
    s.add_argument("--dir")
    s.set_defaults(func=cmd_scaffold)

    v = sub.add_parser("validate", help="schema + TDQS gate")
    v.add_argument("path")
    v.set_defaults(func=cmd_validate)

    t = sub.add_parser("tdqs", help="tool-quality breakdown")
    t.add_argument("path")
    t.set_defaults(func=cmd_tdqs)

    y = sub.add_parser("syndicate", help="emit server.json + targets + snippets (dry-run)")
    y.add_argument("path")
    y.add_argument("--base-url", default=BASE_URL)
    y.set_defaults(func=cmd_syndicate)

    tk = sub.add_parser("token", help="mint a resource-bound owner bearer (self-host/dev issuer)")
    tk.add_argument("--provider", required=True)
    tk.add_argument("--sub", required=True, help="principal id (the provider owner)")
    tk.add_argument("--scopes", nargs="*", default=[])
    tk.add_argument("--base-url", default=BASE_URL)
    tk.add_argument("--ttl", type=int, default=3600)
    tk.add_argument("--signing-key", default=None, help="defaults to $MCP_HOST_SIGNING_KEY")
    tk.set_defaults(func=cmd_token)

    ig = sub.add_parser("ingest", help="push owner data rows into a hosted provider")
    ig.add_argument("provider")
    ig.add_argument("dataset")
    ig.add_argument("file", help="JSON array of rows, or an object with a 'rows' array")
    ig.add_argument("--mode", choices=["replace", "append"], default="replace")
    ig.add_argument("--base-url", default=BASE_URL)
    ig.add_argument("--token", default=None, help="owner bearer; else mint from --sub")
    ig.add_argument("--sub", default=None, help="owner principal id (to mint a token)")
    ig.add_argument("--ttl", type=int, default=3600)
    ig.add_argument("--signing-key", default=None, help="defaults to $MCP_HOST_SIGNING_KEY")
    ig.add_argument("--dry-run", action="store_true", help="print the request instead of sending")
    ig.set_defaults(func=cmd_ingest)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
