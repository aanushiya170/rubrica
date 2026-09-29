"""T4 — versioned REST API with an OpenAPI document and offline docs page.

Mounted at ``/api/v1``. Thin adapters over core services; every scope rule is
enforced there, not here.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from ..core import auth
from ..core.errors import DomainError, Forbidden
from ..core.http import domain_error_handler, get_db
from ..core.services import (community, events as events_svc, export, integrity, judging, normalization,
                             projects as projects_svc, results, teams as teams_svc)
from . import _auth

SCHEMA = _auth.SCHEMA

TAGS = [
    {"name": "events", "description": "Events, tracks, prizes."},
    {"name": "projects", "description": "Gallery and submissions."},
    {"name": "judging", "description": "Assignments and scores — a judge only ever sees their own."},
    {"name": "results", "description": "Normalization runs, published snapshots and replay verification."},
    {"name": "community", "description": "Votes and comments (T3)."},
    {"name": "keys", "description": "API key management for the signed-in user."},
    {"name": "extensions", "description": "Webhooks, certificates, bulk import/export, pairwise (registered by their modules)."},
]

DESCRIPTION = """Rubrica public API. Authenticate with `Authorization: Bearer <api key>`
(create one at `POST /api/v1/keys` while signed in) or with the normal session cookie.

Role isolation is enforced server-side: a judge asking for another judge's scores gets **403**.
"""

v1 = FastAPI(title="Rubrica API", version="1.0.0", description=DESCRIPTION, openapi_tags=TAGS,
             docs_url=None, redoc_url=None, openapi_url="/openapi.json")
v1.add_exception_handler(DomainError, domain_error_handler)


class ProjectIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    summary: str = ""
    repo_url: str = ""
    demo_url: str = ""
    track_id: str | None = None
    event_id: str | None = None
    submit: bool = False


class ScoreIn(BaseModel):
    criteria: dict[str, float]
    comment: str = ""


class EventIn(BaseModel):
    name: str
    submissions_close: str
    submissions_open: str | None = None
    voting_open: str | None = None
    voting_close: str | None = None
    description: str = ""
    tracks: list[str] = []
    prizes: list[dict] = []
    rubric: list[dict] = []


class TeamIn(BaseModel):
    name: str


class RubricIn(BaseModel):
    criteria: list[dict]


class NormalizeIn(BaseModel):
    k: float = 3.0
    zero_variance_policy: str = "baseline"


class CommentIn(BaseModel):
    body: str


class KeyIn(BaseModel):
    label: str = ""
    scopes: str = "read"


def _actor(request: Request, required: bool = True):
    a = _auth.api_actor(request)
    return auth.require(a) if required else a


# --- events ------------------------------------------------------------------

@v1.get("/events", tags=["events"])
def list_events(request: Request):
    db = get_db(request)
    out = []
    for e in events_svc.list_events(db):
        e["phase"] = events_svc.phase(e)
        e["tracks"] = events_svc.tracks(db, e["id"])
        out.append(e)
    return {"events": out}


@v1.post("/events", tags=["events"], status_code=201)
def create_event(request: Request, body: EventIn):
    db = get_db(request)
    _auth.require_write(request)
    actor = _actor(request)
    e = events_svc.create_event(db, actor, body.name, body.submissions_close, body.submissions_open,
                                body.voting_open, body.voting_close, body.description, body.tracks, body.prizes)
    if body.rubric:
        judging.set_rubric(db, actor, e["id"], body.rubric)
    return e


@v1.get("/events/{event_id}", tags=["events"])
def get_event(request: Request, event_id: str):
    db = get_db(request)
    e = events_svc.get_event(db, event_id)
    return {**e, "phase": events_svc.phase(e), "tracks": events_svc.tracks(db, event_id),
            "prizes": events_svc.prizes(db, event_id), "rubric": judging.active_rubric(db, event_id)}


@v1.put("/events/{event_id}/rubric", tags=["events"])
def put_rubric(request: Request, event_id: str, body: RubricIn):
    _auth.require_write(request)
    return judging.set_rubric(get_db(request), _actor(request), event_id, body.criteria)


@v1.post("/events/{event_id}/teams", tags=["events"], status_code=201)
def create_team(request: Request, event_id: str, body: TeamIn):
    _auth.require_write(request)
    return teams_svc.create_team(get_db(request), _actor(request), event_id, body.name)


@v1.get("/events/{event_id}/progress", tags=["judging"])
def progress(request: Request, event_id: str):
    return judging.progress(get_db(request), _actor(request), event_id)


@v1.get("/events/{event_id}/alerts", tags=["judging"])
def alerts(request: Request, event_id: str):
    auth.require_staff(_actor(request))
    return {"alerts": integrity.alerts(get_db(request), event_id)}


@v1.post("/events/{event_id}/assign", tags=["judging"])
def assign(request: Request, event_id: str, reviews_per_project: int = 3, anchor_count: int = 2):
    _auth.require_write(request)
    return judging.assign(get_db(request), _actor(request), event_id, reviews_per_project, anchor_count)


# --- projects -------------------------------------------------------------------

@v1.get("/projects", tags=["projects"])
def list_projects(request: Request, q: str = "", track: str = "", event: str = ""):
    rows = projects_svc.gallery(get_db(request), q=q, track_id=track, event_id=event)
    return {"projects": rows, "count": len(rows)}


@v1.post("/projects", tags=["projects"], status_code=201,
         responses={403: {"description": "Submissions closed for the event"}})
def create_project(request: Request, body: ProjectIn):
    _auth.require_write(request)
    return projects_svc.create_project(get_db(request), _actor(request), body.title, body.summary, body.repo_url,
                                       body.demo_url, body.track_id, body.event_id, body.submit)


@v1.get("/projects/{project_id}", tags=["projects"])
def get_project(request: Request, project_id: str):
    db = get_db(request)
    p = projects_svc.get_project(db, project_id)
    actor = _actor(request, required=False)
    if p["status"] == "draft" and not (actor and (actor.is_staff or teams_svc.is_member(db, actor.id, p["team_id"]))):
        raise DomainError("project not found", 404)
    return p


@v1.patch("/projects/{project_id}", tags=["projects"])
def patch_project(request: Request, project_id: str, body: dict):
    _auth.require_write(request)
    db = get_db(request)
    actor = _actor(request)
    p = projects_svc.update_project(db, actor, project_id, **{k: body.get(k) for k in
                                    ("title", "summary", "repo_url", "demo_url", "track_id")})
    if body.get("submit") is True:
        p = projects_svc.submit_project(db, actor, project_id)
    return p


# --- judging ---------------------------------------------------------------------

@v1.get("/judge/scores", tags=["judging"], responses={403: {"description": "Not your scores"}})
def judge_scores(request: Request, judge: str | None = None):
    rows = judging.judge_queue(get_db(request), _actor(request), judge_id=judge)
    return {"assignments": rows, "count": len(rows)}


@v1.put("/judge/scores/{project_id}", tags=["judging"], status_code=201)
def put_score(request: Request, project_id: str, body: ScoreIn):
    _auth.require_write(request)
    return judging.submit_score(get_db(request), _actor(request), project_id, body.criteria, body.comment)


# --- results -----------------------------------------------------------------------

@v1.post("/events/{event_id}/normalize", tags=["results"])
def normalize(request: Request, event_id: str, body: NormalizeIn | None = None):
    _auth.require_write(request)
    body = body or NormalizeIn()
    run_id, res = normalization.run_and_store(get_db(request), _actor(request), event_id, body.k, body.zero_variance_policy)
    return {"run_id": run_id, **res.as_dict()}


@v1.get("/events/{event_id}/normalization", tags=["results"])
def latest_normalization(request: Request, event_id: str):
    auth.require_staff(_actor(request))
    run = normalization.latest_run(get_db(request), event_id)
    if not run:
        raise DomainError("no normalization run yet", 404)
    return run


@v1.post("/events/{event_id}/publish", tags=["results"])
def publish(request: Request, event_id: str):
    _auth.require_write(request)
    return results.publish(get_db(request), _actor(request), event_id)


@v1.get("/events/{event_id}/results", tags=["results"],
        responses={403: {"description": "Hidden during the voting window / until published"}})
def get_results(request: Request, event_id: str):
    db = get_db(request)
    event = events_svc.get_event(db, event_id)
    if not results.results_visible(db, event, _actor(request, required=False)):
        raise Forbidden("results are hidden until the voting window closes and results are published")
    snap = results.latest_snapshot(db, event_id)
    if not snap:
        raise DomainError("no published results", 404)
    return snap


@v1.get("/results/{snapshot_id}/verify", tags=["results"])
def verify(request: Request, snapshot_id: str):
    return results.verify(get_db(request), snapshot_id)


@v1.get("/export/scores.csv", tags=["results"])
def scores_csv(request: Request, event: str | None = None):
    auth.require_staff(_actor(request))
    return Response(export.scores_csv(get_db(request), event), media_type="text/csv")


@v1.get("/export/results.csv", tags=["results"])
def results_csv(request: Request, event: str):
    auth.require_staff(_actor(request))
    return Response(export.results_csv(get_db(request), event), media_type="text/csv")


# --- community -----------------------------------------------------------------------

@v1.get("/events/{event_id}/ballot", tags=["community"])
def ballot(request: Request, event_id: str):
    return {"projects": community.ballot(get_db(request), event_id, _actor(request, required=False))}


@v1.post("/projects/{project_id}/vote", tags=["community"], status_code=201,
         responses={409: {"description": "Duplicate vote or quota used"}, 429: {"description": "Rate limited"}})
def vote(request: Request, project_id: str):
    _auth.require_write(request)
    client = request.client.host if request.client else ""
    return community.cast_vote(get_db(request), _actor(request), project_id, client_key=client)


@v1.delete("/projects/{project_id}/vote", tags=["community"])
def unvote(request: Request, project_id: str):
    _auth.require_write(request)
    community.retract_vote(get_db(request), _actor(request), project_id)
    return {"ok": True}


@v1.get("/projects/{project_id}/comments", tags=["community"])
def comments(request: Request, project_id: str):
    return {"comments": community.comments(get_db(request), project_id)}


@v1.post("/projects/{project_id}/comments", tags=["community"], status_code=201)
def comment(request: Request, project_id: str, body: CommentIn):
    _auth.require_write(request)
    return community.add_comment(get_db(request), _actor(request), project_id, body.body)


# --- keys --------------------------------------------------------------------------------

@v1.get("/keys", tags=["keys"])
def keys(request: Request):
    actor = auth.require(_auth.api_actor(request))
    return {"keys": _auth.list_keys(get_db(request), actor.id)}


@v1.post("/keys", tags=["keys"], status_code=201)
def make_key(request: Request, body: KeyIn):
    actor = auth.require(_auth.api_actor(request))
    if "write" in body.scopes and actor.role == "participant":
        pass  # participants may hold write keys for their own submissions
    return _auth.create_key(get_db(request), actor.id, body.label, body.scopes)


@v1.delete("/keys/{key_id}", tags=["keys"])
def drop_key(request: Request, key_id: str):
    actor = auth.require(_auth.api_actor(request))
    _auth.revoke_key(get_db(request), actor.id, key_id)
    return {"ok": True}


@v1.get("/me", tags=["keys"])
def me(request: Request):
    return _actor(request).__dict__


# --- offline docs ------------------------------------------------------------------------

DOCS_TEMPLATE = (Path(__file__).parent / "docs.html").read_text() if (Path(__file__).parent / "docs.html").exists() else ""


@v1.get("/docs", include_in_schema=False, response_class=HTMLResponse)
def docs():
    """Self-contained docs page (no CDN, works with the network off)."""
    spec = v1.openapi()
    return HTMLResponse(DOCS_TEMPLATE.replace("__SPEC__", json.dumps(spec).replace("</", "<\\/")))


def register(app: FastAPI) -> None:
    v1.state.db = app.state.db
    app.mount("/api/v1", v1)
