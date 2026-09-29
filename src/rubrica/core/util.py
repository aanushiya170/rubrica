"""Small shared helpers: ids, clocks, hashing."""
from __future__ import annotations

import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None = None) -> str:
    """ISO-8601 UTC with a trailing Z, second precision. Matches the fixture."""
    dt = dt or now()
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    s = s.strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def plus(days: float = 0, hours: float = 0) -> str:
    return iso(now() + timedelta(days=days, hours=hours))


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(6)}"


def token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_json(obj) -> str:
    """Stable hash of a JSON-serialisable object (sorted keys, no spaces)."""
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()
