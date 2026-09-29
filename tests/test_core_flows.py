"""Lifecycle: register → team → invite → draft → edit → submit → judge → normalize → publish → verify."""
from conftest import ORG, login, register


def test_full_lifecycle(client, db):
    alice = register(client, "alice@test.org", "Alice")
    r = client.post("/events/evt_02/teams", data={"name": "Alpha"}, headers=alice)
    assert r.status_code == 303
    team_id = r.headers["location"].split("/")[-1]
    code = db.one("SELECT invite_code FROM teams WHERE id = ?", (team_id,))["invite_code"]

    bob = register(client, "bob@test.org", "Bob")
    assert client.post(f"/join/{code}", headers=bob).status_code == 303
    assert client.post("/join/not-a-code", headers=bob).status_code == 404

    r = client.post("/projects/new", json={"title": "Alpha One", "event_id": "evt_02"}, headers=alice)
    assert r.status_code == 201
    pid = r.json()["id"]
    assert r.json()["status"] == "draft"
    assert client.get(f"/projects/{pid}").status_code == 404  # drafts are private
    assert client.get(f"/projects/{pid}", headers=bob).status_code == 200  # teammates see them

    assert client.post(f"/projects/{pid}/edit", data={"title": "Alpha One!", "action": "submit"}, headers=bob).status_code == 303
    assert db.one("SELECT status, title FROM projects WHERE id = ?", (pid,))["status"] == "submitted"
    assert "Alpha One!" in client.get("/projects?event=evt_02").text

    carol = register(client, "carol@test.org", "Carol")
    assert client.post(f"/projects/{pid}/edit", data={"title": "hijack"}, headers=carol).status_code == 403

    # judging on the demo event
    assert client.post("/organizer/events/evt_02/assign", data={"reviews": 2, "anchors": 1}, headers=ORG).status_code == 303
    j1 = login(client, "jdg_demo_1@rubrica.local")
    q = client.get("/api/judge/scores", headers=j1).json()
    assert any(a["project_id"] == pid for a in q["assignments"])
    r = client.post(f"/api/judge/scores/{pid}", json={"criteria": {"impact": 5, "execution": 4, "presentation": 3}}, headers=j1)
    assert r.status_code == 201 and abs(r.json()["weighted_raw"] - 4.3) < 1e-9
    assert client.post(f"/api/judge/scores/{pid}", json={"criteria": {"impact": 9, "execution": 4, "presentation": 3}}, headers=j1).status_code == 400
    assert client.post("/api/judge/scores/prj_01", json={"criteria": {}}, headers=j1).status_code == 403

    # progress dashboard
    p = client.get("/api/events/evt_02/progress", headers=ORG).json()
    assert p["total_scored"] >= 1 and any(j["id"] == "jdg_demo_1" for j in p["per_judge"])

    # normalize → publish → verify
    r = client.post("/api/events/evt_02/normalize", json={"k": 3}, headers=ORG)
    assert r.status_code == 200
    snap = client.post("/api/events/evt_02/publish", headers=ORG).json()
    v = client.get(f"/api/results/{snap['id']}/verify", headers=ORG).json()
    assert v["ok"] and v["inputs_hash_ok"]

    # tamper with a score → verification fails
    sid = snap["inputs"]["score_ids"][0]
    db.run("UPDATE scores SET weighted_raw = weighted_raw + 1 WHERE id = ?", (sid,))
    v = client.get(f"/api/results/{snap['id']}/verify", headers=ORG).json()
    assert not v["ok"] and v["differences"]
    db.run("UPDATE scores SET weighted_raw = weighted_raw - 1 WHERE id = ?", (sid,))


def test_rubric_versioning(client, db):
    before = db.one("SELECT rubric_version FROM scores WHERE event_id = 'evt_01' LIMIT 1")["rubric_version"]
    r = client.post("/organizer/events/evt_01/rubric", data={"rubric": "A | 0.5\nB | 0.5"}, headers=ORG)
    assert r.status_code == 303
    assert db.one("SELECT version FROM rubrics WHERE event_id = 'evt_01' AND active = 1")["version"] == before + 1
    assert db.one("SELECT rubric_version FROM scores WHERE event_id = 'evt_01' LIMIT 1")["rubric_version"] == before
    assert client.post("/organizer/events/evt_01/rubric", data={"rubric": "A | 0.7\nB | 0.5"}, headers=ORG).status_code == 400
    # restore the fixture rubric so other tests keep their numbers
    client.post("/organizer/events/evt_01/rubric", data={"rubric": "Functionality | 0.4\nQuality | 0.35\nInnovation | 0.25"}, headers=ORG)


def test_event_creation_and_deadline(client):
    r = client.post("/organizer/events/new", data={
        "name": "Past Hack", "submissions_close": "2020-01-01T00:00:00Z", "tracks": "One\nTwo",
        "prizes": "Gold | 1", "rubric": "X | 1.0"}, headers=ORG)
    assert r.status_code == 303
    eid = r.headers["location"].split("/")[-1]
    dave = register(client, "dave@test.org", "Dave")
    assert client.post(f"/events/{eid}/teams", data={"name": "Late"}, headers=dave).status_code == 303
    assert client.post("/projects/new", json={"title": "late", "event_id": eid}, headers=dave).status_code == 403
    assert client.get("/organizer/events/new", headers=dave).status_code == 403


def test_duplicate_resolution_never_deletes(client, db):
    alerts = client.get("/api/events/evt_01/alerts", headers=ORG).json()["alerts"]
    kinds = {a["kind"] for a in alerts}
    assert {"duplicate", "zero_variance", "low_sample", "coverage"} <= kinds
    r = client.post("/organizer/projects/prj_41/duplicate", data={"canonical_id": "prj_07", "action": "merge"}, headers=ORG)
    assert r.status_code == 303
    row = db.one("SELECT status, canonical_project_id FROM projects WHERE id = 'prj_41'")
    assert row["status"] == "excluded" and row["canonical_project_id"] == "prj_07"
    assert db.one("SELECT COUNT(*) AS n FROM scores WHERE project_id = 'prj_41'")["n"] > 0
    n = client.post("/api/events/evt_01/normalize", json={"k": 3}, headers=ORG).json()
    assert "prj_41" in n["excluded_project_ids"] and len(n["projects"]) == 40
    client.post("/organizer/projects/prj_41/duplicate", data={"canonical_id": "prj_07", "action": "keep"}, headers=ORG)


def test_health_and_home(client):
    assert client.get("/health").json()["ok"] is True
    assert "evt_01" in client.get("/").text
