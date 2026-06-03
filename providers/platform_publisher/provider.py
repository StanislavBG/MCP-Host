"""Publisher — owner-only data publishing for hosted MCPs (the 2nd first-party platform MCP).

Provider devs declare their artifacts in provider.json (`data.artifacts`); this MCP lets the
*owner* of a provider manage the bytes behind those declarations. The bytes themselves go over
the authenticated chunked HTTP endpoint (`POST /mcp/<id>/upload/<artifact>`) — JSON-RPC is the
wrong place for large vector blobs — while this MCP handles discovery, finalize and delete.

Authorization is per TARGET provider, checked in-body: the caller must be that provider's
declared `owner` (or the platform super-admin). This is distinct from the gateway's :admin gate
(which is per-called-provider) because the publisher operates ACROSS providers. A non-owner gets
FORBIDDEN_SCOPE even though they can reach the tool.

Like platform-health, the server injects a live host view via `bind_host()` at boot; third-party
providers never receive this.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from mcp_host.sdk import ErrorCode, Provider, ToolError, tool


class _ProviderRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(min_length=1, max_length=40)


class _ArtifactRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider_id: str = Field(min_length=1, max_length=40)
    name: str = Field(min_length=1, max_length=40)


class PlatformPublisherProvider(Provider):
    manifest_path = "provider.json"

    def bind_host(self, gw, meta: dict) -> None:
        self._gw = gw
        self._meta = dict(meta)

    # ---- ownership + artifact-declaration guards -------------------------
    def _target(self, ctx, provider_id: str):
        """Resolve a target provider and assert the caller owns it (or is super-admin)."""
        gw = getattr(self, "_gw", None)
        target = gw.provider(provider_id) if gw else None
        if not target:
            raise ToolError(ErrorCode.PROVIDER_NOT_FOUND, f"No provider '{provider_id}'")
        owner = target.manifest.get("owner")
        super_admin = self._meta.get("platform_owner")
        if not (owner and ctx.principal.id == owner) and ctx.principal.id != super_admin:
            raise ToolError(ErrorCode.FORBIDDEN_SCOPE,
                            f"You do not own '{provider_id}' — publishing is owner-only")
        return target

    @staticmethod
    def _declared_artifacts(target) -> dict[str, dict]:
        return {a["name"]: a for a in target.manifest.get("data", {}).get("artifacts", [])}

    def _declared(self, target, name: str) -> dict:
        decl = self._declared_artifacts(target).get(name)
        if not decl:
            raise ToolError(ErrorCode.VALIDATION_ERROR,
                            f"Artifact '{name}' is not declared in {target.id}'s provider.json", field="name")
        return decl

    # ---- tools -----------------------------------------------------------
    @tool("list_datasets", input_model=_ProviderRef)
    def list_datasets(self, ctx, provider_id: str):
        target = self._target(ctx, provider_id)
        store = self._gw.store
        artifacts = self._meta.get("artifacts")
        out = []
        for name, decl in self._declared_artifacts(target).items():
            meta = store.get_artifact(provider_id, name)
            live_bytes = artifacts.size(provider_id, name) if artifacts else None
            out.append({
                "name": name,
                "kind": decl.get("kind"),
                "max_gb": decl.get("max_gb"),
                "status": "ready" if (meta or live_bytes) else "pending",
                "bytes": (meta or {}).get("bytes") if meta else live_bytes,
                "uploaded_at": (meta or {}).get("uploaded_at"),
            })
        return ctx.json_text({"provider": provider_id, "datasets": out, "count": len(out)})

    @tool("get_upload_target", input_model=_ArtifactRef)
    def get_upload_target(self, ctx, provider_id: str, name: str):
        target = self._target(ctx, provider_id)
        decl = self._declared(target, name)
        base = self._meta.get("base_url", "").rstrip("/")
        return ctx.json_text({
            "provider": provider_id,
            "artifact": name,
            "kind": decl.get("kind"),
            "method": "POST",
            "url": f"{base}/mcp/{provider_id}/upload/{name}",
            "auth": "Bearer <token resource-bound to this provider, sub == owner> OR the platform UPLOAD_SECRET",
            "note": "Push the raw bytes to this URL, then call finalize_upload to record size + sha256.",
        })

    @tool("finalize_upload", input_model=_ArtifactRef)
    def finalize_upload(self, ctx, provider_id: str, name: str):
        target = self._target(ctx, provider_id)
        decl = self._declared(target, name)
        artifacts = self._meta.get("artifacts")
        nbytes = artifacts.size(provider_id, name) if artifacts else None
        if not nbytes:
            raise ToolError(ErrorCode.TOOL_NOT_FOUND,
                            f"No uploaded bytes for {provider_id}/{name}; upload first via get_upload_target")
        sha = artifacts.sha256(provider_id, name)
        uri = f"{self._meta.get('artifact_root', '')}/{provider_id}/{name}"
        self._gw.store.record_artifact(provider_id, name, decl.get("kind", "blob"), nbytes, uri)
        return ctx.json_text({"provider": provider_id, "artifact": name,
                              "status": "ready", "bytes": nbytes, "sha256": sha})

    @tool("delete_dataset", input_model=_ArtifactRef)
    def delete_dataset(self, ctx, provider_id: str, name: str):
        self._target(ctx, provider_id)
        artifacts = self._meta.get("artifacts")
        removed = artifacts.delete(provider_id, name) if artifacts else False
        self._gw.store.delete_artifact(provider_id, name)
        return ctx.json_text({"provider": provider_id, "artifact": name, "deleted": removed})

    # ---- provider hooks --------------------------------------------------
    def health(self, ctx):
        return {"status": "ok", "provider": self.id}

    def catalog(self, ctx):
        return {"provider": self.id, "describes": "owner-only artifact publishing for hosted MCPs",
                "tools": [t["name"] for t in self.manifest["tools"]]}
