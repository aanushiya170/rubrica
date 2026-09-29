"""Authentication and authorisation.

* Passwords: scrypt (stdlib), salted, constant-time compare.
* Sessions: random opaque token in an HttpOnly cookie ``session``. Also accepted
  as ``Authorization: Bearer <token>`` so curl works without cookie jars.
* Identity always comes from the session, never from a URL parameter. The
  scope helpers below are the *only* place role logic lives.

Roles: visitor (no session) < participant < judge < organizer < admin.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
from dataclasses import dataclass

from .db import Database
from .errors import Forbidden, Unauthorized
from .util import iso, new_id, token

ROLES = ("participant", "judge", "organizer", "admin")
STAFF = {"organizer", "admin"}


@dataclass(frozen=True)
class Actor:
    id: str
    email: str
    name: str
    role: str

    @property
    def is_staff(self) -> bool:
        return self.role in STAFF

    @property
    def is_judge(self) -> bool:
        return self.role == "judge"


# --- passwords -----------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1)
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, salt_hex, digest_hex = stored.split("$")
        if algo != "scrypt":
            return False
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


# --- users ---------------------------------------------------------------

def create_user(db: Database, email: str, name: str, password: str, role: str = "participant",
                user_id: str | None = None) -> Actor:
    if role not in ROLES:
        raise ValueError(f"unknown role {role}")
    uid = user_id or new_id("usr")
    with db.tx() as c:
        c.execute(
            "INSERT INTO users(id, email, name, role, password_hash, created_at) VALUES (?,?,?,?,?,?)",
            (uid, email.strip().lower(), name.strip(), role, hash_password(password), iso()),
        )
    return Actor(uid, email.strip().lower(), name.strip(), role)


def get_user(db: Database, user_id: str) -> Actor | None:
    r = db.one("SELECT id, email, name, role FROM users WHERE id = ?", (user_id,))
    return Actor(r["id"], r["email"], r["name"], r["role"]) if r else None


def get_user_by_email(db: Database, email: str) -> Actor | None:
    r = db.one("SELECT id, email, name, role FROM users WHERE email = ?", (email.strip().lower(),))
    return Actor(r["id"], r["email"], r["name"], r["role"]) if r else None


def authenticate(db: Database, email: str, password: str) -> Actor | None:
    r = db.one("SELECT id, email, name, role, password_hash FROM users WHERE email = ?",
               (email.strip().lower(),))
    if not r or not verify_password(password, r["password_hash"]):
        return None
    return Actor(r["id"], r["email"], r["name"], r["role"])


# --- sessions ------------------------------------------------------------

def create_session(db: Database, user_id: str, fixed_token: str | None = None) -> str:
    tok = fixed_token or token()
    with db.tx() as c:
        c.execute("INSERT OR REPLACE INTO sessions(token, user_id, created_at) VALUES (?,?,?)",
                  (tok, user_id, iso()))
    return tok


def resolve_session(db: Database, tok: str | None) -> Actor | None:
    if not tok:
        return None
    r = db.one(
        "SELECT u.id, u.email, u.name, u.role FROM sessions s JOIN users u ON u.id = s.user_id"
        " WHERE s.token = ?", (tok,))
    return Actor(r["id"], r["email"], r["name"], r["role"]) if r else None


def destroy_session(db: Database, tok: str) -> None:
    db.run("DELETE FROM sessions WHERE token = ?", (tok,))


# --- scope checks (the whole point of T2.3) --------------------------------

def require(actor: Actor | None) -> Actor:
    if actor is None:
        raise Unauthorized("sign in required")
    return actor


def require_role(actor: Actor | None, *roles: str) -> Actor:
    actor = require(actor)
    if actor.role not in roles:
        raise Forbidden(f"requires role {' or '.join(roles)}")
    return actor


def require_staff(actor: Actor | None) -> Actor:
    return require_role(actor, "organizer", "admin")


def assert_judge_scope(db: Database, actor: Actor | None, requested_judge_id: str | None = None,
                       project_id: str | None = None) -> Actor:
    """Judges may only touch their own assignments. Staff may see everything.

    ``requested_judge_id`` is whatever the caller *asked* for (e.g. ``?judge=``);
    the actor's identity comes from the session and wins.
    """
    actor = require(actor)
    if actor.is_staff:
        return actor
    if actor.role != "judge":
        raise Forbidden("judges only")
    if requested_judge_id and requested_judge_id != actor.id:
        raise Forbidden("you may only view your own scores")
    if project_id is not None:
        ok = db.one("SELECT 1 FROM assignments WHERE judge_id = ? AND project_id = ?",
                    (actor.id, project_id))
        if not ok:
            raise Forbidden("project is not assigned to you")
    return actor
