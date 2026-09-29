"""Organizer control room."""
from __future__ import annotations

import json

from fastapi import APIRouter, Request

from .. import audit, auth
from ..errors import DomainError
from ..http import body_fields, current_actor, get_db, redirect, render
from ..services import community, events as events_svc, integrity, judging, normalization, projects as projects_svc, results

router = APIRouter(prefix="/organizer")


def _staff(request: Request):
    return auth.require_staff(current_actor(request))


@router.get("")
def organizer_home(request: Request):
    db = get_db(request)
    _staff(request)
    evs = events_svc.list_events(db)
    for e in evs:
        e["phase"] = events_svc.phase(e)
        e["alerts"] = len([a for a in integrity.alerts(db, e["id"]) if a["severity"] == "warn"])
    return render(request, "org_home.html", {"events": evs})


@router.get("/events/new")
def new_event_form(request: Request):
    _staff(request)
    return render(request, "org_event_form.html", {})


@router.post("/events/new")
async def new_event(request: Request):
    db = get_db(request)
    actor = _staff(request)
    d = await body_fields(request)
    tracks = [t.strip() for t in str(d.get("tracks", "")).splitlines() if t.strip()]
    prizes = []
    for i, line in enumerate(str(d.get("prizes", "")).splitlines(), 1):
        if line.strip():
            title, _, amount = line.partition("|")
            prizes.append({"rank": i, "title": title.strip(), "amount": amount.strip()})
    e = events_svc.create_event(db, actor, str(d.get("name", "")), str(d.get("submissions_close", "")),
                                submissions_open=d.get("submissions_open") or None,
                                voting_open=d.get("voting_open") or None, voting_close=d.get("voting_close") or None,
                                description=str(d.get("description", "")), track_names=tracks, prize_rows=prizes)
    crits = []
    for line in str(d.get("rubric", "")).splitlines():
        if line.strip():
            name, _, w = line.partition("|")
            crits.append({"key": name.strip().lower().replace(" ", "_"), "name": name.strip(), "weight": float(w or 0)})
    if crits:
        judging.set_rubric(db, actor, e["id"], crits)
    return redirect(f"/organizer/events/{e['id']}", "Event created.")


@router.get("/events/{event_id}")
def control_room(request: Request, event_id: str):
    db = get_db(request)
    actor = _staff(request)
    e = events_svc.get_event(db, event_id)
    run = normalization.latest_run(db, event_id)
    snap = results.latest_snapshot(db, event_id)
    return render(request, "org_event.html", {
        "e": e, "alerts": integrity.alerts(db, event_id), "progress": judging.progress(db, actor, event_id),
        "rubric": judging.active_rubric(db, event_id), "judges": judging.event_judges(db, event_id),
        "tracks": events_svc.tracks(db, event_id), "run": run, "snap": snap,
        "tally": community.tally(db, event_id), "audit": audit.recent(db, 40),
        "phase": events_svc.phase(e), "voting": events_svc.voting_open(e),
        "projects": projects_svc.gallery(db, event_id=event_id, include_drafts=True),
    })


@router.post("/events/{event_id}/dates")
async def update_dates(request: Request, event_id: str):
    db = get_db(request)
    d = await body_fields(request)
    events_svc.update_event(db, _staff(request), event_id, **{k: (d.get(k) or None) for k in
                            ("name", "submissions_open", "submissions_close", "voting_open", "voting_close")})
    return redirect(f"/organizer/events/{event_id}", "Dates updated.")


@router.post("/events/{event_id}/rubric")
async def update_rubric(request: Request, event_id: str):
    db = get_db(request)
    d = await body_fields(request)
    crits = []
    for line in str(d.get("rubric", "")).splitlines():
        if line.strip():
            name, _, w = line.partition("|")
            crits.append({"key": name.strip().lower().replace(" ", "_"), "name": name.strip(), "weight": float(w or 0)})
    judging.set_rubric(db, _staff(request), event_id, crits)
    return redirect(f"/organizer/events/{event_id}", "New rubric version is active. Old scores keep their version.")


@router.post("/events/{event_id}/invite-judge")
async def invite_judge(request: Request, event_id: str):
    db = get_db(request)
    d = await body_fields(request)
    tracks = [t for t in str(d.get("track_ids", "")).replace(",", " ").split() if t]
    inv = judging.invite_judge(db, _staff(request), event_id, str(d.get("email", "")), tracks)
    link = f"{str(request.base_url).rstrip('/')}/judge/invite/{inv['token']}"
    return redirect(f"/organizer/events/{event_id}", f"Invite link for {inv['email']}: {link}")


@router.post("/events/{event_id}/assign")
async def run_assign(request: Request, event_id: str):
    db = get_db(request)
    d = await body_fields(request)
    out = judging.assign(db, _staff(request), event_id, reviews_per_project=int(d.get("reviews", 3)),
                         anchor_count=int(d.get("anchors", 2)))
    return redirect(f"/organizer/events/{event_id}", f"Assignments: {out['created']} created "
                    f"({out['anchor_assignments']} anchor reviews) across {out['judges']} judges.")


@router.post("/events/{event_id}/normalize")
async def run_normalize(request: Request, event_id: str):
    db = get_db(request)
    d = await body_fields(request)
    run_id, _ = normalization.run_and_store(db, _staff(request), event_id, k=float(d.get("k", 3)),
                                            zero_variance_policy=str(d.get("policy", "baseline")))
    return redirect(f"/organizer/events/{event_id}#normalization", f"Normalization run {run_id} stored.")


@router.post("/events/{event_id}/publish")
def publish(request: Request, event_id: str):
    snap = results.publish(get_db(request), _staff(request), event_id)
    return redirect(f"/organizer/events/{event_id}#results", f"Published snapshot {snap['id']}.")


@router.get("/results/{snapshot_id}/verify")
def verify(request: Request, snapshot_id: str):
    db = get_db(request)
    _staff(request)
    v = results.verify(db, snapshot_id)
    snap = results.get_snapshot(db, snapshot_id)
    return render(request, "org_verify.html", {"v": v, "snap": snap})


@router.post("/projects/{project_id}/duplicate")
async def resolve_duplicate(request: Request, project_id: str):
    db = get_db(request)
    d = await body_fields(request)
    p = projects_svc.mark_duplicate(db, _staff(request), project_id, str(d.get("canonical_id", "")),
                                    str(d.get("action", "")))
    return redirect(f"/organizer/events/{p['event_id']}", f"{project_id}: {d.get('action')} recorded.")


@router.post("/comments/{comment_id}/hide")
def hide_comment(request: Request, comment_id: str):
    community.hide_comment(get_db(request), _staff(request), comment_id)
    return redirect(request.headers.get("referer") or "/organizer", "Comment hidden.")


@router.get("/audit")
def audit_page(request: Request, action: str = ""):
    db = get_db(request)
    _staff(request)
    return render(request, "org_audit.html", {"events": audit.recent(db, 500, action or None), "action": action})
