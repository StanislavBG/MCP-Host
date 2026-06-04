"""Self-serve owner registration — the platform's front door.

Anyone can register as an owner principal and receive a one-time API key; they then publish
declarative providers under that ownership (see mcp_host/sdk/proxy.py). The principal/api_key
tables and the x-api-key auth path already exist (mcp_host/auth/principal.py); this module is
the missing surface that mints them.

The raw API key is shown to the registrant exactly once and stored only as a SHA-256 hash
(store.add_api_key). There is no recovery — losing it means re-registering.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass

OWNER_PREFIX = "usr_"
KEY_PREFIX = "mch_sk_"  # mcp-host secret key
KEY_ID_PREFIX = "key_"


@dataclass
class Registration:
    owner_id: str
    api_key: str  # raw key — returned to the caller ONCE, never persisted in the clear
    key_id: str


def gen_owner_id() -> str:
    return OWNER_PREFIX + secrets.token_hex(8)


def gen_api_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def register_owner(store, display_name: str = "") -> Registration:
    """Create a fresh owner principal + issue its first API key. Returns the raw key once.

    The key carries no scopes here: a declarative owner's authority is by OWNERSHIP (the gateway
    authorizes :admin tools against provider.owner == principal.id), not by seeded scopes.
    """
    owner_id = gen_owner_id()
    raw_key = gen_api_key()
    key_id = KEY_ID_PREFIX + secrets.token_hex(8)
    store.create_principal(owner_id, kind="user", plan="free", owner=owner_id)
    store.add_api_key(key_id, owner_id, raw_key, scopes=[])
    return Registration(owner_id=owner_id, api_key=raw_key, key_id=key_id)
