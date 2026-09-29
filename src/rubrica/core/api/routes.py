"""Core JSON/CSV API — the routes the acceptance checker calls, plus a few
siblings. Everything is scope-checked in the service layer; this file only
parses requests and formats responses.
"""
from __future__ import annotations

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .. import audit
from ..auth import require, require_staff
from ..errors import DomainError, Forbidden
from ..http import body_fields, current_actor, get_db
from ..services import community, export, integrity, judging, normalization, projects as projects_svc, results

router = APIRouter(prefix="/api", tags=["core"])


@router.get("/health")
def health(request: Request):
    db = get_db(request)
    n = db.one("SELECT COUNT(*) AS n FROM projects")["n"]
    return {"ok": True, "projects": n}


@router.get("/projects")
def api_projects(request: Request, q: str = "", track: str = "", event: str = ""):
    rows = projects_svc.gallery(get_db(request), q=q, track_id=track, event_id=event)
    return {"projects": rows, "count": len(rows)}


@router.get("/judge/scores")
def judge_scores(request: Request, judge: str | None = None):
    """A judge's own assignments and scores.

    ``?judge=`` is honoured only for organizers/admins. A judge asking for any
    id other than their own gets 403 — identity comes from the session.
    """
    db = get_db(request)
    actor = current_actor(request)
    rows = judging.judge_queue(db, actor, judge_id=judge)
    who = judge if (actor and actor.is_staff and judge) else actor.id
    return {"judge_id": who, "assignments": rows, "scored": sum(1 for r in rows if r["score_id"]),
            "count": len(rows)}


@router.post("/judge/scores/{project_id}")
async def post_score(request: Request, project_id: str):
    db = get_db(request)
    data = await body_fields(request)
    crit = data.get("criteria") if isinstance(data.get("criteria"), dict) else \
        {k: v for k, v in data.items() if k not in ("comment", "project_id")}
    out = judging.submit_score(db, current_actor(request), project_id, crit, data.get("comment", ""))
    return JSONResponse(out, status_code=201)


@router.get("/export.csv")
def export_csv(request: Request, event: str | None = None):
    db = get_db(request)
    actor = require_staff(current_actor(request))
    audit.record(db, actor.id, "export.scores_csv", "event", event, {})
    return Response(export.scores_csv(db, event), media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=scores.csv"})


@router.get("/export/results.csv")
def export_results_csv(request: Request, event: str):
    db = get_db(request)
    require_staff(current_actor(request))
    return Response(export.results_csv(db, event), media_type="text/csv")


@router.get("/export/projects.csv")
def export_projects_csv(request: Request, event: str | None = None):
    db = get_db(request)
    require_staff(current_actor(request))
    return Response(export.projects_csv(db, event), media_type="text/csv")


@router.get("/export/audit.csv")
def export_audit_csv(request: Request):
    db = get_db(request)
    require_staff(current_actor(request))
    return Response(export.audit_csv(db), media_type="text/csv")


@router.get("/events/{event_id}/progress")
def api_progress(request: Request, event_id: str):
    return judging.progress(get_db(request), current_actor(request), event_id)


@router.get("/events/{event_id}/alerts")
def api_alerts(request: Request, event_id: str):
    require_staff(current_actor(request))
    return {"alerts": integrity.alerts(get_db(request), event_id)}


@router.post("/events/{event_id}/normalize")
async def api_normalize(request: Request, event_id: str):
    db = get_db(request)
    data = await body_fields(request)
    k = float(data.get("k", 3))
    policy = data.get("zero_variance_policy", "baseline")
    run_id, res = normalization.run_and_store(db, current_actor(request), event_id, k=k, zero_variance_policy=policy)
    return {"run_id": run_id, **res.as_dict()}


@router.get("/events/{event_id}/results")
def api_results(request: Request, event_id: str):
    db = get_db(request)
    from ..services import events as events_svc
    event = events_svc.get_event(db, event_id)
    actor = current_actor(request)
    if not results.results_visible(db, event, actor):
        raise Forbidden("results are hidden until the voting window closes and results are published")
    snap = results.latest_snapshot(db, event_id)
    if not snap:
        raise DomainError("no published results", 404)
    return snap


@router.post("/events/{event_id}/publish")
def api_publish(request: Request, event_id: str):
    return results.publish(get_db(request), current_actor(request), event_id)


@router.get("/results/{snapshot_id}/verify")
def api_verify(request: Request, snapshot_id: str):
    return results.verify(get_db(request), snapshot_id)


@router.post("/projects/{project_id}/vote")
def api_vote(request: Request, project_id: str):
    client = request.client.host if request.client else ""
    return community.cast_vote(get_db(request), current_actor(request), project_id, client_key=client)


@router.delete("/projects/{project_id}/vote")
def api_unvote(request: Request, project_id: str):
    community.retract_vote(get_db(request), current_actor(request), project_id)
    return {"ok": True}


@router.post("/projects/{project_id}/comments")
async def api_comment(request: Request, project_id: str):
    data = await body_fields(request)
    return JSONResponse(community.add_comment(get_db(request), current_actor(request), project_id,
                                              data.get("body", "")), status_code=201)


@router.get("/projects/{project_id}/comments")
def api_comments(request: Request, project_id: str):
    return {"comments": community.comments(get_db(request), project_id)}


@router.get("/me")
def me(request: Request):
    actor = require(current_actor(request))
    return actor.__dict__
