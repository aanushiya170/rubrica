"""API-key authentication for the extensions layer.

Falls back to the core session (cookie / bearer session token) so the same
endpoints work from a browser. Keys are stored hashed; the plaintext is shown
once at creation.
"""
from __future__ import annotations

import hashlib

from fastapi import Request

from ..core import auth
from ..core.db import Database
from ..core.errors import Forbidden
from ..core.http import current_actor, get_db
from ..core.util import iso, new_id, token

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
  id         TEXT PRIMARY KEY,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  key_hash   TEXT NOT NULL UNIQUE,
  prefix     TEXT NOT NULL,
  label      TEXT NOT NULL DEFAULT '',
  scopes     TEXT NOT NULL DEFAULT 'read',
  created_at TEXT NOT NULL,
  last_used  TEXT,
  revoked    INTEGER NOT NULL DEFAULT 0
);
"""


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_key(db: Database, user_id: str, label: str = "", scopes: str = "read") -> dict:
    raw = "vk_" + token(24)
    kid = new_id("key")
    db.run("INSERT INTO api_keys(id, user_id, key_hash, prefix, label, scopes, created_at) VALUES (?,?,?,?,?,?,?)",
           (kid, user_id, _hash(raw), raw[:10], label, scopes, iso()))
    return {"id": kid, "key": raw, "prefix": raw[:10], "label": label, "scopes": scopes}


def list_keys(db: Database, user_id: str) -> list[dict]:
    return [dict(r) for r in db.all("SELECT id, prefix, label, scopes, created_at, last_used, revoked FROM api_keys"
                                    " WHERE user_id = ? ORDER BY created_at DESC", (user_id,))]


def revoke_key(db: Database, user_id: str, key_id: str) -> None:
    db.run("UPDATE api_keys SET revoked = 1 WHERE id = ? AND user_id = ?", (key_id, user_id))


def api_actor(request: Request) -> auth.Actor | None:
    """Resolve actor from ``Authorization: Bearer vk_...`` (API key) or the core session."""
    if "api_actor" in request.scope:
        return request.scope["api_actor"]
    db = get_db(request)
    actor = None
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer vk_"):
        raw = authz[7:].strip()
        r = db.one("SELECT k.id, k.user_id, k.scopes FROM api_keys k WHERE k.key_hash = ? AND k.revoked = 0", (_hash(raw),))
        if r:
            actor = auth.get_user(db, r["user_id"])
            db.run("UPDATE api_keys SET last_used = ? WHERE id = ?", (iso(), r["id"]))
            request.scope["api_scopes"] = r["scopes"]
    if actor is None:
        actor = current_actor(request)
        request.scope["api_scopes"] = "read write"
    request.scope["api_actor"] = actor
    return actor


def require_write(request: Request) -> None:
    api_actor(request)  # resolves the key (and its scopes) if not done yet
    if "write" not in request.scope.get("api_scopes", "read write"):
        raise Forbidden("this API key is read-only")
