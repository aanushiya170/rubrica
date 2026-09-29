"""Append-only audit log. Plain, readable, exportable — no hash chain on purpose.

Every mutation that matters for judging integrity calls ``record``. The table
has no UPDATE/DELETE path in the codebase, and ``seq`` is a monotonic rowid so
an organizer can read it top to bottom.
"""
from __future__ import annotations

import json

from . import signals
from .db import Database
from .util import iso, new_id


def record(db: Database, actor_id: str | None, action: str, entity_type: str,
           entity_id: str | None = None, payload: dict | None = None) -> str:
    aid = new_id("aud")
    with db.tx() as c:
        c.execute(
            "INSERT INTO audit_events(id, timestamp, actor_id, action, entity_type, entity_id, payload_json)"
            " VALUES (?,?,?,?,?,?,?)",
            (aid, iso(), actor_id, action, entity_type, entity_id, json.dumps(payload or {}, sort_keys=True)),
        )
    signals.emit(action, {"audit_id": aid, "actor_id": actor_id, "action": action, "entity_type": entity_type,
                          "entity_id": entity_id, "payload": payload or {}})
    return aid


def recent(db: Database, limit: int = 200, action_prefix: str | None = None) -> list[dict]:
    sql = "SELECT * FROM audit_events"
    params: list = []
    if action_prefix:
        sql += " WHERE action LIKE ?"
        params.append(action_prefix + "%")
    sql += " ORDER BY seq DESC LIMIT ?"
    params.append(limit)
    rows = db.all(sql, params)
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d.pop("payload_json") or "{}")
        out.append(d)
    return out


def all_events(db: Database) -> list[dict]:
    return [dict(r) for r in db.all("SELECT * FROM audit_events ORDER BY seq ASC")]
