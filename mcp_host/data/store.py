"""Control-plane store (platform.* tables).

Production target is Postgres (one DB, `platform.*` + per-provider schemas, RLS). For local
dev and the test suite we use a SQLite-backed implementation with the SAME interface, so the
gateway/billing/auth code is storage-agnostic. `data/factory.make_backends()` picks the
backend from DATABASE_URL (postgres:// -> PgStore once provisioned; otherwise SQLite, file-backed
on a persistent workspace so issued API keys survive a redeploy).

The schema here mirrors plan §6. Tenant data (`<provider>.*`) lives behind TenantDB
(data/tenant.py); this module is the host-owned control plane only.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Any

ISO = "%Y-%m-%dT%H:%M:%S+00:00"


def now_iso() -> str:
    return time.strftime(ISO, time.gmtime())


def hash_key(raw: str) -> str:
    """Store only a hash of API keys, never the raw value."""
    return hashlib.sha256(raw.encode()).hexdigest()


def _ensure_parent_dir(path: str) -> None:
    """Create the parent dir for a file-backed SQLite path so a durable path on a fresh VM
    can't crash boot with 'unable to open database file'. No-op for :memory:."""
    if path and path != ":memory:":
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)


@dataclass
class ProviderRow:
    id: str
    display_name: str
    discipline: str
    version: str
    manifest: dict[str, Any]
    status: str


@dataclass
class Entitlement:
    plan: str
    provider_id: str
    scope: str
    monthly_quota: int  # -1 = unlimited
    rate_per_min: int


