"""Judge invitation, rubric, assignment engine and score submission."""
from __future__ import annotations

import json
import random

from ..auth import Actor, assert_judge_scope, require_staff
from ..db import Database
from ..errors import Conflict, DomainError, Forbidden, NotFound
from ..util import iso, new_id, token
from .. import audit

# --- rubric ----------------------------------------------------------------

def active_rubric(db: Database, event_id: str) -> dict | None:
    r = db.one("SELECT * FROM rubrics WHERE event_id = ? AND active = 1 ORDER BY version DESC LIMIT 1",
               (event_id,))
    if not r:
        return None
    rub = dict(r)
    rub["criteria"] = [dict(c) for c in db.all(
        "SELECT * FROM criteria WHERE rubric_id = ? ORDER BY position, key", (rub["id"],))]
    return rub


def rubric_by_version(db: Database, event_id: str, version: int) -> dict | None:
    r = db.one("SELECT * FROM rubrics WHERE event_id = ? AND version = ?", (event_id, version))
    if not r:
        return None
    rub = dict(r)
    rub["criteria"] = [dict(c) for c in db.all(
        "SELECT * FROM criteria WHERE rubric_id = ? ORDER BY position, key", (rub["id"],))]
    return rub


def set_rubric(db: Database, actor: Actor, event_id: str, criteria: list[dict]) -> dict:
    """Create a new rubric version. Weights must sum to 1.0 (±0.001).

    Old versions stay in the table; every score remembers its ``rubric_version``.
    """
    require_staff(actor)
    if not criteria:
        raise DomainError("at least one criterion", 400)
    total = sum(float(c["weight"]) for c in criteria)
    if abs(total - 1.0) > 1e-3:
        raise DomainError(f"weights must sum to 1.0 (got {total:.3f})", 400)
    keys = [c["key"].strip().lower() for c in criteria]
    if len(set(keys)) != len(keys):
        raise DomainError("criterion keys must be unique", 400)
    prev = db.one("SELECT MAX(version) AS v FROM rubrics WHERE event_id = ?", (event_id,))["v"] or 0
    rid = new_id("rub")
    with db.tx() as c:
        c.execute("UPDATE rubrics SET active = 0 WHERE event_id = ?", (event_id,))
        c.execute("INSERT INTO rubrics(id, event_id, version, active, created_at) VALUES (?,?,?,1,?)",
                  (rid, event_id, prev + 1, iso()))
        for i, cr in enumerate(criteria):
            c.execute("INSERT INTO criteria(id, rubric_id, key, name, weight, position) VALUES (?,?,?,?,?,?)",
                      (new_id("crt"), rid, cr["key"].strip().lower(), cr.get("name") or cr["key"],
                       float(cr["weight"]), i))
    audit.record(db, actor.id, "rubric.created", "rubric", rid, {"event_id": event_id, "version": prev + 1})
    return active_rubric(db, event_id)


def weighted(criteria_scores: dict, rubric: dict) -> float:
    return round(sum(float(criteria_scores[c["key"]]) * c["weight"] for c in rubric["criteria"]), 6)


# --- judges ------------------------------------------------------------------

def event_judges(db: Database, event_id: str) -> list[dict]:
    rows = db.all(
        "SELECT DISTINCT u.id, u.name, u.email FROM users u"
        " JOIN judge_tracks jt ON jt.judge_id = u.id JOIN tracks t ON t.id = jt.track_id"
        " WHERE t.event_id = ? AND u.role = 'judge' ORDER BY u.id", (event_id,))
    out = []
    for r in rows:
        d = dict(r)
        d["tracks"] = [x["track_id"] for x in db.all(
            "SELECT jt.track_id FROM judge_tracks jt JOIN tracks t ON t.id = jt.track_id"
            " WHERE jt.judge_id = ? AND t.event_id = ? ORDER BY jt.track_id", (d["id"], event_id))]
        out.append(d)
    return out


def set_judge_tracks(db: Database, judge_id: str, track_ids: list[str]) -> None:
    with db.tx() as c:
        for t in track_ids:
            c.execute("INSERT OR IGNORE INTO judge_tracks(judge_id, track_id) VALUES (?,?)", (judge_id, t))


def invite_judge(db: Database, actor: Actor, event_id: str, email: str, track_ids: list[str]) -> dict:
    require_staff(actor)
    iid, tok = new_id("inv"), token(12)
    db.run("INSERT INTO judge_invites(id, event_id, email, token, track_ids, created_at) VALUES (?,?,?,?,?,?)",
           (iid, event_id, email.strip().lower(), tok, json.dumps(track_ids), iso()))
    audit.record(db, actor.id, "judge.invited", "judge_invite", iid, {"email": email, "event_id": event_id})
    return {"id": iid, "token": tok, "email": email, "event_id": event_id, "track_ids": track_ids}


