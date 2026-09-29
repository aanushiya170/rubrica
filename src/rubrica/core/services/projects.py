"""Projects: draft → submitted, editable until the event's submissions_close.

Deadline enforcement is server-side and uses the event's own dates. Nothing
here trusts the client's clock or a hidden form field.
"""
from __future__ import annotations

from ..auth import Actor
from ..db import Database
from ..errors import Closed, DomainError, Forbidden, NotFound
from ..util import iso, new_id
from .. import audit
from . import events as events_svc
from . import teams as teams_svc

GALLERY_SQL = """
SELECT p.*, t.name AS team_name, tr.name AS track_name, e.name AS event_name
FROM projects p
JOIN teams t ON t.id = p.team_id
LEFT JOIN tracks tr ON tr.id = p.track_id
JOIN events e ON e.id = p.event_id
"""


def get_project(db: Database, project_id: str) -> dict:
    r = db.one(GALLERY_SQL + " WHERE p.id = ?", (project_id,))
    if not r:
        raise NotFound("project not found")
    return dict(r)


def gallery(db: Database, q: str = "", track_id: str = "", event_id: str = "",
            include_drafts: bool = False) -> list[dict]:
    """Deterministic ordering: event (fixture first), then project id ascending."""
    where, params = [], []
    if not include_drafts:
        where.append("p.status = 'submitted'")
    if q:
        where.append("(p.title LIKE ? OR p.summary LIKE ? OR t.name LIKE ?)")
        params += [f"%{q}%"] * 3
    if track_id:
        where.append("p.track_id = ?")
        params.append(track_id)
    if event_id:
        where.append("p.event_id = ?")
        params.append(event_id)
    sql = GALLERY_SQL
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY e.is_historical DESC, e.created_at ASC, p.id ASC"
    return [dict(r) for r in db.all(sql, params)]


def projects_for_user(db: Database, user_id: str) -> list[dict]:
    return [dict(r) for r in db.all(
        GALLERY_SQL + " JOIN team_members tm ON tm.team_id = p.team_id WHERE tm.user_id = ?"
        " ORDER BY p.updated_at DESC", (user_id,))]


def _writable(db: Database, actor: Actor, project: dict) -> None:
    if actor.is_staff:
        return
    if not teams_svc.is_member(db, actor.id, project["team_id"]):
        raise Forbidden("not your team's project")
    event = events_svc.get_event(db, project["event_id"])
    if not events_svc.submissions_open(event):
        raise Closed(f"submissions for {event['name']} closed at {event['submissions_close']}")


def resolve_event_for_submission(db: Database, actor: Actor, event_id: str | None) -> tuple[dict, dict]:
    """Return (event, team). Explicit event wins; otherwise the actor's only team."""
    if event_id:
        event = events_svc.get_event(db, event_id)
        team = teams_svc.team_for_user_in_event(db, actor.id, event_id)
    else:
        teams = teams_svc.teams_for_user(db, actor.id)
        if len(teams) != 1:
            raise DomainError("event_id is required when you belong to zero or several teams", 400)
        team = teams[0]
        event = events_svc.get_event(db, team["event_id"])
    if not team:
        raise Forbidden("join or create a team in this event before submitting")
    return event, team


def create_project(db: Database, actor: Actor, title: str, summary: str = "", repo_url: str = "",
                   demo_url: str = "", track_id: str | None = None, event_id: str | None = None,
                   submit: bool = False) -> dict:
    if actor.role not in ("participant", "organizer", "admin"):
        raise Forbidden("judges cannot submit projects")
    if not title or not title.strip():
        raise DomainError("title is required", 400)
    event, team = resolve_event_for_submission(db, actor, event_id)
    if not events_svc.submissions_open(event):
        raise Closed(f"submissions for {event['name']} closed at {event['submissions_close']}")
    if track_id and not db.one("SELECT 1 FROM tracks WHERE id = ? AND event_id = ?", (track_id, event["id"])):
        raise DomainError("track does not belong to this event", 400)
    pid = new_id("prj")
    ts = iso()
    with db.tx() as c:
        c.execute(
            "INSERT INTO projects(id, event_id, team_id, track_id, title, summary, repo_url, demo_url,"
            " status, submitted_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (pid, event["id"], team["id"], track_id or None, title.strip(), summary or "", repo_url or "",
             demo_url or "", "submitted" if submit else "draft", ts if submit else None, ts, ts))
    audit.record(db, actor.id, "project.submitted" if submit else "project.drafted", "project", pid,
                 {"event_id": event["id"], "title": title})
    return get_project(db, pid)


def update_project(db: Database, actor: Actor, project_id: str, **fields) -> dict:
    project = get_project(db, project_id)
    _writable(db, actor, project)
    allowed = {"title", "summary", "repo_url", "demo_url", "track_id", "thumbnail_url"}
    sets = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "title" in sets and not sets["title"].strip():
        raise DomainError("title is required", 400)
    if sets:
        sets["updated_at"] = iso()
        with db.tx() as c:
            c.execute(f"UPDATE projects SET {', '.join(f'{k} = ?' for k in sets)} WHERE id = ?",
                      (*sets.values(), project_id))
        audit.record(db, actor.id, "project.updated", "project", project_id, {"fields": sorted(sets)})
    return get_project(db, project_id)


def submit_project(db: Database, actor: Actor, project_id: str) -> dict:
    project = get_project(db, project_id)
    _writable(db, actor, project)
    if project["status"] == "submitted":
        return project
    ts = iso()
    db.run("UPDATE projects SET status = 'submitted', submitted_at = ?, updated_at = ? WHERE id = ?",
           (ts, ts, project_id))
    audit.record(db, actor.id, "project.submitted", "project", project_id, {})
    return get_project(db, project_id)


def withdraw_to_draft(db: Database, actor: Actor, project_id: str) -> dict:
    project = get_project(db, project_id)
    _writable(db, actor, project)
    db.run("UPDATE projects SET status = 'draft', updated_at = ? WHERE id = ?", (iso(), project_id))
    audit.record(db, actor.id, "project.unsubmitted", "project", project_id, {})
    return get_project(db, project_id)


def mark_duplicate(db: Database, actor: Actor, project_id: str, canonical_id: str, action: str) -> dict:
    """Organizer resolution of a duplicate. Never deletes; sets canonical_project_id.

    action: 'merge' (exclude from ranking, point at canonical) | 'exclude' | 'keep'.
    """
    if not actor.is_staff:
        raise Forbidden("organizers only")
    get_project(db, project_id)
    if action == "keep":
        db.run("UPDATE projects SET canonical_project_id = NULL, status = 'submitted' WHERE id = ?", (project_id,))
    elif action in ("merge", "exclude"):
        get_project(db, canonical_id)
        if canonical_id == project_id:
            raise DomainError("a project cannot be canonical for itself", 400)
        db.run("UPDATE projects SET canonical_project_id = ?, status = 'excluded' WHERE id = ?",
               (canonical_id, project_id))
    else:
        raise DomainError("action must be keep, merge or exclude", 400)
    audit.record(db, actor.id, f"project.duplicate.{action}", "project", project_id, {"canonical": canonical_id})
    return get_project(db, project_id)
