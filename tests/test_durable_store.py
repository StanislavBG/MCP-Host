"""Durable control-plane storage: API keys issued by /register must survive a restart.

Regression coverage for external-requests/001 — the in-memory fallback wiped every issued
key on redeploy, 401-ing unattended customer crons.
"""
from __future__ import annotations

import os

from mcp_host.auth.registration import register_owner
from mcp_host.data.factory import make_backends, resolve_sqlite_path
from mcp_host.data.store import SqliteStore


def test_resolve_path_prefers_workspace(monkeypatch):
    monkeypatch.delenv("MCP_HOST_DB", raising=False)
    monkeypatch.setenv("REPLIT_DEPLOYMENT", "1")
    path, durable = resolve_sqlite_path("MCP_HOST_DB", "mcp-host.db")
    assert durable and path != ":memory:" and path.endswith("mcp-host.db")


def test_explicit_memory_is_not_durable(monkeypatch):
    monkeypatch.setenv("MCP_HOST_DB", ":memory:")
    _, durable = resolve_sqlite_path("MCP_HOST_DB", "mcp-host.db")
    assert durable is False


def test_api_key_survives_reopen(tmp_path):
    """A key issued into a file-backed store still authenticates after the process 'restarts'
    (a fresh SqliteStore on the same file) — the core durability guarantee."""
    db = str(tmp_path / "mcp-host.db")
    store = SqliteStore(db)
    reg = register_owner(store, display_name="cron-owner")
    assert store.principal_for_key(reg.api_key)[0] == reg.owner_id

    reopened = SqliteStore(db)  # simulate redeploy / restart
    got = reopened.principal_for_key(reg.api_key)
    assert got is not None and got[0] == reg.owner_id


def test_make_backends_uses_file_when_configured(monkeypatch, tmp_path):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("MCP_HOST_DB", str(tmp_path / "cp.db"))
    monkeypatch.setenv("MCP_HOST_TENANT_DB", str(tmp_path / "tn.db"))
    store, _ = make_backends()
    assert store.backend == "sqlite-file"


def test_durable_path_with_missing_parent_dir_does_not_crash(tmp_path):
    """A durable path whose parent dir does not yet exist (fresh VM) must not crash boot —
    SqliteStore creates the parent rather than raising 'unable to open database file'."""
    db = str(tmp_path / "does" / "not" / "exist" / "mcp-host.db")
    store = SqliteStore(db)
    reg = register_owner(store)
    assert store.principal_for_key(reg.api_key)[0] == reg.owner_id
