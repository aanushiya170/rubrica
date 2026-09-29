"""Judge pages. Every data access goes through scope-checked services."""
from __future__ import annotations

from fastapi import APIRouter, Request

from .. import auth
from ..errors import Forbidden
from ..http import body_fields, current_actor, get_db, redirect, render
from ..services import events as events_svc, judging, projects as projects_svc

router = APIRouter(prefix="/judge")


@router.get("")
def judge_home(request: Request):
    db = get_db(request)
    actor = auth.require_role(current_actor(request), "judge", "organizer", "admin")
    queue = judging.judge_queue(db, actor) if actor.is_judge else []
    by_event: dict[str, list] = {}
    for q in queue:
        by_event.setdefault(q["event_id"], []).append(q)
    events = {eid: events_svc.get_event(db, eid) for eid in by_event}
    return render(request, "judge_home.html", {"by_event": by_event, "events": events})


@router.get("/score/{project_id}")
def score_form(request: Request, project_id: str):
    db = get_db(request)
    actor = auth.assert_judge_scope(db, current_actor(request), project_id=project_id)
    p = projects_svc.get_project(db, project_id)
    rubric = judging.active_rubric(db, p["event_id"])
    mine = next((q for q in judging.judge_queue(db, actor) if q["project_id"] == project_id), None)
    return render(request, "score_form.html", {"p": p, "rubric": rubric, "mine": mine})


@router.post("/score/{project_id}")
async def score_submit(request: Request, project_id: str):
    db = get_db(request)
    data = await body_fields(request)
    crit = {k[5:]: v for k, v in data.items() if k.startswith("crit_")}
    judging.submit_score(db, current_actor(request), project_id, crit, str(data.get("comment", "")))
    return redirect("/judge", "Score saved. Only you and the organizers can see it.")


@router.get("/invite/{token}")
def accept_invite(request: Request, token: str):
    db = get_db(request)
    actor = current_actor(request)
    if not actor:
        return redirect(f"/register?next=/judge/invite/{token}", "Create an account to accept the judge invite.")
    judging.accept_judge_invite(db, actor, token)
    return redirect("/judge", "You are now a judge for this event.")