def accept_judge_invite(db: Database, actor: Actor, tok: str) -> dict:
    r = db.one("SELECT * FROM judge_invites WHERE token = ?", (tok,))
    if not r:
        raise NotFound("invite not found")
    inv = dict(r)
    if actor.role not in ("judge", "participant"):
        raise Forbidden("this invite is for a judge account")
    with db.tx() as c:
        c.execute("UPDATE users SET role = 'judge' WHERE id = ?", (actor.id,))
        c.execute("UPDATE judge_invites SET accepted_by = ? WHERE id = ?", (actor.id, inv["id"]))
    set_judge_tracks(db, actor.id, json.loads(inv["track_ids"]))
    audit.record(db, actor.id, "judge.invite.accepted", "judge_invite", inv["id"], {})
    return inv


# --- assignment engine ---------------------------------------------------------

def assign(db: Database, actor: Actor, event_id: str, reviews_per_project: int = 3,
           anchor_count: int = 3, seed: int = 42) -> dict:
    """Blind-overlap assignment.

    * Judges only get projects in tracks they cover.
    * A judge never gets their own team's project.
    * ``anchor_count`` projects per track are *anchors*: every eligible judge of
      that track reviews them, so calibration has a shared reference.
    * Remaining projects are dealt round-robin to the least-loaded eligible judges
      until each has ``reviews_per_project`` reviewers.
    * Nobody ever sees anyone else's numbers — the overlap is in *what* is
      reviewed, not in *who sees whose scores*.
    Idempotent: existing (project, judge) pairs are kept.
    """
    require_staff(actor)
    rng = random.Random(seed)
    judges = event_judges(db, event_id)
    if not judges:
        raise DomainError("no judges cover this event's tracks", 400)
    projects = [dict(r) for r in db.all(
        "SELECT id, track_id, team_id FROM projects WHERE event_id = ? AND status = 'submitted' ORDER BY id",
        (event_id,))]
    own = {}
    for j in judges:
        own[j["id"]] = {r["team_id"] for r in db.all(
            "SELECT team_id FROM team_members WHERE user_id = ?", (j["id"],))}
    load = {j["id"]: db.one("SELECT COUNT(*) AS n FROM assignments WHERE judge_id = ? AND event_id = ?",
                            (j["id"], event_id))["n"] for j in judges}
    created, anchors = 0, 0
    by_track: dict[str, list[dict]] = {}
    for p in projects:
        by_track.setdefault(p["track_id"], []).append(p)
    with db.tx() as c:
        for track_id, plist in by_track.items():
            # Judges cover tracks; a project with no track is open to every judge of the event.
            eligible = judges if track_id is None else [j for j in judges if track_id in j["tracks"]]
            if not eligible:
                continue
            plist = sorted(plist, key=lambda p: p["id"])
            rng.shuffle(plist)
            anchor_ids = {p["id"] for p in plist[:anchor_count]}
            for p in plist:
                ok = [j for j in eligible if p["team_id"] not in own[j["id"]]]
                if p["id"] in anchor_ids:
                    chosen = ok
                else:
                    ok.sort(key=lambda j: (load[j["id"]], rng.random()))
                    chosen = ok[:reviews_per_project]
                for j in chosen:
                    cur = c.execute("INSERT OR IGNORE INTO assignments(id, event_id, project_id, judge_id,"
                                    " is_anchor, created_at) VALUES (?,?,?,?,?,?)",
                                    (new_id("asg"), event_id, p["id"], j["id"], int(p["id"] in anchor_ids), iso()))
                    if cur.rowcount:
                        created += 1
                        load[j["id"]] += 1
                        anchors += int(p["id"] in anchor_ids)
    audit.record(db, actor.id, "assignments.generated", "event", event_id,
                 {"created": created, "anchor_assignments": anchors, "seed": seed})
    return {"created": created, "anchor_assignments": anchors, "judges": len(judges), "projects": len(projects)}


def assign_one(db: Database, actor: Actor, event_id: str, project_id: str, judge_id: str,
               is_anchor: bool = False) -> dict:
    require_staff(actor)
    aid = new_id("asg")
    try:
        db.run("INSERT INTO assignments(id, event_id, project_id, judge_id, is_anchor, created_at)"
               " VALUES (?,?,?,?,?,?)", (aid, event_id, project_id, judge_id, int(is_anchor), iso()))
    except Exception as e:  # UNIQUE
        raise Conflict("already assigned") from e
    audit.record(db, actor.id, "assignment.created", "assignment", aid,
                 {"project_id": project_id, "judge_id": judge_id})
    return {"id": aid}


# --- the judge's view --------------------------------------------------------------

