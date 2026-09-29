"""T3: voting, comments, rate limiting, ballots."""
from __future__ import annotations

import hashlib
import random
import threading
import time
from collections import defaultdict, deque

from ..auth import Actor, require
from ..db import Database
from ..errors import Closed, Conflict, DomainError, Forbidden, NotFound, RateLimited
from ..util import iso, new_id
from .. import audit
from . import events as events_svc
from . import projects as projects_svc
from . import teams as teams_svc


class RateLimiter:
    """Sliding-window limiter keyed by (bucket, key). In-process; enough for one node."""

    def __init__(self):
        self._hits: dict[tuple, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def check(self, bucket: str, key: str, limit: int, window_s: int) -> None:
        now = time.monotonic()
        with self._lock:
            dq = self._hits[(bucket, key)]
            while dq and dq[0] < now - window_s:
                dq.popleft()
            if len(dq) >= limit:
                raise RateLimited(f"too many {bucket} requests; try again in a minute")
            dq.append(now)


limiter = RateLimiter()

VOTE_LIMIT = (10, 60)      # 10 votes / minute / user
COMMENT_LIMIT = (5, 60)    # 5 comments / minute / user
MAX_VOTES_PER_EVENT = 5    # each voter backs at most 5 projects


def ballot(db: Database, event_id: str, actor: Actor | None) -> list[dict]:
    """Submitted projects in an order that is random per voter but stable across reloads."""
    projects = projects_svc.gallery(db, event_id=event_id)
    seed_src = f"{event_id}:{actor.id if actor else 'anon'}"
    seed = int(hashlib.sha256(seed_src.encode()).hexdigest(), 16)
    random.Random(seed).shuffle(projects)
    if actor:
        mine = {r["project_id"] for r in db.all(
            "SELECT project_id FROM votes WHERE event_id = ? AND voter_id = ?", (event_id, actor.id))}
        for p in projects:
            p["voted"] = p["id"] in mine
    return projects


def cast_vote(db: Database, actor: Actor | None, project_id: str, client_key: str = "") -> dict:
    actor = require(actor)
    project = projects_svc.get_project(db, project_id)
    event = events_svc.get_event(db, project["event_id"])
    if not events_svc.voting_open(event):
        raise Closed("voting is not open for this event")
    if project["status"] != "submitted":
        raise DomainError("project is not on the ballot", 400)
    if teams_svc.is_member(db, actor.id, project["team_id"]):
        raise Forbidden("you cannot vote for your own project")
    limiter.check("vote", actor.id, *VOTE_LIMIT)
    if client_key:
        limiter.check("vote-ip", client_key, VOTE_LIMIT[0] * 3, VOTE_LIMIT[1])
    n = db.one("SELECT COUNT(*) AS n FROM votes WHERE event_id = ? AND voter_id = ?", (event["id"], actor.id))["n"]
    if db.one("SELECT 1 FROM votes WHERE project_id = ? AND voter_id = ?", (project_id, actor.id)):
        audit.record(db, actor.id, "vote.duplicate_rejected", "project", project_id, {"client": client_key})
        raise Conflict("you already voted for this project")
    if n >= MAX_VOTES_PER_EVENT:
        raise Conflict(f"you have used all {MAX_VOTES_PER_EVENT} votes for this event")
    vid = new_id("vot")
    db.run("INSERT INTO votes(id, event_id, project_id, voter_id, created_at) VALUES (?,?,?,?,?)",
           (vid, event["id"], project_id, actor.id, iso()))
    audit.record(db, actor.id, "vote.cast", "project", project_id, {"event_id": event["id"], "client": client_key})
    return {"id": vid, "project_id": project_id, "votes_used": n + 1, "votes_max": MAX_VOTES_PER_EVENT}


def retract_vote(db: Database, actor: Actor | None, project_id: str) -> None:
    actor = require(actor)
    project = projects_svc.get_project(db, project_id)
    event = events_svc.get_event(db, project["event_id"])
    if not events_svc.voting_open(event):
        raise Closed("voting is not open for this event")
    db.run("DELETE FROM votes WHERE project_id = ? AND voter_id = ?", (project_id, actor.id))
    audit.record(db, actor.id, "vote.retracted", "project", project_id, {})


def tally(db: Database, event_id: str) -> list[dict]:
    return [dict(r) for r in db.all(
        "SELECT p.id, p.title, t.name AS team_name, COUNT(v.id) AS votes FROM projects p"
        " JOIN teams t ON t.id = p.team_id LEFT JOIN votes v ON v.project_id = p.id"
        " WHERE p.event_id = ? AND p.status = 'submitted' GROUP BY p.id ORDER BY votes DESC, p.id", (event_id,))]


def vote_count(db: Database, project_id: str) -> int:
    return db.one("SELECT COUNT(*) AS n FROM votes WHERE project_id = ?", (project_id,))["n"]


def add_comment(db: Database, actor: Actor | None, project_id: str, body: str) -> dict:
    actor = require(actor)
    projects_svc.get_project(db, project_id)
    body = (body or "").strip()
    if not body:
        raise DomainError("comment is empty", 400)
    if len(body) > 2000:
        raise DomainError("comment too long (2000 chars)", 400)
    limiter.check("comment", actor.id, *COMMENT_LIMIT)
    cid = new_id("cmt")
    db.run("INSERT INTO comments(id, project_id, user_id, body, created_at) VALUES (?,?,?,?,?)",
           (cid, project_id, actor.id, body, iso()))
    audit.record(db, actor.id, "comment.posted", "project", project_id, {"comment_id": cid})
    return {"id": cid, "project_id": project_id, "body": body}


def hide_comment(db: Database, actor: Actor | None, comment_id: str) -> None:
    actor = require(actor)
    if not actor.is_staff:
        raise Forbidden("organizers only")
    db.run("UPDATE comments SET hidden = 1 WHERE id = ?", (comment_id,))
    audit.record(db, actor.id, "comment.hidden", "comment", comment_id, {})


def comments(db: Database, project_id: str) -> list[dict]:
    return [dict(r) for r in db.all(
        "SELECT c.*, u.name AS author FROM comments c JOIN users u ON u.id = c.user_id"
        " WHERE c.project_id = ? AND c.hidden = 0 ORDER BY c.created_at ASC", (project_id,))]
