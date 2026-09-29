"""T3: votes, comments, hidden results, randomized ballots, rate limits, audit."""
from conftest import ORG, login, register
from rubrica.core.services import community


def _project(client, db):
    return db.one("SELECT id FROM projects WHERE event_id = 'evt_02' AND status = 'submitted' AND team_id = 'tm_demo_02'")["id"]


def test_voting_rules(client, db):
    pid = _project(client, db)
    v1 = register(client, "v1@test.org")
    assert client.post(f"/api/projects/{pid}/vote").status_code == 401
    assert client.post(f"/api/projects/{pid}/vote", headers=v1).status_code == 200
    assert client.post(f"/api/projects/{pid}/vote", headers=v1).status_code == 409  # duplicate
    assert client.post("/api/projects/prj_01/vote", headers=v1).status_code == 403  # voting closed on fixture
    own = login(client, "second@rubrica.local")
    assert client.post(f"/api/projects/{pid}/vote", headers=own).status_code == 403  # own project
    assert db.one("SELECT COUNT(*) AS n FROM audit_events WHERE action = 'vote.duplicate_rejected'")["n"] >= 1
    assert client.delete(f"/api/projects/{pid}/vote", headers=v1).status_code == 200


def test_results_hidden_during_voting(client, db):
    assert client.get("/api/events/evt_02/results").status_code == 403
    page = client.get("/events/evt_02/results")
    assert page.status_code == 200 and "hidden" in page.text.lower()
    # organizer always sees
    r = client.get("/api/events/evt_02/results", headers=ORG)
    assert r.status_code in (200, 404)


def test_ballot_is_randomized_per_voter_but_stable(client, db):
    a = register(client, "ba@test.org")
    b = register(client, "bb@test.org")
    ida = [p["id"] for p in community.ballot(db, "evt_01", type("A", (), {"id": "ba"})())]
    idb = [p["id"] for p in community.ballot(db, "evt_01", type("B", (), {"id": "bb"})())]
    ida2 = [p["id"] for p in community.ballot(db, "evt_01", type("A", (), {"id": "ba"})())]
    assert ida == ida2 and sorted(ida) == sorted(idb) and ida != idb
    assert client.get("/events/evt_02/ballot", headers=a).status_code == 200
    assert client.get("/events/evt_02/ballot", headers=b).status_code == 200


def test_comments_and_rate_limit(client, db):
    pid = _project(client, db)
    u = register(client, "commenter@test.org")
    assert client.post(f"/api/projects/{pid}/comments", json={"body": ""}, headers=u).status_code == 400
    codes = [client.post(f"/api/projects/{pid}/comments", json={"body": f"zebra-comment-{i}"}, headers=u).status_code for i in range(7)]
    assert codes[:5] == [201] * 5 and codes[5:] == [429, 429]
    assert "zebra-comment-0" in client.get(f"/projects/{pid}").text
    cid = db.one("SELECT id FROM comments WHERE body = 'zebra-comment-0'")["id"]
    assert client.post(f"/organizer/comments/{cid}/hide", headers=u).status_code == 403
    assert client.post(f"/organizer/comments/{cid}/hide", headers=ORG).status_code == 303
    assert "zebra-comment-0" not in client.get(f"/projects/{pid}").text


def test_audit_trail_readable_and_exportable(client):
    r = client.get("/api/export/audit.csv", headers=ORG)
    assert r.status_code == 200 and r.text.splitlines()[0].startswith("seq,id,timestamp,actor_id,action")
    assert client.get("/organizer/audit?action=vote.", headers=ORG).status_code == 200
    assert client.get("/organizer/audit").status_code == 303  # anon → login
