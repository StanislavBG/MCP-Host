"""Artifact store — large/vector/blob data lives here, NOT in Postgres and NOT on the
ephemeral container FS as source of truth.

Generalizes edgar-rag's /upload-vectors[-chunk]: HMAC-authenticated, chunked upload, atomic
swap. Locally the backend is a directory tree (objects/<provider>/<name>); in production the
same interface points at object storage. Providers get a read-only ArtifactView via ctx.
"""

from __future__ import annotations

import hashlib
import hmac
import shutil
from dataclasses import dataclass
from pathlib import Path


def secure_eq(a: str, b: str) -> bool:
    if not a or not b:
        return False
    return hmac.compare_digest(a, b)


def verify_upload_auth(authorization: str | None, upload_secret: str) -> bool:
    """Bearer-token check matching edgar-rag's verify_upload_token (constant-time)."""
    expected = f"Bearer {upload_secret}" if upload_secret else ""
    return secure_eq(authorization or "", expected)


@dataclass
class ArtifactView:
    """Read-only handle a provider uses to reach its uploaded artifacts."""

    root: Path
    provider_id: str

    def path(self, name: str) -> Path | None:
        p = self.root / self.provider_id / name
        return p if p.exists() else None

    def read_bytes(self, name: str) -> bytes | None:
        p = self.path(name)
        return p.read_bytes() if p else None


class ArtifactStore:
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._staging: dict[str, bytearray] = {}  # (provider/name) -> assembling chunks

    def _key(self, provider_id: str, name: str) -> str:
        return f"{provider_id}/{name}"

    def begin(self, provider_id: str, name: str) -> None:
        self._staging[self._key(provider_id, name)] = bytearray()

    def append_chunk(self, provider_id: str, name: str, data: bytes) -> None:
        key = self._key(provider_id, name)
        self._staging.setdefault(key, bytearray()).extend(data)

    def commit(self, provider_id: str, name: str) -> int:
        """Atomically swap the assembled bytes into place. Returns byte count."""
        key = self._key(provider_id, name)
        data = bytes(self._staging.pop(key, b""))
        dest_dir = self.root / provider_id
        dest_dir.mkdir(parents=True, exist_ok=True)
        tmp = dest_dir / f".{name}.tmp"
        final = dest_dir / name
        tmp.write_bytes(data)
        if final.exists():
            final.unlink()
        shutil.move(str(tmp), str(final))
        return len(data)

    def put(self, provider_id: str, name: str, data: bytes) -> int:
        """Single-shot upload (small artifacts)."""
        self.begin(provider_id, name)
        self.append_chunk(provider_id, name, data)
        return self.commit(provider_id, name)

    def view(self, provider_id: str) -> ArtifactView:
        return ArtifactView(self.root, provider_id)

    def sha256(self, provider_id: str, name: str) -> str | None:
        p = self.root / provider_id / name
        if not p.exists():
            return None
        return hashlib.sha256(p.read_bytes()).hexdigest()
