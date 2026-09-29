"""Publishing and result replay.

On publish we freeze the *inputs* (score ids, project ids, rubric version,
normalization params) and the *outputs*. ``verify`` recomputes from the stored
inputs and compares — an organizer can prove a published table was derived
from the recorded evidence. A SHA-256 of the inputs is stored for tamper
detection; there is deliberately no hash chain (see ARCHITECTURE.md).
"""
from __future__ import annotations

import json

from ..auth import Actor, require_staff
from ..db import Database
from ..errors import DomainError, NotFound
from ..util import iso, new_id, sha256_json
from .. import audit
from . import normalization as norm_svc
from . import events as events_svc


def publish(db: Database, actor: Actor, event_id: str, run_id: str | None = None) -> dict:
    require_staff(actor)
    run = norm_svc.load_run(db, run_id) if run_id else norm_svc.latest_run(db, event_id)
    if not run:
        raise DomainError("run normalization before publishing", 400)
    rubric = db.one("SELECT version FROM rubrics WHERE event_id = ? AND active = 1", (event_id,))
    score_ids = [r["id"] for r in db.all("SELECT id FROM scores WHERE event_id = ? ORDER BY id", (event_id,))]
    project_ids = [r["project_id"] for r in run["results"]]
    inputs = {"score_ids": score_ids, "project_ids": sorted(project_ids), "params": run["params"],
              "method": run["method"], "excluded": run["stats"].get("excluded", [])}
    results = [{k: r[k] for k in ("project_id", "review_count", "raw_avg", "normalized", "rank_raw", "rank_norm")}
               for r in run["results"]]
    sid = new_id("res")
    with db.tx() as c:
        c.execute("INSERT INTO result_snapshots(id, event_id, normalization_run_id, rubric_version, inputs_json,"
                  " inputs_sha256, results_json, published_by, published_at) VALUES (?,?,?,?,?,?,?,?,?)",
                  (sid, event_id, run["id"], rubric["version"] if rubric else 0,
                   json.dumps(inputs, sort_keys=True), sha256_json(inputs),
                   json.dumps(results, sort_keys=True), actor.id, iso()))
        c.execute("UPDATE events SET results_published = 1 WHERE id = ?", (event_id,))
    audit.record(db, actor.id, "results.published", "result_snapshot", sid,
                 {"event_id": event_id, "run_id": run["id"], "inputs_sha256": sha256_json(inputs)})
    return get_snapshot(db, sid)


def get_snapshot(db: Database, snapshot_id: str) -> dict:
    r = db.one("SELECT * FROM result_snapshots WHERE id = ?", (snapshot_id,))
    if not r:
        raise NotFound("snapshot not found")
    s = dict(r)
    s["inputs"] = json.loads(s.pop("inputs_json"))
    s["results"] = json.loads(s.pop("results_json"))
    return s


def latest_snapshot(db: Database, event_id: str) -> dict | None:
    r = db.one("SELECT id FROM result_snapshots WHERE event_id = ? ORDER BY published_at DESC, id DESC LIMIT 1",
               (event_id,))
    return get_snapshot(db, r["id"]) if r else None


def verify(db: Database, snapshot_id: str) -> dict:
    """Recompute from the snapshot's recorded inputs and diff against its outputs."""
    snap = get_snapshot(db, snapshot_id)
    inputs = snap["inputs"]
    hash_ok = sha256_json(inputs) == snap["inputs_sha256"]
    q = ",".join("?" * len(inputs["score_ids"])) or "''"
    rows = [dict(r) for r in db.all(
        f"SELECT id, judge_id, project_id, weighted_raw FROM scores WHERE id IN ({q}) ORDER BY id",
        inputs["score_ids"])]
    missing = sorted(set(inputs["score_ids"]) - {r["id"] for r in rows})
    result = norm_svc.normalize(rows, k=inputs["params"]["k"],
                                zero_variance_policy=inputs["params"]["zero_variance_policy"],
                                excluded_project_ids=set(inputs.get("excluded", [])))
    recomputed = {p.project_id: p for p in result.projects}
    diffs = []
    for r in snap["results"]:
        p = recomputed.get(r["project_id"])
        if not p:
            diffs.append({"project_id": r["project_id"], "problem": "missing after recompute"})
            continue
        for k in ("review_count", "raw_avg", "normalized", "rank_raw", "rank_norm"):
            a, b = r[k], getattr(p, k)
            if isinstance(a, float) or isinstance(b, float):
                same = abs(float(a) - float(b)) < 1e-6
            else:
                same = a == b
            if not same:
                diffs.append({"project_id": r["project_id"], "field": k, "published": a, "recomputed": b})
    extra = sorted(set(recomputed) - {r["project_id"] for r in snap["results"]})
    ok = hash_ok and not missing and not diffs and not extra
    return {"snapshot_id": snapshot_id, "ok": ok, "inputs_hash_ok": hash_ok, "missing_scores": missing,
            "extra_projects": extra, "differences": diffs, "scores_checked": len(rows),
            "projects_checked": len(snap["results"])}


def results_visible(db: Database, event: dict, actor: Actor | None) -> bool:
    """Hidden from everyone but staff while the voting window is open or unpublished."""
    if actor and actor.is_staff:
        return True
    if events_svc.voting_open(event):
        return False
    return bool(event.get("results_published"))
