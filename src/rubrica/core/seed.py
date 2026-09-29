"""Seeding: the official fixture (evt_01, never mutated) + a live demo event.

Idempotent: every insert is keyed on the fixture's own ids, and the whole
fixture load is skipped when ``evt_01`` already exists. Running
``docker compose up`` twice yields exactly 41 fixture projects.

The four test logins the acceptance checker attaches are fixed session tokens
(override with ``SEED_TOKEN_*`` env vars). They are printed on boot.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from . import auth
from .db import Database
from .services import judging, normalization
from .util import iso, new_id, parse_iso, plus

FIXTURE_EVENT = "evt_01"
DEMO_EVENT = "evt_02"
DEFAULT_PASSWORD = os.environ.get("SEED_PASSWORD", "dogfood")

# Judge A must have scores of their own; judge B is a different judge.
JUDGE_A = "jdg_24"
JUDGE_B = "jdg_26"

TEST_LOGINS = {
    "organizer": ("usr_organizer", os.environ.get("SEED_TOKEN_ORGANIZER", "org_7f2a9c1d")),
    "judge_a": (JUDGE_A, os.environ.get("SEED_TOKEN_JUDGE_A", "jdg_a_91bc3e4f")),
    "judge_b": (JUDGE_B, os.environ.get("SEED_TOKEN_JUDGE_B", "jdg_b_44de5a6b")),
    "participant": ("usr_participant", os.environ.get("SEED_TOKEN_PARTICIPANT", "prt_2e88c7d0")),
}

FIXTURE_RUBRIC = [
    {"key": "functionality", "name": "Functionality", "weight": 0.40},
    {"key": "quality", "name": "Quality", "weight": 0.35},
    {"key": "innovation", "name": "Innovation", "weight": 0.25},
]


def fixture_path() -> Path:
    env = os.environ.get("FIXTURES_PATH")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for cand in (here.parents[3] / "fixtures.json", Path("fixtures.json"), Path("data/fixtures.json")):
        if cand.exists():
            return cand
    return here.parents[3] / "fixtures.json"


def _ensure_user(db: Database, user_id: str, email: str, name: str, role: str) -> str:
    r = db.one("SELECT id FROM users WHERE id = ? OR email = ?", (user_id, email.lower()))
    if r:
        return r["id"]
    auth.create_user(db, email, name, DEFAULT_PASSWORD, role, user_id=user_id)
    return user_id


def _system_actor(db: Database) -> auth.Actor:
    uid = _ensure_user(db, "usr_organizer", "organizer@rubrica.local", "Olive Organizer", "organizer")
    _ensure_user(db, "usr_admin", "admin@rubrica.local", "Ada Admin", "admin")
    return auth.get_user(db, uid)


def load_fixture(db: Database, path: Path | None = None, data: dict | None = None,
                 actor: auth.Actor | None = None, historical: bool = True) -> dict:
    """Load a fixtures.json-shaped document into its own event. Returns a summary.

    Used for the official fixture on boot and for bulk import (T4). Ids in the
    document are used verbatim, so importing the same document twice is a no-op.
    """
    if data is None:
        path = path or fixture_path()
        data = json.loads(Path(path).read_text())
    fx = data
    actor = actor or _system_actor(db)
    ev = fx["event"]
    if db.one("SELECT 1 FROM events WHERE id = ?", (ev["id"],)):
        return {"skipped": True, "event": ev["id"]}

    with db.tx() as c:
        c.execute("INSERT INTO events(id, name, description, submissions_open, submissions_close, voting_open,"
                  " voting_close, is_historical, results_published, created_by, created_at)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                  (ev["id"], ev["name"],
                   ev.get("description") or ("Official DOGFOOD 2026 fixture. Loaded verbatim; never mutated."
                                             if ev["id"] == FIXTURE_EVENT else "Imported event."),
                   ev.get("submissions_open"), ev["submissions_close"], ev.get("voting_open"),
                   ev.get("voting_close"), int(historical), 0, actor.id, iso()))
        for t in fx["tracks"]:
            c.execute("INSERT INTO tracks(id, event_id, name) VALUES (?,?,?)", (t["id"], ev["id"], t["name"]))
        prizes = fx.get("prizes") or ([{"title": "Grand prize", "amount": "1500 USD"},
                                       {"title": "Runner up", "amount": "700 USD"},
                                       {"title": "Best judging engine", "amount": "300 USD"}]
                                      if ev["id"] == FIXTURE_EVENT else [])
        for i, pr in enumerate(prizes, 1):
            c.execute("INSERT INTO prizes(id, event_id, rank, title, amount) VALUES (?,?,?,?,?)",
                      (f"{ev['id']}_prz_{i}", ev["id"], int(pr.get("rank", i)), pr["title"], str(pr.get("amount", ""))))

    for j in fx.get("judges", []):
        _ensure_user(db, j["id"], j["email"], j["name"], "judge")
        judging.set_judge_tracks(db, j["id"], j["tracks"])

    for t in fx.get("teams", []):
        with db.tx() as c:
            c.execute("INSERT INTO teams(id, event_id, name, invite_code, created_at) VALUES (?,?,?,?,?)",
                      (t["id"], ev["id"], t["name"], t.get("invite_code") or f"inv-{t['id']}", iso()))
        for i, email in enumerate(t["members"]):
            uid = _ensure_user(db, f"usr_{email.split('@')[0]}", email, email.split("@")[0].replace(".", " ").title(),
                               "participant")
            db.run("INSERT OR IGNORE INTO team_members(team_id, user_id) VALUES (?,?)", (t["id"], uid))

    with db.tx() as c:
        for p in fx.get("projects", []):
            ts = p.get("submitted_at") or iso()
            c.execute("INSERT INTO projects(id, event_id, team_id, track_id, title, summary, repo_url, demo_url,"
                      " status, submitted_at, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                      (p["id"], ev["id"], p["team"], p.get("track"), p["title"], p.get("summary", ""),
                       p.get("repo_url", ""), p.get("demo_url", ""), p.get("status", "submitted"),
                       p.get("submitted_at"), ts, ts))

    rubric = judging.set_rubric(db, actor, ev["id"], fx.get("rubric") or FIXTURE_RUBRIC)

    # Scores → assignments + scores. Every fixture review is an independent
    # overlap review, so each is marked as an anchor observation.
    with db.tx() as c:
        for i, s in enumerate(fx.get("scores", []), 1):
            aid = f"asg_{ev['id']}_{i:03d}"
            c.execute("INSERT OR IGNORE INTO assignments(id, event_id, project_id, judge_id, is_anchor, created_at)"
                      " VALUES (?,?,?,?,1,?)", (aid, ev["id"], s["project"], s["judge"], ev["submissions_close"]))
            row = c.execute("SELECT id FROM assignments WHERE project_id = ? AND judge_id = ?",
                            (s["project"], s["judge"])).fetchone()
            crit = {k: float(v) for k, v in s["criteria"].items()}
            c.execute("INSERT INTO scores(id, assignment_id, event_id, judge_id, project_id, rubric_version,"
                      " criteria_json, weighted_raw, comment, submitted_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                      (s.get("id") or f"scr_{ev['id']}_{i:03d}", row["id"], ev["id"], s["judge"], s["project"], rubric["version"],
                       json.dumps(crit, sort_keys=True), judging.weighted(crit, rubric),
                       s.get("comment") or "", ev["submissions_close"]))
        c.execute("INSERT INTO audit_events(id, timestamp, actor_id, action, entity_type, entity_id, payload_json)"
                  " VALUES (?,?,?,?,?,?,?)",
                  (new_id("aud"), iso(), actor.id, "fixture.loaded", "event", ev["id"],
                   json.dumps({"path": str(path), "projects": len(fx.get("projects", [])),
                               "scores": len(fx.get("scores", []))})))
    return {"skipped": False, "event": ev["id"], "projects": len(fx.get("projects", [])),
            "judges": len(fx.get("judges", [])), "tracks": len(fx.get("tracks", [])), "scores": len(fx.get("scores", []))}


def seed_demo_event(db: Database) -> dict:
    """evt_02: an open event so submit → judge → publish can be demonstrated live."""
    actor = _system_actor(db)
    if db.one("SELECT 1 FROM events WHERE id = ?", (DEMO_EVENT,)):
        return {"skipped": True, "event": DEMO_EVENT}
    from .services import events as events_svc, teams as teams_svc, projects as projects_svc
    events_svc.create_event(
        db, actor, "Rubrica Live Demo 2026", submissions_close=plus(days=30),
        submissions_open=plus(days=-1), voting_open=plus(days=-1), voting_close=plus(days=45),
        description="A live event seeded so the full lifecycle can be exercised. Submissions are open.",
        track_names=["Developer tools", "Civic tech", "Open hardware"],
        prize_rows=[{"rank": 1, "title": "Winner", "amount": "500 USD"}, {"rank": 2, "title": "Runner up", "amount": "250 USD"}],
        event_id=DEMO_EVENT)
    tracks = events_svc.tracks(db, DEMO_EVENT)
    judging.set_rubric(db, actor, DEMO_EVENT, [
        {"key": "impact", "name": "Impact", "weight": 0.5},
        {"key": "execution", "name": "Execution", "weight": 0.3},
        {"key": "presentation", "name": "Presentation", "weight": 0.2},
    ])
    # Two demo judges cover all demo tracks.
    for jid, name in (("jdg_demo_1", "Dana Demo-Judge"), ("jdg_demo_2", "Eli Demo-Judge")):
        _ensure_user(db, jid, f"{jid}@rubrica.local", name, "judge")
        judging.set_judge_tracks(db, jid, [t["id"] for t in tracks])
    # A demo team with a submitted project and a draft.
    demo_user = _ensure_user(db, "usr_demo_maker", "maker@rubrica.local", "Mira Maker", "participant")
    demo_actor = auth.get_user(db, demo_user)
    team = teams_svc.create_team(db, demo_actor, DEMO_EVENT, "Demo Makers", team_id="tm_demo_01",
                                 invite_code="inv-demo-makers")
    projects_svc.create_project(db, demo_actor, "Lantern Ledger", "Shows how any published result was derived.",
                                "https://example.org/repo/lantern", track_id=tracks[0]["id"], event_id=DEMO_EVENT,
                                submit=True)
    projects_svc.create_project(db, demo_actor, "Draft Compass", "Still a draft — editable until the deadline.",
                                "", track_id=tracks[1]["id"], event_id=DEMO_EVENT, submit=False)
    second = _ensure_user(db, "usr_demo_second", "second@rubrica.local", "Sam Second", "participant")
    second_actor = auth.get_user(db, second)
    teams_svc.create_team(db, second_actor, DEMO_EVENT, "Second Wind", team_id="tm_demo_02", invite_code="inv-second-wind")
    projects_svc.create_project(db, second_actor, "Quiet Quorum", "Pairwise judging with a Bradley–Terry fit.",
                                "https://example.org/repo/quorum", track_id=tracks[0]["id"], event_id=DEMO_EVENT,
                                submit=True)
    judging.assign(db, actor, DEMO_EVENT, reviews_per_project=2, anchor_count=1)
    return {"skipped": False, "event": DEMO_EVENT, "team_invite": team["invite_code"]}


def seed_test_logins(db: Database) -> dict[str, str]:
    """Fixed sessions for the checker. The participant sits on tm_01 in the closed fixture event only."""
    _system_actor(db)
    pid = _ensure_user(db, "usr_participant", "participant@rubrica.local", "Pat Participant", "participant")
    if db.one("SELECT 1 FROM teams WHERE id = 'tm_01'"):
        db.run("INSERT OR IGNORE INTO team_members(team_id, user_id) VALUES ('tm_01', ?)", (pid,))
    headers = {}
    for label, (uid, tok) in TEST_LOGINS.items():
        if not db.one("SELECT 1 FROM users WHERE id = ?", (uid,)):
            continue
        auth.create_session(db, uid, fixed_token=tok)
        headers[label] = f"Cookie: session={tok}"
    return headers


def seed_all(db: Database, quiet: bool = False) -> dict:
    fx = load_fixture(db)
    demo = seed_demo_event(db)
    logins = seed_test_logins(db)
    counts = {
        "projects": db.one("SELECT COUNT(*) AS n FROM projects WHERE event_id = ?", (FIXTURE_EVENT,))["n"],
        "judges": db.one("SELECT COUNT(*) AS n FROM users WHERE role = 'judge' AND id LIKE 'jdg___'")["n"],
        "tracks": db.one("SELECT COUNT(*) AS n FROM tracks WHERE event_id = ?", (FIXTURE_EVENT,))["n"],
        "scores": db.one("SELECT COUNT(*) AS n FROM scores WHERE event_id = ?", (FIXTURE_EVENT,))["n"],
    }
    if not quiet:
        print(f"seeded. fixture {FIXTURE_EVENT}: {counts['projects']} projects, {counts['judges']} judges, "
              f"{counts['tracks']} tracks, {counts['scores']} scores"
              f"{' (already present, skipped)' if fx.get('skipped') else ''}", file=sys.stderr)
        print(f"demo event {DEMO_EVENT}: {'ready' if demo.get('skipped') else 'created'} (submissions open)",
              file=sys.stderr)
        print("test logins:", file=sys.stderr)
        for label, h in logins.items():
            print(f"  {label:<12} {h}", file=sys.stderr)
        print(f"password for every seeded account: {DEFAULT_PASSWORD}", file=sys.stderr)
        sys.stderr.flush()
    return {"fixture": fx, "demo": demo, "logins": logins, "counts": counts}
