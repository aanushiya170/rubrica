from __future__ import annotations

from ..auth import Actor
from ..db import Database
from ..errors import Conflict, Forbidden, NotFound
from ..util import iso, new_id, token
from .. import audit

MAX_TEAM = 4


def get_team(db: Database, team_id: str) -> dict:
    r = db.one("SELECT * FROM teams WHERE id = ?", (team_id,))
    if not r:
        raise NotFound("team not found")
    t = dict(r)
    t["members"] = [dict(m) for m in db.all(
        "SELECT u.id, u.name, u.email FROM team_members tm JOIN users u ON u.id = tm.user_id"
        " WHERE tm.team_id = ? ORDER BY u.name", (team_id,))]
    return t


def teams_for_user(db: Database, user_id: str) -> list[dict]:
    rows = db.all(
        "SELECT t.* FROM teams t JOIN team_members tm ON tm.team_id = t.id WHERE tm.user_id = ?"
        " ORDER BY t.created_at DESC", (user_id,))
    return [dict(r) for r in rows]


def team_for_user_in_event(db: Database, user_id: str, event_id: str) -> dict | None:
    r = db.one(
        "SELECT t.* FROM teams t JOIN team_members tm ON tm.team_id = t.id"
        " WHERE tm.user_id = ? AND t.event_id = ?", (user_id, event_id))
    return dict(r) if r else None


def is_member(db: Database, user_id: str, team_id: str) -> bool:
    return db.one("SELECT 1 FROM team_members WHERE team_id = ? AND user_id = ?", (team_id, user_id)) is not None


def create_team(db: Database, actor: Actor, event_id: str, name: str, team_id: str | None = None,
                invite_code: str | None = None, add_creator: bool = True) -> dict:
    if actor.role not in ("participant", "organizer", "admin"):
        raise Forbidden("judges cannot form teams")
    if add_creator and team_for_user_in_event(db, actor.id, event_id):
        raise Conflict("you are already on a team in this event")
    tid = team_id or new_id("tm")
    with db.tx() as c:
        c.execute("INSERT INTO teams(id, event_id, name, invite_code, created_at) VALUES (?,?,?,?,?)",
                  (tid, event_id, name.strip(), invite_code or token(9), iso()))
        if add_creator:
            c.execute("INSERT INTO team_members(team_id, user_id) VALUES (?,?)", (tid, actor.id))
    audit.record(db, actor.id, "team.created", "team", tid, {"event_id": event_id, "name": name})
    return get_team(db, tid)


def join_by_invite(db: Database, actor: Actor, invite_code: str) -> dict:
    r = db.one("SELECT * FROM teams WHERE invite_code = ?", (invite_code,))
    if not r:
        raise NotFound("invite link is not valid")
    team = dict(r)
    if actor.role == "judge":
        raise Forbidden("judges cannot join teams")
    if is_member(db, actor.id, team["id"]):
        return get_team(db, team["id"])
    if team_for_user_in_event(db, actor.id, team["event_id"]):
        raise Conflict("you are already on a team in this event")
    n = db.one("SELECT COUNT(*) AS n FROM team_members WHERE team_id = ?", (team["id"],))["n"]
    if n >= MAX_TEAM:
        raise Conflict("team is full")
    db.run("INSERT INTO team_members(team_id, user_id) VALUES (?,?)", (team["id"], actor.id))
    audit.record(db, actor.id, "team.joined", "team", team["id"], {})
    return get_team(db, team["id"])
