"""Cross-judge normalization.

Method: **sample-size-shrunk location-scale normalization** — a heuristic
*inspired by* empirical-Bayes shrinkage. It is not a full Bayesian posterior
and we do not call it one. See JUDGING.md for the derivation, the worked
fixture table and the stated limitations.

    μ_j* = (n_j·μ_j + k·μ_g) / (n_j + k)
    σ_j* = sqrt((n_j·v_j + k·v_g) / (n_j + k))
    z    = (S_jp − μ_j*) / σ_j*
    norm = clip(μ_g + z·σ_g, 1, 5)

Variances are population variances (divide by n). ``k`` is the prior
strength, default 3. A zero-variance judge (identical scores everywhere) is
handled by ``zero_variance_policy``:

* ``"baseline"`` (default): their scores map to μ_g. They contributed no
  relative information, so they do not move any project.
* ``"shrink"``: fall through to the formula; σ_j* is still > 0 because of the
  k·v_g term, and all their scores map to one identical value anyway.

Everything is pure Python (statistics module) so it can be re-run verbatim by
a reader and by the result-verification replay.
"""
from __future__ import annotations

import json
import math
from collections import defaultdict
from dataclasses import asdict, dataclass, field

from ..auth import Actor, require_staff
from ..db import Database
from ..util import iso, new_id
from .. import audit

METHOD = "shrunk-location-scale-v1"


def _mean(xs):
    return sum(xs) / len(xs)


def _pvar(xs, m):
    return sum((x - m) ** 2 for x in xs) / len(xs)


@dataclass
class JudgeStats:
    judge_id: str
    n: int
    raw_mean: float
    raw_var: float
    shrunk_mean: float
    shrunk_sd: float
    zero_variance: bool
    low_sample: bool


@dataclass
class NormalizedScore:
    score_id: str
    judge_id: str
    project_id: str
    raw: float
    normalized: float


@dataclass
class ProjectResult:
    project_id: str
    review_count: int
    raw_avg: float
    normalized: float
    rank_raw: int = 0
    rank_norm: int = 0


@dataclass
class Normalization:
    method: str
    params: dict
    global_mean: float
    global_sd: float
    judges: list[JudgeStats]
    scores: list[NormalizedScore]
    projects: list[ProjectResult]
    excluded_project_ids: list[str] = field(default_factory=list)

    def as_dict(self):
        return asdict(self)


def normalize(score_rows: list[dict], k: float = 3.0, zero_variance_policy: str = "baseline",
              low_sample_threshold: int = 2, excluded_project_ids: set[str] | None = None) -> Normalization:
    """``score_rows``: dicts with id, judge_id, project_id, weighted_raw."""
    excluded = set(excluded_project_ids or ())
    rows = [r for r in score_rows if r["project_id"] not in excluded]
    if not rows:
        return Normalization(METHOD, {"k": k, "zero_variance_policy": zero_variance_policy},
                             0.0, 0.0, [], [], [], sorted(excluded))
    all_vals = [float(r["weighted_raw"]) for r in rows]
    mu_g = _mean(all_vals)
    v_g = _pvar(all_vals, mu_g)
    sd_g = math.sqrt(v_g)

    by_judge: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_judge[r["judge_id"]].append(r)

    judge_stats: dict[str, JudgeStats] = {}
    for jid, jrows in sorted(by_judge.items()):
        vals = [float(r["weighted_raw"]) for r in jrows]
        n = len(vals)
        mu_j = _mean(vals)
        v_j = _pvar(vals, mu_j)
        mu_s = (n * mu_j + k * mu_g) / (n + k)
        sd_s = math.sqrt((n * v_j + k * v_g) / (n + k)) if (n + k) else sd_g
        judge_stats[jid] = JudgeStats(jid, n, mu_j, v_j, mu_s, sd_s,
                                      zero_variance=(n > 1 and v_j == 0.0),
                                      low_sample=(n < low_sample_threshold))

    out_scores: list[NormalizedScore] = []
    for r in rows:
        js = judge_stats[r["judge_id"]]
        raw = float(r["weighted_raw"])
        if js.zero_variance and zero_variance_policy == "baseline":
            norm = mu_g
        elif js.shrunk_sd == 0 or sd_g == 0:
            norm = mu_g
        else:
            z = (raw - js.shrunk_mean) / js.shrunk_sd
            norm = mu_g + z * sd_g
        norm = min(5.0, max(1.0, norm))
        out_scores.append(NormalizedScore(r["id"], r["judge_id"], r["project_id"], raw, round(norm, 6)))

    by_project: dict[str, list[NormalizedScore]] = defaultdict(list)
    for s in out_scores:
        by_project[s.project_id].append(s)
    projects = [ProjectResult(pid, len(ss), round(_mean([s.raw for s in ss]), 6),
                              round(_mean([s.normalized for s in ss]), 6))
                for pid, ss in sorted(by_project.items())]
    for key, attr in (("raw_avg", "rank_raw"), ("normalized", "rank_norm")):
        ordered = sorted(projects, key=lambda p: (-getattr(p, key), p.project_id))
        for i, p in enumerate(ordered, 1):
            setattr(p, attr, i)
    return Normalization(METHOD, {"k": k, "zero_variance_policy": zero_variance_policy,
                                  "low_sample_threshold": low_sample_threshold},
                         round(mu_g, 6), round(sd_g, 6), list(judge_stats.values()), out_scores,
                         projects, sorted(excluded))


