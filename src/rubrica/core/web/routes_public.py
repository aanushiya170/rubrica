"""Visitor-facing pages: home, gallery, project detail, login/register, results, ballots."""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import auth, audit
from ..errors import DomainError, Forbidden, NotFound
from ..http import body_fields, current_actor, get_db, redirect, render
from ..services import community, events as events_svc, normalization, projects as projects_svc, results, teams as teams_svc

router = APIRouter()


@router.get("/")
def home(request: Request):
    db = get_db(request)
    evs = events_svc.list_events(db)
    for e in evs:
        e["project_count"] = db.one("SELECT COUNT(*) AS n FROM projects WHERE event_id = ? AND status = 'submitted'",
                                    (e["id"],))["n"]
        e["phase"] = events_svc.phase(e)
    return render(request, "home.html", {"events": evs})


@router.get("/projects")
def gallery(request: Request, q: str = "", track: str = "", event: str = ""):
    db = get_db(request)
    rows = projects_svc.gallery(db, q=q, track_id=track, event_id=event)
    tracks = [dict(r) for r in db.all(
        "SELECT t.*, e.name AS event_name FROM tracks t JOIN events e ON e.id = t.event_id ORDER BY e.is_historical DESC, t.id")]
    evs = events_svc.list_events(db)
    return render(request, "gallery.html", {"projects": rows, "tracks": tracks, "events": evs,
                                            "q": q, "track": track, "event": event})


@router.get("/projects/{project_id}")
def project_detail(request: Request, project_id: str):
    if project_id == "new":
        return new_project_form(request)
    db = get_db(request)
    p = projects_svc.get_project(db, project_id)
    actor = current_actor(request)
    if p["status"] == "draft" and not (actor and (actor.is_staff or teams_svc.is_member(db, actor.id, p["team_id"]))):
        raise NotFound("project not found")
    event = events_svc.get_event(db, p["event_id"])
    team = teams_svc.get_team(db, p["team_id"])
    can_edit = bool(actor and (actor.is_staff or teams_svc.is_member(db, actor.id, team["id"]))) \
        and events_svc.submissions_open(event)
    voting = events_svc.voting_open(event)
    voted = bool(actor and db.one("SELECT 1 FROM votes WHERE project_id = ? AND voter_id = ?", (project_id, actor.id)))
    show_votes = results.results_visible(db, event, actor)
    return render(request, "project.html", {
        "p": p, "event": event, "team": team, "can_edit": can_edit, "voting": voting, "voted": voted,
        "comments": community.comments(db, project_id), "show_votes": show_votes,
        "votes": community.vote_count(db, project_id) if show_votes else None,
        "is_member": bool(actor and teams_svc.is_member(db, actor.id, team["id"])),
        "canonical": projects_svc.get_project(db, p["canonical_project_id"]) if p["canonical_project_id"] else None,
    })


def new_project_form(request: Request):
    db = get_db(request)
    actor = auth.require(current_actor(request))
    my_teams = teams_svc.teams_for_user(db, actor.id)
    options = []
    for t in my_teams:
        e = events_svc.get_event(db, t["event_id"])
        options.append({"team": t, "event": e, "open": events_svc.submissions_open(e), "tracks": events_svc.tracks(db, e["id"])})
    return render(request, "project_form.html", {"options": options, "p": None})


@router.post("/projects/new")
async def create_project(request: Request):
    """Create a project (draft or submitted). JSON or form. 4xx when the event is closed."""
    db = get_db(request)
    actor = auth.require(current_actor(request))
    data = await body_fields(request)
    p = projects_svc.create_project(
        db, actor, title=str(data.get("title", "")), summary=str(data.get("summary", "")),
        repo_url=str(data.get("repo_url", "")), demo_url=str(data.get("demo_url", "")),
        track_id=data.get("track_id") or None, event_id=data.get("event_id") or None,
        submit=str(data.get("action", data.get("submit", ""))).lower() in ("submit", "true", "1", "yes"))
    if "application/json" in request.headers.get("content-type", ""):
        from fastapi.responses import JSONResponse
        return JSONResponse(p, status_code=201)
    return redirect(f"/projects/{p['id']}", "Project saved." if p["status"] == "draft" else "Project submitted.")


@router.get("/projects/{project_id}/edit")
def edit_project_form(request: Request, project_id: str):
    db = get_db(request)
    actor = auth.require(current_actor(request))
    p = projects_svc.get_project(db, project_id)
    if not (actor.is_staff or teams_svc.is_member(db, actor.id, p["team_id"])):
        raise Forbidden("not your project")
    e = events_svc.get_event(db, p["event_id"])
    return render(request, "project_form.html", {"p": p, "event": e, "tracks": events_svc.tracks(db, e["id"]),
                                                 "open": events_svc.submissions_open(e), "options": []})


