"""The seven run.py checks, replicated in-process, plus the edges around them."""
import json
from pathlib import Path

from conftest import JUDGE_A, JUDGE_B, ORG, PARTICIPANT

FIXTURE = json.loads((Path(__file__).resolve().parents[1] / "fixtures.json").read_text())


def test_gallery_public_and_shows_fixture(client):
    r = client.get("/projects")
    assert r.status_code == 200
    for p in FIXTURE["projects"][:3]:
        assert p["title"] in r.text


def test_gallery_search_and_filter(client):
    r = client.get("/projects", params={"q": "Dry Harbour"})
    assert r.text.count("Dry Harbour") >= 2  # prj_07 and prj_41
    r = client.get("/api/projects", params={"track": "trk_03"}).json()
    assert r["count"] > 0 and all(p["track_id"] == "trk_03" for p in r["projects"])


def test_closed_event_refuses_submissions(client):
    r = client.post("/projects/new", json={"title": "dogfood-late-submission-probe", "summary": "probe"},
                    headers=PARTICIPANT)
    assert 400 <= r.status_code < 500
    r = client.post("/projects/new", data={"title": "late", "event_id": "evt_01"}, headers=PARTICIPANT)
    assert r.status_code == 403


def test_anonymous_cannot_submit(client):
    assert client.post("/projects/new", json={"title": "x"}).status_code == 401


def test_judge_sees_own_scores(client):
    r = client.get("/api/judge/scores", headers=JUDGE_A)
    assert r.status_code == 200
    body = r.json()
    assert body["judge_id"] == "jdg_24" and body["count"] == 11
    assert all(a["score_id"] for a in body["assignments"])


def test_judge_cannot_see_peer_scores(client):
    assert client.get("/api/judge/scores?judge=jdg_24", headers=JUDGE_B).status_code == 403
    assert client.get("/api/judge/scores?judge=jdg_24", headers=JUDGE_A).status_code == 200  # own id is fine
    assert client.get("/judge/score/prj_01", headers=JUDGE_A).status_code == 403  # not assigned


def test_participant_and_anon_blocked(client):
    assert client.get("/api/judge/scores", headers=PARTICIPANT).status_code == 403
    assert client.get("/api/judge/scores").status_code == 401
    assert client.get("/api/export.csv", headers=PARTICIPANT).status_code == 403
    assert client.get("/api/export.csv", headers=JUDGE_A).status_code == 403


def test_organizer_can_see_any_judge(client):
    r = client.get("/api/judge/scores?judge=jdg_07", headers=ORG)
    assert r.status_code == 200 and r.json()["count"] == 3


def test_csv_export(client):
    r = client.get("/api/export.csv", headers=ORG)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    lines = r.text.splitlines()
    assert "," in lines[0] and len(lines) >= 127