def judge_queue(db: Database, actor: Actor, judge_id: str | None = None) -> list[dict]:
    """Assignments + own score (if any) for ONE judge. Scope-checked."""
    actor = assert_judge_scope(db, actor, requested_judge_id=judge_id)
    target = judge_id or actor.id
    if not actor.is_staff:
        target = actor.id  # identity from the session, never from the query
    rows = db.all(
        "SELECT a.id AS assignment_id, a.project_id, a.event_id, a.is_anchor, p.title, p.summary,"
        " p.repo_url, p.demo_url, p.status, t.name AS team_name, tr.name AS track_name,"
        " s.id AS score_id, s.criteria_json, s.weighted_raw, s.comment, s.submitted_at, s.rubric_version"
        " FROM assignments a JOIN projects p ON p.id = a.project_id JOIN teams t ON t.id = p.team_id"
        " LEFT JOIN tracks tr ON tr.id = p.track_id"
        " LEFT JOIN scores s ON s.assignment_id = a.id"
        " WHERE a.judge_id = ? ORDER BY a.event_id, p.id", (target,))
    out = []
    for r in rows:
        d = dict(r)
        d["criteria"] = json.loads(d.pop("criteria_json")) if d.get("criteria_json") else None
        out.append(d)
    return out


def submit_score(db: Database, actor: Actor, project_id: str, criteria: dict, comment: str = "") -> dict:
    actor = assert_judge_scope(db, actor, project_id=project_id)
    a = db.one("SELECT * FROM assignments WHERE judge_id = ? AND project_id = ?", (actor.id, project_id))
    if not a and actor.is_staff:
        raise Forbidden("organizers do not score; assign a judge")
    rubric = active_rubric(db, a["event_id"])
    if not rubric:
        raise DomainError("event has no rubric", 400)
    clean = {}
    for c in rubric["criteria"]:
        if c["key"] not in criteria:
            raise DomainError(f"missing criterion {c['key']}", 400)
        v = float(criteria[c["key"]])
        if not 1 <= v <= 5:
            raise DomainError(f"{c['key']} must be between 1 and 5", 400)
        clean[c["key"]] = v
    w = weighted(clean, rubric)
    existing = db.one("SELECT id FROM scores WHERE assignment_id = ?", (a["id"],))
    sid = existing["id"] if existing else new_id("scr")
    with db.tx() as c:
        if existing:
            c.execute("UPDATE scores SET criteria_json = ?, weighted_raw = ?, comment = ?, submitted_at = ?,"
                      " rubric_version = ? WHERE id = ?",
                      (json.dumps(clean, sort_keys=True), w, comment or "", iso(), rubric["version"], sid))
        else:
            c.execute("INSERT INTO scores(id, assignment_id, event_id, judge_id, project_id, rubric_version,"
                      " criteria_json, weighted_raw, comment, submitted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (sid, a["id"], a["event_id"], actor.id, project_id, rubric["version"],
                       json.dumps(clean, sort_keys=True), w, comment or "", iso()))
    audit.record(db, actor.id, "score.updated" if existing else "score.submitted", "score", sid,
                 {"project_id": project_id, "weighted_raw": w, "rubric_version": rubric["version"]})
    return {"id": sid, "weighted_raw": w, "criteria": clean, "rubric_version": rubric["version"]}


# --- organizer dashboard -------------------------------------------------------------

def progress(db: Database, actor: Actor, event_id: str) -> dict:
    require_staff(actor)
    judges = db.all(
        "SELECT u.id, u.name, COUNT(a.id) AS assigned, COUNT(s.id) AS scored"
        " FROM users u JOIN assignments a ON a.judge_id = u.id AND a.event_id = ?"
        " LEFT JOIN scores s ON s.assignment_id = a.id GROUP BY u.id ORDER BY u.id", (event_id,))
    per_judge = []
    for j in judges:
        d = dict(j)
        d["remaining"] = d["assigned"] - d["scored"]
        d["not_started"] = d["scored"] == 0
        per_judge.append(d)
    per_project = [dict(r) for r in db.all(
        "SELECT p.id, p.title, p.status, COUNT(a.id) AS assigned, COUNT(s.id) AS reviews"
        " FROM projects p LEFT JOIN assignments a ON a.project_id = p.id"
        " LEFT JOIN scores s ON s.assignment_id = a.id"
        " WHERE p.event_id = ? AND p.status != 'draft' GROUP BY p.id ORDER BY p.id", (event_id,))]
    tot_a = sum(j["assigned"] for j in per_judge)
    tot_s = sum(j["scored"] for j in per_judge)
    return {
        "per_judge": per_judge,
        "per_project": per_project,
        "total_assigned": tot_a,
        "total_scored": tot_s,
        "percent": round(100 * tot_s / tot_a, 1) if tot_a else 0.0,
        "judges_not_started": [j for j in per_judge if j["not_started"]],
    }
