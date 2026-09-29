from __future__ import annotations

from ..auth import Actor
from ..db import Database
from ..errors import Forbidden, NotFound
from ..util import iso, new_id, now, parse_iso
from .. import audit


def list_events(db: Database) -> list[dict]:
    return [dict(r) for r in db.all("SELECT * FROM events ORDER BY is_historical ASC, submissions_close DESC")]


def get_event(db: Database, event_id: str) -> dict:
    r = db.one("SELECT * FROM events WHERE id = ?", (event_id,))
    if not r:
        raise NotFound("event not found")
    return dict(r)


def tracks(db: Database, event_id: str) -> list[dict]:
    return [dict(r) for r in db.all("SELECT * FROM tracks WHERE event_id = ? ORDER BY id", (event_id,))]


def prizes(db: Database, event_id: str) -> list[dict]:
    return [dict(r) for r in db.all("SELECT * FROM prizes WHERE event_id = ? ORDER BY rank", (event_id,))]


def create_event(db: Database, actor: Actor, name: str, submissions_close: str,
                 submissions_open: str | None = None, voting_open: str | None = None,
                 voting_close: str | None = None, description: str = "",
                 track_names: list[str] | None = None, prize_rows: list[dict] | None = None,
                 event_id: str | None = None, is_historical: bool = False) -> dict:
    if not actor.is_staff:
        raise Forbidden("organizers only")
    if not name.strip():
        raise ValueError("name required")
    parse_iso(submissions_close)  # validates
    eid = event_id or new_id("evt")
    with db.tx() as c:
        c.execute(
            "INSERT INTO events(id, name, description, submissions_open, submissions_close, voting_open,"
            " voting_close, is_historical, created_by, created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (eid, name.strip(), description, submissions_open, submissions_close, voting_open,
             voting_close, int(is_historical), actor.id, iso()))
        for i, t in enumerate(track_names or []):
            if t.strip():
                c.execute("INSERT INTO tracks(id, event_id, name) VALUES (?,?,?)",
                          (f"{eid}_trk_{i+1:02d}", eid, t.strip()))
        for p in prize_rows or []:
            c.execute("INSERT INTO prizes(id, event_id, rank, title, amount) VALUES (?,?,?,?,?)",
                      (new_id("prz"), eid, int(p["rank"]), p["title"], str(p.get("amount", ""))))
    audit.record(db, actor.id, "event.created", "event", eid, {"name": name})
    return get_event(db, eid)


def update_event(db: Database, actor: Actor, event_id: str, **fields) -> dict:
    if not actor.is_staff:
        raise Forbidden("organizers only")
    allowed = {"name", "description", "submissions_open", "submissions_close", "voting_open", "voting_close"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not sets:
        return get_event(db, event_id)
    for k in ("submissions_open", "submissions_close", "voting_open", "voting_close"):
        if k in sets and sets[k]:
            parse_iso(sets[k])
    with db.tx() as c:
        c.execute(f"UPDATE events SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                  (*sets.values(), event_id))
    audit.record(db, actor.id, "event.updated", "event", event_id, sets)
    return get_event(db, event_id)


def add_track(db: Database, actor: Actor, event_id: str, name: str) -> dict:
    if not actor.is_staff:
        raise Forbidden("organizers only")
    tid = new_id("trk")
    db.run("INSERT INTO tracks(id, event_id, name) VALUES (?,?,?)", (tid, event_id, name.strip()))
    return {"id": tid, "event_id": event_id, "name": name.strip()}


def submissions_open(event: dict, at=None) -> bool:
    at = at or now()
    if event.get("is_historical"):
        return False
    o = parse_iso(event.get("submissions_open"))
    c = parse_iso(event.get("submissions_close"))
    if o and at < o:
        return False
    return c is None or at < c


def voting_open(event: dict, at=None) -> bool:
    at = at or now()
    o, c = parse_iso(event.get("voting_open")), parse_iso(event.get("voting_close"))
    if not o or not c:
        return False
    return o <= at < c


def phase(event: dict) -> str:
    if submissions_open(event):
        return "submissions"
    if voting_open(event):
        return "voting"
    if event.get("results_published"):
        return "published"
    return "judging"