@router.post("/projects/{project_id}/edit")
async def edit_project(request: Request, project_id: str):
    db = get_db(request)
    actor = auth.require(current_actor(request))
    data = await body_fields(request)
    projects_svc.update_project(db, actor, project_id, title=data.get("title"), summary=data.get("summary"),
                                repo_url=data.get("repo_url"), demo_url=data.get("demo_url"),
                                track_id=data.get("track_id") or None)
    action = str(data.get("action", "")).lower()
    if action == "submit":
        projects_svc.submit_project(db, actor, project_id)
    elif action == "unsubmit":
        projects_svc.withdraw_to_draft(db, actor, project_id)
    return redirect(f"/projects/{project_id}", "Saved.")


@router.post("/projects/{project_id}/vote")
async def vote(request: Request, project_id: str):
    db = get_db(request)
    data = await body_fields(request)
    client = request.client.host if request.client else ""
    if data.get("action") == "retract":
        community.retract_vote(db, current_actor(request), project_id)
        return redirect(request.headers.get("referer") or f"/projects/{project_id}", "Vote retracted.")
    out = community.cast_vote(db, current_actor(request), project_id, client_key=client)
    return redirect(request.headers.get("referer") or f"/projects/{project_id}",
                    f"Vote recorded ({out['votes_used']}/{out['votes_max']} used).")


@router.post("/projects/{project_id}/comments")
async def comment(request: Request, project_id: str):
    data = await body_fields(request)
    community.add_comment(get_db(request), current_actor(request), project_id, data.get("body", ""))
    return redirect(f"/projects/{project_id}#comments", "Comment posted.")


@router.get("/events/{event_id}")
def event_page(request: Request, event_id: str):
    db = get_db(request)
    e = events_svc.get_event(db, event_id)
    actor = current_actor(request)
    return render(request, "event.html", {
        "e": e, "tracks": events_svc.tracks(db, event_id), "prizes": events_svc.prizes(db, event_id),
        "projects": projects_svc.gallery(db, event_id=event_id),
        "my_team": teams_svc.team_for_user_in_event(db, actor.id, event_id) if actor else None,
        "open": events_svc.submissions_open(e), "voting": events_svc.voting_open(e),
        "results_visible": results.results_visible(db, e, actor),
    })


@router.get("/events/{event_id}/ballot")
def ballot(request: Request, event_id: str):
    db = get_db(request)
    e = events_svc.get_event(db, event_id)
    actor = current_actor(request)
    used = db.one("SELECT COUNT(*) AS n FROM votes WHERE event_id = ? AND voter_id = ?",
                  (event_id, actor.id))["n"] if actor else 0
    return render(request, "ballot.html", {"e": e, "projects": community.ballot(db, event_id, actor),
                                           "voting": events_svc.voting_open(e), "used": used,
                                           "max": community.MAX_VOTES_PER_EVENT})


@router.get("/events/{event_id}/results")
def results_page(request: Request, event_id: str):
    db = get_db(request)
    e = events_svc.get_event(db, event_id)
    actor = current_actor(request)
    visible = results.results_visible(db, e, actor)
    snap = results.latest_snapshot(db, event_id) if visible else None
    run = normalization.load_run(db, snap["normalization_run_id"]) if snap else None
    titles = {p["id"]: p for p in projects_svc.gallery(db, event_id=event_id, include_drafts=True)}
    return render(request, "results.html", {"e": e, "visible": visible, "snap": snap, "run": run, "titles": titles,
                                            "tally": community.tally(db, event_id) if visible else [],
                                            "voting": events_svc.voting_open(e)})


# --- auth pages -----------------------------------------------------------------

@router.get("/login")
def login_form(request: Request, next: str = "/"):
    return render(request, "login.html", {"next": next, "mode": "login"})


@router.post("/login")
async def login(request: Request):
    db = get_db(request)
    data = await body_fields(request)
    actor = auth.authenticate(db, str(data.get("email", "")), str(data.get("password", "")))
    if not actor:
        audit.record(db, None, "auth.login_failed", "user", None, {"email": str(data.get("email", ""))[:80]})
        return render(request, "login.html", {"error": "Wrong email or password.", "next": data.get("next", "/"),
                                              "mode": "login"}, status_code=401)
    tok = auth.create_session(db, actor.id)
    audit.record(db, actor.id, "auth.login", "user", actor.id, {})
    nxt = str(data.get("next") or "/")
    if not nxt.startswith("/"):
        nxt = "/"
    resp = redirect(nxt, f"Signed in as {actor.name}.")
    resp.set_cookie("session", tok, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 14)
    return resp


