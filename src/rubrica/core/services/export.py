"""CSV / JSON export paths (organizer)."""
from __future__ import annotations

import csv
import io
import json

from ..db import Database
from . import normalization as norm_svc


def _csv(header: list[str], rows) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(header)
    for r in rows:
        w.writerow(r)
    return buf.getvalue()


def scores_csv(db: Database, event_id: str | None = None) -> str:
    sql = ("SELECT s.id, s.event_id, s.project_id, p.title, s.judge_id, s.rubric_version, s.criteria_json,"
           " s.weighted_raw, s.comment, s.submitted_at FROM scores s JOIN projects p ON p.id = s.project_id")
    params: list = []
    if event_id:
        sql += " WHERE s.event_id = ?"
        params.append(event_id)
    sql += " ORDER BY s.event_id, s.project_id, s.judge_id"
    rows = db.all(sql, params)
    keys: list[str] = []
    for r in rows:
        for k in json.loads(r["criteria_json"]):
            if k not in keys:
                keys.append(k)
    header = ["score_id", "event_id", "project_id", "project_title", "judge_id", "rubric_version",
              *keys, "weighted_raw", "comment", "submitted_at"]
    out = []
    for r in rows:
        crit = json.loads(r["criteria_json"])
        out.append([r["id"], r["event_id"], r["project_id"], r["title"], r["judge_id"], r["rubric_version"],
                    *[crit.get(k, "") for k in keys], r["weighted_raw"], r["comment"], r["submitted_at"]])
    return _csv(header, out)


def results_csv(db: Database, event_id: str) -> str:
    run = norm_svc.latest_run(db, event_id)
    if not run:
        return _csv(["project_id", "title", "team", "review_count", "raw_avg", "normalized", "rank_raw", "rank_norm"], [])
    return _csv(["project_id", "title", "team", "review_count", "raw_avg", "normalized", "rank_raw", "rank_norm"],
                [[r["project_id"], r["title"], r["team_name"], r["review_count"], r["raw_avg"], r["normalized"],
                  r["rank_raw"], r["rank_norm"]] for r in run["results"]])


def projects_csv(db: Database, event_id: str | None = None) -> str:
    sql = ("SELECT p.id, p.event_id, p.team_id, t.name AS team, p.track_id, p.title, p.summary, p.repo_url,"
           " p.demo_url, p.status, p.submitted_at, p.canonical_project_id FROM projects p JOIN teams t ON t.id = p.team_id")
    params: list = []
    if event_id:
        sql += " WHERE p.event_id = ?"
        params.append(event_id)
    sql += " ORDER BY p.event_id, p.id"
    rows = db.all(sql, params)
    return _csv(["project_id", "event_id", "team_id", "team", "track_id", "title", "summary", "repo_url", "demo_url",
                 "status", "submitted_at", "canonical_project_id"], [list(r) for r in rows])


def audit_csv(db: Database) -> str:
    rows = db.all("SELECT seq, id, timestamp, actor_id, action, entity_type, entity_id, payload_json"
                  " FROM audit_events ORDER BY seq")
    return _csv(["seq", "id", "timestamp", "actor_id", "action", "entity_type", "entity_id", "payload"],
                [list(r) for r in rows])


def event_json(db: Database, event_id: str) -> dict:
    """Fixture-shaped JSON of one event — the migration path *out* (and back in via bulk import)."""
    ev = dict(db.one("SELECT * FROM events WHERE id = ?", (event_id,)))
    rub = db.one("SELECT id FROM rubrics WHERE event_id = ? AND active = 1", (event_id,))
    criteria = [dict(r) for r in db.all("SELECT key, name, weight FROM criteria WHERE rubric_id = ? ORDER BY position",
                                         (rub["id"],))] if rub else []
    judges = []
    for j in db.all("SELECT DISTINCT u.id, u.name, u.email FROM users u JOIN judge_tracks jt ON jt.judge_id = u.id"
                    " JOIN tracks t ON t.id = jt.track_id WHERE t.event_id = ? ORDER BY u.id", (event_id,)):
        judges.append({**dict(j), "tracks": [r["track_id"] for r in db.all(
            "SELECT jt.track_id FROM judge_tracks jt JOIN tracks t ON t.id = jt.track_id"
            " WHERE jt.judge_id = ? AND t.event_id = ?", (j["id"], event_id))]})
    teams = []
    for t in db.all("SELECT id, name, invite_code FROM teams WHERE event_id = ? ORDER BY id", (event_id,)):
        teams.append({"id": t["id"], "name": t["name"], "invite_code": t["invite_code"],
                      "members": [r["email"] for r in db.all(
                          "SELECT u.email FROM team_members tm JOIN users u ON u.id = tm.user_id WHERE tm.team_id = ?",
                          (t["id"],))]})
    projects = [{"id": p["id"], "team": p["team_id"], "track": p["track_id"], "title": p["title"],
                 "summary": p["summary"], "repo_url": p["repo_url"], "demo_url": p["demo_url"],
                 "status": p["status"], "submitted_at": p["submitted_at"],
                 "canonical_project_id": p["canonical_project_id"]}
                for p in db.all("SELECT * FROM projects WHERE event_id = ? ORDER BY id", (event_id,))]
    scores = [{"id": s["id"], "judge": s["judge_id"], "project": s["project_id"],
               "criteria": json.loads(s["criteria_json"]), "comment": s["comment"],
               "rubric_version": s["rubric_version"], "submitted_at": s["submitted_at"]}
              for s in db.all("SELECT * FROM scores WHERE event_id = ? ORDER BY id", (event_id,))]
    return {
        "event": {k: ev.get(k) for k in ("id", "name", "description", "submissions_open", "submissions_close",
                                          "voting_open", "voting_close")},
        "tracks": [dict(r) for r in db.all("SELECT id, name FROM tracks WHERE event_id = ? ORDER BY id", (event_id,))],
        "prizes": [dict(r) for r in db.all("SELECT rank, title, amount FROM prizes WHERE event_id = ? ORDER BY rank", (event_id,))],
        "rubric": criteria, "judges": judges, "teams": teams, "projects": projects, "scores": scores,
    }
