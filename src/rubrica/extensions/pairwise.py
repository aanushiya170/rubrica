"""T4 bonus — pairwise judging with a Bradley–Terry estimator.

Judges are shown two projects from their assignments and pick the stronger
one. Strengths are fit with the MM algorithm (Hunter, 2004) on the win matrix
with a small symmetric pseudo-count so every project has a finite estimate
even with sparse comparisons. Output is a ranking with log-strengths and the
number of comparisons per project, so thin evidence is visible.

    p(i beats j) = π_i / (π_i + π_j)
    MM update:    π_i ← W_i / Σ_{j≠i} n_ij / (π_i + π_j)

Everything is pure Python and the fit is re-runnable from the stored
comparison rows.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict

from fastapi import APIRouter, FastAPI, Request
from pydantic import BaseModel

from ..core import audit, auth
from ..core.auth import assert_judge_scope
from ..core.errors import DomainError, Forbidden
from ..core.http import body_fields, current_actor, get_db, redirect, render
from ..core.services import events as events_svc, judging
from ..core.util import iso, new_id
from . import _auth

SCHEMA = """
CREATE TABLE IF NOT EXISTS pairwise_comparisons (
  id         TEXT PRIMARY KEY,
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  judge_id   TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  project_a  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  project_b  TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  winner     TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pairwise_event ON pairwise_comparisons(event_id);
"""


def bradley_terry(comparisons: list[tuple[str, str]], pseudo: float = 0.5, iters: int = 500, tol: float = 1e-9) -> dict:
    """``comparisons``: (winner, loser) pairs. Returns per-item strength, log-strength, wins, games."""
    items = sorted({x for pair in comparisons for x in pair})
    if not items:
        return {}
    idx = {x: i for i, x in enumerate(items)}
    n = len(items)
    wins = [[0.0] * n for _ in range(n)]  # wins[i][j] = times i beat j
    for w, l in comparisons:
        wins[idx[w]][idx[l]] += 1
    # symmetric pseudo-count: every pair has `pseudo` wins each way → finite MLE
    for i in range(n):
        for j in range(n):
            if i != j:
                wins[i][j] += pseudo
    W = [sum(wins[i]) for i in range(n)]
    games = [[wins[i][j] + wins[j][i] for j in range(n)] for i in range(n)]
    pi = [1.0] * n
    for _ in range(iters):
        new = []
        for i in range(n):
            denom = sum(games[i][j] / (pi[i] + pi[j]) for j in range(n) if j != i)
            new.append(W[i] / denom if denom else pi[i])
        g = math.exp(sum(math.log(x) for x in new) / n)  # normalise geometric mean to 1
        new = [x / g for x in new]
        delta = max(abs(a - b) for a, b in zip(new, pi))
        pi = new
        if delta < tol:
            break
    real_wins = defaultdict(int)
    real_games = defaultdict(int)
    for w, l in comparisons:
        real_wins[w] += 1
        real_games[w] += 1
        real_games[l] += 1
    return {x: {"strength": round(pi[idx[x]], 6), "log_strength": round(math.log(pi[idx[x]]), 6),
                "wins": real_wins[x], "comparisons": real_games[x]} for x in items}


def fit_event(db, event_id: str) -> list[dict]:
    rows = db.all("SELECT project_a, project_b, winner FROM pairwise_comparisons WHERE event_id = ?", (event_id,))
    pairs = [(r["winner"], r["project_b"] if r["winner"] == r["project_a"] else r["project_a"]) for r in rows]
    fit = bradley_terry(pairs)
    titles = {r["id"]: (r["title"], r["team"]) for r in db.all(
        "SELECT p.id, p.title, t.name AS team FROM projects p JOIN teams t ON t.id = p.team_id WHERE p.event_id = ?", (event_id,))}
    out = [{"project_id": pid, "title": titles.get(pid, ("?", "?"))[0], "team": titles.get(pid, ("?", "?"))[1], **v}
           for pid, v in fit.items()]
    out.sort(key=lambda r: (-r["strength"], r["project_id"]))
    for i, r in enumerate(out, 1):
        r["rank"] = i
    return out


def next_pair(db, judge_id: str, event_id: str) -> tuple[dict, dict] | None:
    """Least-compared pair among this judge's assigned projects, ties broken randomly."""
    mine = [dict(r) for r in db.all(
        "SELECT p.id, p.title, p.summary, t.name AS team_name FROM assignments a JOIN projects p ON p.id = a.project_id"
        " JOIN teams t ON t.id = p.team_id WHERE a.judge_id = ? AND a.event_id = ? AND p.status = 'submitted' ORDER BY p.id",
        (judge_id, event_id))]
    if len(mine) < 2:
        return None
    seen = defaultdict(int)
    for r in db.all("SELECT project_a, project_b FROM pairwise_comparisons WHERE judge_id = ? AND event_id = ?", (judge_id, event_id)):
        seen[frozenset((r["project_a"], r["project_b"]))] += 1
    pairs = [(a, b) for i, a in enumerate(mine) for b in mine[i + 1:]]
    random.shuffle(pairs)
    pairs.sort(key=lambda ab: seen[frozenset((ab[0]["id"], ab[1]["id"]))])
    a, b = pairs[0]
    return (a, b) if random.random() < 0.5 else (b, a)


def record(db, actor, event_id: str, project_a: str, project_b: str, winner: str) -> dict:
    actor = assert_judge_scope(db, actor, project_id=project_a)
    assert_judge_scope(db, actor, project_id=project_b)
    if winner not in (project_a, project_b) or project_a == project_b:
        raise DomainError("winner must be one of the two projects", 400)
    cid = new_id("cmp")
    db.run("INSERT INTO pairwise_comparisons(id, event_id, judge_id, project_a, project_b, winner, created_at) VALUES (?,?,?,?,?,?,?)",
           (cid, event_id, actor.id, project_a, project_b, winner, iso()))
    audit.record(db, actor.id, "pairwise.compared", "project", winner, {"against": project_b if winner == project_a else project_a})
    return {"id": cid, "winner": winner}


class CompareIn(BaseModel):
    project_a: str
    project_b: str
    winner: str


PAGE = """{% extends "base.html" %}{% block title %}Pairwise · {{ e.name }}{% endblock %}{% block content %}
<p class="small"><a href="/judge">← My judging</a></p><h1>Which is stronger? <span class="pill">{{ e.name }}</span></h1>
<p class="small muted">You have made {{ done }} comparisons. Pick the project you would rank higher; the organizer fits a Bradley–Terry model on everyone's picks. You never see other judges' choices.</p>
{% if pair %}<div class="grid">{% for p in pair %}<form method="post" class="card"><input type="hidden" name="project_a" value="{{ pair[0].id }}"><input type="hidden" name="project_b" value="{{ pair[1].id }}"><input type="hidden" name="winner" value="{{ p.id }}">
<h3 style="margin-top:0"><a href="/projects/{{ p.id }}" target="_blank">{{ p.title }}</a></h3><p>{{ p.summary }}</p><p class="small muted">{{ p.team_name }}</p><button>This one</button></form>{% endfor %}</div>
{% else %}<div class="card">You need at least two assigned projects in this event to compare.</div>{% endif %}{% endblock %}"""

ORG_PAGE = """{% extends "base.html" %}{% block title %}Pairwise results · {{ e.name }}{% endblock %}{% block content %}
<p class="small"><a href="/organizer/events/{{ e.id }}">← Control room</a></p><h1>Pairwise ranking (Bradley–Terry) — {{ e.name }}</h1>
<p class="small muted">{{ total }} comparisons from {{ judges }} judges. MM fit with 0.5 pseudo-wins per pair; strengths normalised to geometric mean 1. Few comparisons = thin evidence — the column is shown on purpose.</p>
<table><tr><th class="num">#</th><th>Project</th><th>Team</th><th class="num">Strength</th><th class="num">log π</th><th class="num">Wins</th><th class="num">Comparisons</th></tr>
{% for r in rows %}<tr><td class="num">{{ r.rank }}</td><td><a href="/projects/{{ r.project_id }}">{{ r.title }}</a></td><td>{{ r.team }}</td><td class="num">{{ "%.3f"|format(r.strength) }}</td><td class="num">{{ "%.3f"|format(r.log_strength) }}</td><td class="num">{{ r.wins }}</td><td class="num">{{ r.comparisons }}{% if r.comparisons < 3 %} <span class="pill warn">thin</span>{% endif %}</td></tr>{% else %}<tr><td colspan="7" class="muted">No comparisons yet.</td></tr>{% endfor %}</table>
<p class="small muted">JSON: <span class="mono">GET /api/v1/events/{{ e.id }}/pairwise</span></p>{% endblock %}"""


def register(app: FastAPI) -> None:
    from ..core.http import TEMPLATES
    from .api import v1
    TEMPLATES.env.loader.mapping = getattr(TEMPLATES.env.loader, "mapping", {})  # no-op guard
    from jinja2 import ChoiceLoader, DictLoader
    TEMPLATES.env.loader = ChoiceLoader([TEMPLATES.env.loader, DictLoader({"ext_pairwise.html": PAGE, "ext_pairwise_org.html": ORG_PAGE})])

    web = APIRouter()

    @web.get("/judge/pairwise/{event_id}")
    def page(request: Request, event_id: str):
        db = get_db(request)
        actor = auth.require_role(current_actor(request), "judge")
        e = events_svc.get_event(db, event_id)
        done = db.one("SELECT COUNT(*) AS n FROM pairwise_comparisons WHERE judge_id = ? AND event_id = ?", (actor.id, event_id))["n"]
        return render(request, "ext_pairwise.html", {"e": e, "pair": next_pair(db, actor.id, event_id), "done": done})

    @web.post("/judge/pairwise/{event_id}")
    async def submit(request: Request, event_id: str):
        db = get_db(request)
        d = await body_fields(request)
        record(db, current_actor(request), event_id, str(d.get("project_a")), str(d.get("project_b")), str(d.get("winner")))
        return redirect(f"/judge/pairwise/{event_id}", "Recorded.")

    @web.get("/organizer/events/{event_id}/pairwise")
    def org_page(request: Request, event_id: str):
        db = get_db(request)
        auth.require_staff(current_actor(request))
        e = events_svc.get_event(db, event_id)
        total = db.one("SELECT COUNT(*) AS n, COUNT(DISTINCT judge_id) AS j FROM pairwise_comparisons WHERE event_id = ?", (event_id,))
        return render(request, "ext_pairwise_org.html", {"e": e, "rows": fit_event(db, event_id), "total": total["n"], "judges": total["j"]})

    app.include_router(web)

    @v1.get("/events/{event_id}/pairwise", tags=["extensions"])
    def api_fit(request: Request, event_id: str):
        auth.require_staff(_auth.api_actor(request))
        return {"method": "bradley-terry-mm", "pseudo_wins": 0.5, "ranking": fit_event(get_db(request), event_id)}

    @v1.get("/events/{event_id}/pairwise/next", tags=["extensions"])
    def api_next(request: Request, event_id: str):
        actor = auth.require_role(_auth.api_actor(request), "judge")
        pair = next_pair(get_db(request), actor.id, event_id)
        return {"pair": [{"id": p["id"], "title": p["title"]} for p in pair] if pair else None}

    @v1.post("/events/{event_id}/pairwise", tags=["extensions"], status_code=201)
    def api_compare(request: Request, event_id: str, body: CompareIn):
        _auth.require_write(request)
        return record(get_db(request), _auth.api_actor(request), event_id, body.project_a, body.project_b, body.winner)