# --- persistence ---------------------------------------------------------------

def score_rows_for_event(db: Database, event_id: str) -> list[dict]:
    return [dict(r) for r in db.all(
        "SELECT id, judge_id, project_id, weighted_raw FROM scores WHERE event_id = ? ORDER BY id", (event_id,))]


def excluded_projects(db: Database, event_id: str) -> set[str]:
    return {r["id"] for r in db.all(
        "SELECT id FROM projects WHERE event_id = ? AND status = 'excluded'", (event_id,))}


def run_and_store(db: Database, actor: Actor, event_id: str, k: float = 3.0,
                  zero_variance_policy: str = "baseline") -> tuple[str, Normalization]:
    require_staff(actor)
    result = normalize(score_rows_for_event(db, event_id), k=k, zero_variance_policy=zero_variance_policy,
                       excluded_project_ids=excluded_projects(db, event_id))
    run_id = new_id("nrm")
    stats = {"global_mean": result.global_mean, "global_sd": result.global_sd,
             "judges": [asdict(j) for j in result.judges], "excluded": result.excluded_project_ids}
    with db.tx() as c:
        c.execute("INSERT INTO normalization_runs(id, event_id, method, params_json, stats_json, created_by,"
                  " created_at) VALUES (?,?,?,?,?,?,?)",
                  (run_id, event_id, result.method, json.dumps(result.params, sort_keys=True),
                   json.dumps(stats, sort_keys=True), actor.id, iso()))
        for p in result.projects:
            c.execute("INSERT INTO normalization_results(run_id, project_id, review_count, raw_avg, normalized,"
                      " rank_raw, rank_norm) VALUES (?,?,?,?,?,?,?)",
                      (run_id, p.project_id, p.review_count, p.raw_avg, p.normalized, p.rank_raw, p.rank_norm))
    audit.record(db, actor.id, "normalization.run", "normalization_run", run_id,
                 {"event_id": event_id, "k": k, "policy": zero_variance_policy, "scores": len(result.scores)})
    return run_id, result


def latest_run(db: Database, event_id: str) -> dict | None:
    r = db.one("SELECT * FROM normalization_runs WHERE event_id = ? ORDER BY created_at DESC, id DESC LIMIT 1",
               (event_id,))
    return load_run(db, r["id"]) if r else None


def load_run(db: Database, run_id: str) -> dict | None:
    r = db.one("SELECT * FROM normalization_runs WHERE id = ?", (run_id,))
    if not r:
        return None
    run = dict(r)
    run["params"] = json.loads(run.pop("params_json"))
    run["stats"] = json.loads(run.pop("stats_json"))
    run["results"] = [dict(x) for x in db.all(
        "SELECT nr.*, p.title, t.name AS team_name FROM normalization_results nr"
        " JOIN projects p ON p.id = nr.project_id JOIN teams t ON t.id = p.team_id"
        " WHERE run_id = ? ORDER BY rank_norm", (run_id,))]
    return run