@router.get("/register")
def register_form(request: Request, next: str = "/", invite: str = ""):
    return render(request, "login.html", {"next": next, "mode": "register", "invite": invite})


@router.post("/register")
async def register(request: Request):
    db = get_db(request)
    data = await body_fields(request)
    email, name, pw = str(data.get("email", "")).strip(), str(data.get("name", "")).strip(), str(data.get("password", ""))
    if not email or "@" not in email or not name or len(pw) < 6:
        return render(request, "login.html", {"error": "Need a valid email, a name and a password of 6+ chars.",
                                              "mode": "register", "next": data.get("next", "/")}, status_code=400)
    if auth.get_user_by_email(db, email):
        return render(request, "login.html", {"error": "That email already has an account.", "mode": "register",
                                              "next": data.get("next", "/")}, status_code=409)
    actor = auth.create_user(db, email, name, pw, "participant")
    audit.record(db, actor.id, "auth.registered", "user", actor.id, {})
    tok = auth.create_session(db, actor.id)
    nxt = str(data.get("next") or "/")
    if data.get("invite"):
        nxt = f"/join/{data['invite']}"
    resp = redirect(nxt if nxt.startswith("/") else "/", "Welcome aboard.")
    resp.set_cookie("session", tok, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 14)
    return resp


@router.post("/logout")
def logout(request: Request):
    tok = request.cookies.get("session")
    if tok:
        auth.destroy_session(get_db(request), tok)
    resp = redirect("/", "Signed out.")
    resp.delete_cookie("session")
    return resp


# --- teams ------------------------------------------------------------------------

@router.get("/join/{invite_code}")
def join_page(request: Request, invite_code: str):
    db = get_db(request)
    actor = current_actor(request)
    r = db.one("SELECT * FROM teams WHERE invite_code = ?", (invite_code,))
    if not r:
        raise NotFound("invite link is not valid")
    team = teams_svc.get_team(db, r["id"])
    if not actor:
        return render(request, "login.html", {"mode": "register", "invite": invite_code,
                                              "next": f"/join/{invite_code}",
                                              "notice": f"Sign up or sign in to join {team['name']}."})
    return render(request, "join.html", {"team": team, "event": events_svc.get_event(db, team["event_id"])})


@router.post("/join/{invite_code}")
def join(request: Request, invite_code: str):
    db = get_db(request)
    team = teams_svc.join_by_invite(db, auth.require(current_actor(request)), invite_code)
    return redirect(f"/teams/{team['id']}", f"You joined {team['name']}.")


@router.get("/teams/{team_id}")
def team_page(request: Request, team_id: str):
    db = get_db(request)
    team = teams_svc.get_team(db, team_id)
    actor = current_actor(request)
    member = bool(actor and (actor.is_staff or teams_svc.is_member(db, actor.id, team_id)))
    projects = [dict(r) for r in db.all(projects_svc.GALLERY_SQL + " WHERE p.team_id = ? ORDER BY p.id", (team_id,))]
    if not member:
        projects = [p for p in projects if p["status"] == "submitted"]
    return render(request, "team.html", {"team": team, "member": member, "projects": projects,
                                         "event": events_svc.get_event(db, team["event_id"]),
                                         "base": str(request.base_url).rstrip("/")})


@router.post("/events/{event_id}/teams")
async def create_team(request: Request, event_id: str):
    db = get_db(request)
    data = await body_fields(request)
    name = str(data.get("name", "")).strip()
    if not name:
        raise DomainError("team name required", 400)
    team = teams_svc.create_team(db, auth.require(current_actor(request)), event_id, name)
    return redirect(f"/teams/{team['id']}", "Team created. Share the invite link.")


@router.get("/dashboard")
def dashboard(request: Request):
    db = get_db(request)
    actor = auth.require(current_actor(request))
    if actor.is_staff:
        return redirect("/organizer")
    if actor.is_judge:
        return redirect("/judge")
    teams = teams_svc.teams_for_user(db, actor.id)
    for t in teams:
        t["event"] = events_svc.get_event(db, t["event_id"])
    return render(request, "dashboard.html", {"teams": teams, "projects": projects_svc.projects_for_user(db, actor.id),
                                              "open_events": [e for e in events_svc.list_events(db) if events_svc.submissions_open(e)]})
