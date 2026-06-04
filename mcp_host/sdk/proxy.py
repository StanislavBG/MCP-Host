"""Declarative (backend-proxied) provider — the self-serve third-party execution model.

A third party registers as an owner, then publishes a `provider.json` whose `backend` declares
an external HTTPS endpoint. The host runs NO guest Python: `ProxyProvider` is host-owned code
that forwards each `tools/call` to the owner's endpoint over HTTPS, HMAC-signed so the owner can
trust the caller is the host. Auth, billing, metering, audit, and RLS provisioning still run
host-side on the normal `tools/call` hot path — only the tool *body* lives off-platform.

This is what makes open self-serve safe on the single shared process: a stranger's code never
executes next to the shared wallet key or another tenant's schema.

Security:
- SSRF guard (`validate_external_endpoint`): https only; reject endpoints that resolve to a
  private/loopback/link-local/reserved address. Re-checked at *call* time too, to defeat DNS
  rebinding (a host that resolved public at deploy and flips to 169.254.169.254 at call time).
- Fail-closed: any transport error / timeout / non-2xx / oversized or non-JSON body → a
  BACKEND_UNAVAILABLE ToolError (502). The provider never returns partial/garbage.
- Never log the request/response body (50-char preview max upstream); we log nothing here.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable

from mcp_host.sdk import manifest as manifest_mod
from mcp_host.sdk.context import ToolContext
from mcp_host.sdk.errors import ErrorCode, ToolError
from mcp_host.sdk.provider import Provider

# Hostnames that name a local/metadata endpoint without being IP literals.
_BLOCKED_HOSTS = {
    "localhost", "ip6-localhost", "ip6-loopback",
    "metadata", "metadata.google.internal",
}
_MAX_RESPONSE_BYTES = 256 * 1024  # an MCP tool result envelope, not a data dump

# A transport returns (status_code, body_bytes). Injectable so tests never touch the network.
Transport = Callable[[str, bytes, dict[str, str], float], "tuple[int, bytes]"]
# A resolver maps host -> list of IP strings. Defaults to real DNS; injectable in tests.
Resolver = Callable[[str], "list[str]"]


def _default_resolver(host: str) -> list[str]:
    # O(#A/AAAA records); one syscall. Returns every address the host resolves to.
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [info[4][0] for info in infos]


def _addr_blocked(addr: ipaddress._BaseAddress) -> bool:
    return (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_multicast or addr.is_reserved or addr.is_unspecified)


def _as_ip(host: str) -> "ipaddress._BaseAddress | None":
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None  # not an IP literal — it's a hostname to resolve


def validate_external_endpoint(url: str, resolver: Resolver = _default_resolver) -> None:
    """Raise ManifestError unless `url` is a safe, public HTTPS endpoint (SSRF guard).

    Complexity: O(#resolved addresses), bounded by DNS; a handful in practice.

    NOTE: ManifestError subclasses ValueError, so we must NEVER raise it inside a `try` whose
    `except` catches ValueError — that would silently swallow the block. Hence the explicit
    _as_ip() helper instead of try/except around the raise.
    """
    if not url:
        raise manifest_mod.ManifestError("backend.endpoint is required for a declarative provider")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https":
        raise manifest_mod.ManifestError(
            f"backend.endpoint must be https, got '{parts.scheme or '(none)'}'")
    host = (parts.hostname or "").lower()
    if not host or host in _BLOCKED_HOSTS:
        raise manifest_mod.ManifestError(f"backend.endpoint host '{host}' is not allowed")

    literal = _as_ip(host)
    if literal is not None:
        if _addr_blocked(literal):
            raise manifest_mod.ManifestError(
                "backend.endpoint points at a private/loopback/link-local address")
        return  # public IP literal — nothing to resolve

    try:
        addresses = resolver(host)
    except (OSError, socket.gaierror) as e:
        raise manifest_mod.ManifestError(f"backend.endpoint host '{host}' does not resolve: {e}")
    if not addresses:
        raise manifest_mod.ManifestError(f"backend.endpoint host '{host}' does not resolve")
    for ip in addresses:
        addr = _as_ip(ip)
        if addr is None or _addr_blocked(addr):
            raise manifest_mod.ManifestError(
                f"backend.endpoint host '{host}' resolves to blocked address {ip}")


def _urllib_transport(url: str, body: bytes, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 (https, SSRF-guarded)
            return resp.status, resp.read(_MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as e:
        return e.code, e.read(_MAX_RESPONSE_BYTES + 1)


class ProxyProvider(Provider):
    """Host-owned provider that forwards tools/call to an owner's external HTTPS endpoint.

    Unlike a code provider it has no @tool methods, so it bypasses Provider's __init__
    reconciliation and drives dispatch straight from the manifest.
    """

    def __init__(self, manifest: dict[str, Any], signing_key: str, *,
                 transport: Transport | None = None,
                 resolver: Resolver = _default_resolver,
                 timeout: float = 10.0) -> None:
        manifest_mod.validate_manifest(manifest)
        backend = manifest.get("backend") or {}
        self.endpoint: str = backend.get("endpoint", "")
        validate_external_endpoint(self.endpoint, resolver=resolver)
        self.manifest = manifest
        self.id = manifest["id"]
        self._signing_key = signing_key
        self._resolver = resolver
        self._timeout = timeout
        self._transport: Transport = transport or _urllib_transport
        # name -> manifest tool entry (no Pydantic models; declarative input schemas only)
        self._tools = {t["name"]: t for t in manifest["tools"]}

    # ---- overrides so the base Provider surface works without @tool methods ----
    def _input_schema(self, tool_name: str) -> dict[str, Any]:
        t = self._tools.get(tool_name, {})
        return t.get("input_schema") or {"type": "object", "properties": {}}

    def call_tool(self, ctx: ToolContext, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool_name not in self._tools:
            raise ToolError(ErrorCode.TOOL_NOT_FOUND, f"Unknown tool '{tool_name}'")
        # Re-validate at call time: the endpoint passed deploy-time checks, but DNS can change.
        try:
            validate_external_endpoint(self.endpoint, resolver=self._resolver)
        except manifest_mod.ManifestError as e:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, f"Backend endpoint rejected: {e}")

        body = json.dumps({
            "tool": tool_name,
            "arguments": arguments or {},
            "provider": self.id,
            "principal": {"id": ctx.principal.id, "plan": ctx.principal.plan,
                          "scopes": list(ctx.principal.scopes)},
        }, separators=(",", ":")).encode()
        ts = str(int(time.time()))
        # Owner verifies: HMAC(signing_key, "<ts>." + body). Bind the timestamp into the MAC so a
        # captured signature can't be replayed with a fresh timestamp.
        sig = hmac.new(self._signing_key.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
        headers = {
            "Content-Type": "application/json",
            "X-MCP-Host-Timestamp": ts,
            "X-MCP-Host-Signature": sig,
            "X-MCP-Host-Provider": self.id,
        }
        try:
            status, raw = self._transport(self.endpoint, body, headers, self._timeout)
        except (urllib.error.URLError, OSError, TimeoutError) as e:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                            f"Provider backend unreachable: {type(e).__name__}")
        if status < 200 or status >= 300:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                            f"Provider backend returned HTTP {status}")
        if len(raw) > _MAX_RESPONSE_BYTES:
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, "Provider backend response too large")
        try:
            result = json.loads(raw)
        except (ValueError, json.JSONDecodeError):
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE, "Provider backend returned non-JSON")
        if not isinstance(result, dict):
            raise ToolError(ErrorCode.BACKEND_UNAVAILABLE,
                            "Provider backend result must be a JSON object")
        return result