class SqliteStore:
    """SQLite implementation of the control plane. Thread-safe enough for the single-VM model."""

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        _ensure_parent_dir(path)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()

    def _init_schema(self) -> None:
        c = self._conn
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS providers(
                id TEXT PRIMARY KEY, display_name TEXT, discipline TEXT, version TEXT,
                owner TEXT, manifest_json TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS tools(
                provider_id TEXT NOT NULL, name TEXT NOT NULL, scope TEXT NOT NULL,
                price_usdc TEXT NOT NULL, annotations_json TEXT,
                PRIMARY KEY(provider_id, name));
            CREATE TABLE IF NOT EXISTS principals(
                id TEXT PRIMARY KEY, kind TEXT NOT NULL, owner TEXT, plan TEXT NOT NULL DEFAULT 'free',
                created_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS api_keys(
                id TEXT PRIMARY KEY, principal_id TEXT NOT NULL, key_hash TEXT NOT NULL UNIQUE,
                scopes TEXT NOT NULL, revoked_at TEXT);
            CREATE TABLE IF NOT EXISTS entitlements(
                plan TEXT NOT NULL, provider_id TEXT NOT NULL, scope TEXT NOT NULL,
                monthly_quota INTEGER NOT NULL, rate_per_min INTEGER NOT NULL,
                PRIMARY KEY(plan, provider_id, scope));
            CREATE TABLE IF NOT EXISTS usage(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, provider_id TEXT NOT NULL,
                tool TEXT NOT NULL, principal_id TEXT NOT NULL, duration_ms INTEGER NOT NULL,
                paid INTEGER NOT NULL, tx_hash TEXT, status_code INTEGER NOT NULL);
            CREATE INDEX IF NOT EXISTS idx_usage_lookup ON usage(principal_id, provider_id, ts);
            CREATE TABLE IF NOT EXISTS sessions(
                mcp_session_id TEXT PRIMARY KEY, provider_id TEXT NOT NULL, principal_id TEXT NOT NULL,
                created_at TEXT NOT NULL, expires_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS audit(
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL, method TEXT, path TEXT,
                principal_id TEXT, status_code INTEGER, duration_ms INTEGER, ip_masked TEXT);
            CREATE TABLE IF NOT EXISTS artifacts(
                provider_id TEXT NOT NULL, name TEXT NOT NULL, kind TEXT, bytes INTEGER,
                uri TEXT, uploaded_at TEXT, PRIMARY KEY(provider_id, name));
            """
        )
        c.commit()

    # ---- providers -------------------------------------------------------
    def register_provider(self, manifest: dict[str, Any]) -> None:
        import json

        c = self._conn
        c.execute(
            "INSERT OR REPLACE INTO providers(id, display_name, discipline, version, owner, manifest_json, status, created_at)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (manifest["id"], manifest["display_name"], manifest["discipline"], manifest["version"],
             manifest.get("owner"), json.dumps(manifest), "active", now_iso()),
        )
        c.execute("DELETE FROM tools WHERE provider_id=?", (manifest["id"],))
        for t in manifest["tools"]:
            c.execute(
                "INSERT INTO tools(provider_id, name, scope, price_usdc, annotations_json) VALUES(?,?,?,?,?)",
                (manifest["id"], t["name"], t["scope"], t.get("price_usdc", "0.00"),
                 json.dumps(t.get("annotations", {}))),
            )
        c.commit()

    def get_provider(self, provider_id: str) -> ProviderRow | None:
        import json

        r = self._conn.execute("SELECT * FROM providers WHERE id=?", (provider_id,)).fetchone()
        if not r:
            return None
        return ProviderRow(r["id"], r["display_name"], r["discipline"], r["version"],
                           json.loads(r["manifest_json"]), r["status"])

    def list_providers(self) -> list[ProviderRow]:
        import json

        rows = self._conn.execute("SELECT * FROM providers ORDER BY id").fetchall()
        return [ProviderRow(r["id"], r["display_name"], r["discipline"], r["version"],
                            json.loads(r["manifest_json"]), r["status"]) for r in rows]

    # ---- principals / api keys ------------------------------------------
    def create_principal(self, principal_id: str, kind: str = "user", plan: str = "free",
                         owner: str | None = None) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO principals(id, kind, owner, plan, created_at) VALUES(?,?,?,?,?)",
            (principal_id, kind, owner, plan, now_iso()),
        )
        self._conn.commit()

    def add_api_key(self, key_id: str, principal_id: str, raw_key: str, scopes: list[str]) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO api_keys(id, principal_id, key_hash, scopes, revoked_at) VALUES(?,?,?,?,NULL)",
            (key_id, principal_id, hash_key(raw_key), ",".join(scopes)),
        )
        self._conn.commit()

    def principal_for_key(self, raw_key: str) -> tuple[str, str, tuple[str, ...]] | None:
        """Return (principal_id, plan, scopes) for a valid, non-revoked API key, else None."""
        r = self._conn.execute(
            "SELECT k.principal_id, k.scopes, p.plan FROM api_keys k JOIN principals p ON p.id=k.principal_id"
            " WHERE k.key_hash=? AND k.revoked_at IS NULL",
            (hash_key(raw_key),),
        ).fetchone()
        if not r:
            return None
        scopes = tuple(s for s in r["scopes"].split(",") if s)
        return r["principal_id"], r["plan"], scopes

    # ---- entitlements ----------------------------------------------------
    def set_entitlement(self, e: Entitlement) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO entitlements(plan, provider_id, scope, monthly_quota, rate_per_min)"
            " VALUES(?,?,?,?,?)",
            (e.plan, e.provider_id, e.scope, e.monthly_quota, e.rate_per_min),
        )
        self._conn.commit()

    def get_entitlement(self, plan: str, provider_id: str, scope: str) -> Entitlement | None:
        r = self._conn.execute(
            "SELECT * FROM entitlements WHERE plan=? AND provider_id=? AND scope=?",
            (plan, provider_id, scope),
        ).fetchone()
        if not r:
            return None
        return Entitlement(r["plan"], r["provider_id"], r["scope"], r["monthly_quota"], r["rate_per_min"])

    # ---- usage / metering ------------------------------------------------
    def record_usage(self, provider_id: str, tool: str, principal_id: str, duration_ms: int,
                     paid: bool, status_code: int, tx_hash: str | None = None) -> None:
        self._conn.execute(
            "INSERT INTO usage(ts, provider_id, tool, principal_id, duration_ms, paid, tx_hash, status_code)"
            " VALUES(?,?,?,?,?,?,?,?)",
            (now_iso(), provider_id, tool, principal_id, duration_ms, 1 if paid else 0, tx_hash, status_code),
        )
        self._conn.commit()

    def usage_count_since(self, principal_id: str, provider_id: str, since_iso: str) -> int:
        r = self._conn.execute(
            "SELECT COUNT(*) n FROM usage WHERE principal_id=? AND provider_id=? AND ts>=? AND status_code<400",
            (principal_id, provider_id, since_iso),
        ).fetchone()
        return int(r["n"])

    def recent_call_count(self, principal_id: str, provider_id: str, window_secs: int) -> int:
        since = time.strftime(ISO, time.gmtime(time.time() - window_secs))
        r = self._conn.execute(
            "SELECT COUNT(*) n FROM usage WHERE principal_id=? AND provider_id=? AND ts>=?",
            (principal_id, provider_id, since),
        ).fetchone()
        return int(r["n"])

    def usage_summary(self) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT provider_id, tool, COUNT(*) calls, SUM(paid) paid_calls, AVG(duration_ms) avg_ms"
            " FROM usage GROUP BY provider_id, tool ORDER BY calls DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- sessions --------------------------------------------------------
    def create_session(self, session_id: str, provider_id: str, principal_id: str, ttl_secs: int = 3600) -> None:
        exp = time.strftime(ISO, time.gmtime(time.time() + ttl_secs))
        self._conn.execute(
            "INSERT OR REPLACE INTO sessions(mcp_session_id, provider_id, principal_id, created_at, expires_at)"
            " VALUES(?,?,?,?,?)",
            (session_id, provider_id, principal_id, now_iso(), exp),
        )
        self._conn.commit()

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        r = self._conn.execute("SELECT * FROM sessions WHERE mcp_session_id=?", (session_id,)).fetchone()
        if not r:
            return None
        if r["expires_at"] < now_iso():
            return None
        return dict(r)

    # ---- audit / artifacts ----------------------------------------------
    def audit(self, method: str, path: str, principal_id: str | None, status_code: int,
              duration_ms: int, ip_masked: str) -> None:
        self._conn.execute(
            "INSERT INTO audit(ts, method, path, principal_id, status_code, duration_ms, ip_masked)"
            " VALUES(?,?,?,?,?,?,?)",
            (now_iso(), method, path, principal_id, status_code, duration_ms, ip_masked),
        )
        self._conn.commit()

    def record_artifact(self, provider_id: str, name: str, kind: str, nbytes: int, uri: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO artifacts(provider_id, name, kind, bytes, uri, uploaded_at) VALUES(?,?,?,?,?,?)",
            (provider_id, name, kind, nbytes, uri, now_iso()),
        )
        self._conn.commit()

    def get_artifact(self, provider_id: str, name: str) -> dict[str, Any] | None:
        r = self._conn.execute(
            "SELECT * FROM artifacts WHERE provider_id=? AND name=?", (provider_id, name)
        ).fetchone()
        return dict(r) if r else None

    def list_artifacts(self, provider_id: str) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM artifacts WHERE provider_id=? ORDER BY name", (provider_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_artifact(self, provider_id: str, name: str) -> None:
        self._conn.execute("DELETE FROM artifacts WHERE provider_id=? AND name=?", (provider_id, name))
        self._conn.commit()
