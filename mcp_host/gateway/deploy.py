"""Self-serve provider deploy — validate a submitted manifest, mount it as a declarative
(proxied) provider, and seed its entitlements. Shared by the POST /providers endpoint and by
boot-time re-loading of already-published providers from the store.

Ownership is bound here: the submitted manifest's `owner` is OVERWRITTEN with the authenticated
principal id, so a registrant can never claim another principal's namespace. Only declarative
providers (with a `backend.endpoint`) can be self-served — guest Python is never accepted over
this path, because it would run in-process next to the shared wallet.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from mcp_host.data.store import Entitlement
from mcp_host.registry.tdqs import GATE, passes
from mcp_host.sdk import manifest as manifest_mod
from mcp_host.sdk.dataset import ManagedDatasetProvider, expand_dataset_manifest
from mcp_host.sdk.errors import ErrorCode, ToolError
from mcp_host.sdk.proxy import ProxyProvider, _default_resolver

# id prefixes the platform keeps for first-party providers; self-serve owners can't take them.
RESERVED_PREFIXES = ("platform-",)


def normalize_submitted_manifest(manifest: dict[str, Any], owner_id: str) -> dict[str, Any]:
    """Return a copy with ownership bound to `owner_id` and postgres_schema derived from id.

    Binding owner here (not trusting the submitted value) is the whole integrity guarantee of
    self-serve: the principal authenticated by their API key owns exactly what they publish.
    """
    m = dict(manifest)
    m["owner"] = owner_id
    data = dict(m.get("data") or {})
    if not data.get("postgres_schema"):
        # Postgres identifiers can't contain hyphens; mirror the scaffold/deploy normalization.
        data["postgres_schema"] = (m.get("id") or "").replace("-", "_")
    m["data"] = data
    return m


def seed_entitlements(store, provider, default_plans: dict[str, dict], owner_managed: bool = False) -> None:
    """Seed plan→scope entitlements for a provider's non-admin scopes.

    :admin scopes are NEVER seeded — the gateway authorizes them by ownership. Owner-managed
    providers grant their (non-admin) scopes to every plan because the tool body enforces
    per-target ownership. Mirrors the boot-time seeding in server.build_gateway.
    """
    for scope in provider.manifest["auth"]["scopes"]:
        if scope.endswith(":admin"):
            continue
        for plan, cfg in default_plans.items():
            if scope.endswith(cfg["scopes_suffix"]) or owner_managed:
                store.set_entitlement(Entitlement(plan, provider.id, scope,
                                                  cfg["quota"], cfg["rate"]))


def manifest_kind(manifest: dict[str, Any]) -> str:
    """Which self-serve execution kind a manifest declares: 'proxy' (backend.endpoint),
    'dataset' (datasets[]), or 'unknown'."""
    if (manifest.get("backend") or {}).get("endpoint"):
        return "proxy"
    if manifest.get("datasets"):
        return "dataset"
    return "unknown"


def _gate_tdqs(provider, gate: float) -> float:
    ok, score, _ = passes(provider, gate)
    if not ok:
        raise ToolError(ErrorCode.VALIDATION_ERROR,
                        f"TDQS {score} below gate {gate} — improve tool descriptions/annotations")
    return score


def publish_provider(gw, manifest: dict[str, Any], owner_id: str, signing_key: str, *,
                     default_plans: dict[str, dict],
                     resolver: Callable[[str], list[str]] = _default_resolver,
                     gate: float = GATE,
                     reserved_ids: Iterable[str] = ()) -> dict[str, Any]:
    """Validate, mount, and entitle a self-served provider — either a declarative proxy
    (backend.endpoint) or a managed-dataset provider (datasets[]). Raise ToolError on any
    rejection (caller maps .http_status). Returns a summary dict on success.
    """
    m = normalize_submitted_manifest(manifest, owner_id)
    pid = m.get("id") or ""

    if any(pid.startswith(p) for p in RESERVED_PREFIXES):
        raise ToolError(ErrorCode.FORBIDDEN_SCOPE,
                        f"provider id prefix is reserved for first-party providers: '{pid}'")
    if gw.provider(pid) is not None or pid in set(reserved_ids):
        raise ToolError(ErrorCode.INVALID_REQUEST, f"provider '{pid}' already exists")

    kind = manifest_kind(m)
    if kind == "proxy":
        try:
            provider = ProxyProvider(m, signing_key, resolver=resolver)
        except manifest_mod.ManifestError as e:
            raise ToolError(ErrorCode.VALIDATION_ERROR, str(e))
        score = _gate_tdqs(provider, gate)
        gw.mount(provider)
        seed_entitlements(gw.store, provider, default_plans)
        return {"id": pid, "kind": "declarative-proxy", "owner": owner_id, "mounted": True,
                "endpoint": provider.endpoint, "route": gw.canonical_uri(pid),
                "scopes": list(provider.manifest["auth"]["scopes"]), "tdqs": score}

    if kind == "dataset":
        try:
            expanded = expand_dataset_manifest(m)
            provider = ManagedDatasetProvider(expanded)
        except manifest_mod.ManifestError as e:
            raise ToolError(ErrorCode.VALIDATION_ERROR, str(e))
        score = _gate_tdqs(provider, gate)
        gw.mount(provider)
        if gw.tenant is not None:
            provider.provision(gw.tenant.handle(pid))
        seed_entitlements(gw.store, provider, default_plans)
        return {"id": pid, "kind": "managed-dataset", "owner": owner_id, "mounted": True,
                "datasets": [d["name"] for d in expanded["datasets"]],
                "route": gw.canonical_uri(pid),
                "scopes": list(provider.manifest["auth"]["scopes"]), "tdqs": score}

    raise ToolError(ErrorCode.INVALID_REQUEST,
                    "self-serve providers must declare either backend.endpoint (proxy) or datasets (managed-dataset)")


# Back-compat alias: the proxy-only entry point now dispatches through publish_provider.
publish_declarative_provider = publish_provider


def load_self_serve_providers(gw, signing_key: str, code_ids: Iterable[str],
                              resolver: Callable[[str], list[str]] = _default_resolver) -> list[str]:
    """At boot, re-mount every persisted self-serve provider (proxy or managed-dataset) that isn't
    already loaded from code. Returns the ids mounted. Fails closed: a provider that no longer
    builds (e.g. an endpoint that now fails the SSRF guard) is simply not mounted."""
    code = set(code_ids)
    mounted: list[str] = []
    for row in gw.store.list_providers():
        m = row.manifest
        if row.id in code:
            continue
        kind = manifest_kind(m)
        try:
            if kind == "proxy":
                provider = ProxyProvider(m, signing_key, resolver=resolver)
            elif kind == "dataset":
                provider = ManagedDatasetProvider(m)
            else:
                continue
        except manifest_mod.ManifestError:
            continue
        gw.mount(provider)
        if kind == "dataset" and gw.tenant is not None:
            provider.provision(gw.tenant.handle(row.id))
        mounted.append(row.id)
    return mounted


# Back-compat alias.
load_declarative_providers = load_self_serve_providers
