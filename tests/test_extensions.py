"""T4: API keys + OpenAPI, webhooks, signed records, embeds, bulk import/export, pairwise."""
import base64
import hashlib
import hmac
import json

from conftest import JUDGE_A, JUDGE_B, ORG, PARTICIPANT, login
from rubrica.core import signals
from rubrica.extensions import webhooks
from rubrica.extensions.pairwise import bradley_terry


def test_openapi_and_docs(client):
    spec = client.get("/api/v1/openapi.json").json()
    assert spec["info"]["title"] == "Rubrica API" and "/judge/scores" in spec["paths"]
    assert len(spec["paths"]) >= 35
    assert client.get("/api/v1/docs").status_code == 200


def test_api_keys_and_scopes(client):
    assert client.get("/api/v1/keys").status_code == 401
    rw = client.post("/api/v1/keys", json={"label": "ci", "scopes": "read write"}, headers=ORG).json()
    ro = client.post("/api/v1/keys", json={"label": "ro"}, headers=ORG).json()
    assert rw["key"].startswith("vk_")
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer " + rw["key"]}).json()["role"] == "organizer"
    assert client.post("/api/v1/events/evt_01/normalize", json={"k": 3}, headers={"Authorization": "Bearer " + ro["key"]}).status_code == 403
    assert client.post("/api/v1/events/evt_01/normalize", json={"k": 3}, headers={"Authorization": "Bearer " + rw["key"]}).status_code == 200
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer vk_bogus"}).status_code == 401
    client.delete(f"/api/v1/keys/{rw['id']}", headers=ORG)
    assert client.get("/api/v1/me", headers={"Authorization": "Bearer " + rw["key"]}).status_code == 401


def test_v1_role_isolation_is_the_same(client):
    assert client.get("/api/v1/judge/scores", headers=JUDGE_A).status_code == 200
    assert client.get("/api/v1/judge/scores?judge=jdg_24", headers=JUDGE_B).status_code == 403
    assert client.get("/api/v1/judge/scores", headers=PARTICIPANT).status_code == 403
    assert client.post("/api/v1/projects", json={"title": "late", "event_id": "evt_01"}, headers=PARTICIPANT).status_code == 403


def test_webhook_signature_and_delivery_records(client, db, monkeypatch):
    sent = []

    class FakeResp:
        status_code = 204

    def fake_post(url, content=None, timeout=None, headers=None):
        sent.append((url, content, headers))
        return FakeResp()

    monkeypatch.setattr(webhooks.httpx, "post", fake_post)
    w = client.post("/api/v1/webhooks", json={"url": "http://example.test/hook", "event_types": ["results.*"]}, headers=ORG).json()
    webhooks._deliver("results.published", {"entity_id": "x", "payload": {"event_id": "evt_01"}})
    webhooks._deliver("score.submitted", {"entity_id": "x", "payload": {}})  # not subscribed
    assert len(sent) == 1
    url, body, headers = sent[0]
    assert headers["X-Rubrica-Signature"] == "sha256=" + hmac.new(w["secret"].encode(), body, hashlib.sha256).hexdigest()
    lst = client.get("/api/v1/webhooks", headers=ORG).json()["webhooks"]
    assert lst[0]["deliveries"][0]["status"] == "delivered"
    assert client.post("/api/v1/webhooks", json={"url": "x"}, headers=PARTICIPANT).status_code == 403


def test_signed_judge_records(client):
    r = client.post("/api/v1/events/evt_01/records/judges", headers=ORG).json()
    assert r["issued"] == 30
    rec = client.get(f"/api/v1/records/{r['records'][0]['id']}").json()
    assert rec["valid"] and rec["algorithm"] == "ed25519"
    tampered = dict(rec["record"], reviews_completed=99)
    assert client.post("/api/v1/records/verify", json={"record": tampered, "signature": rec["signature"]}).json()["valid"] is False
    pk = client.get("/api/v1/records/public-key").json()["public_key_pem"]
    from cryptography.hazmat.primitives import serialization
    pub = serialization.load_pem_public_key(pk.encode())
    pub.verify(base64.b64decode(rec["signature"]), json.dumps(rec["record"], sort_keys=True, separators=(",", ":")).encode())
    assert client.get("/certificates/evt_01/judge", headers=JUDGE_A).status_code == 200
    assert client.get("/certificates/evt_01/participant", headers=PARTICIPANT).status_code == 200
    assert client.get("/certificates/evt_01/judge", headers=PARTICIPANT).status_code == 403


def test_embeds(client):
    assert "Glass Signal" in client.get("/embed/gallery?event=evt_01").text
    assert client.get("/embed/gallery.js?event=evt_01").headers["content-type"].startswith("application/javascript")
    r = client.get("/api/v1/embed/projects?event=evt_01")
    assert r.headers["access-control-allow-origin"] == "*" and r.json()["projects"][0]["id"] == "prj_01"


def test_bulk_export_import_roundtrip(client):
    ex = client.get("/api/v1/export/events/evt_01.json", headers=ORG).json()
    assert len(ex["projects"]) == 41 and len(ex["scores"]) == 126 and ex["rubric"]
    doc = json.loads(json.dumps(ex).replace("evt_01", "evt_rt").replace("prj_", "rt_p_").replace("tm_", "rt_t_")
                     .replace("trk_", "rt_k_").replace("scr_", "rt_s_"))
    doc["historical"] = True
    r = client.post("/api/v1/import/events", json=doc, headers=ORG)
    assert r.status_code == 201, r.text
    assert client.post("/api/v1/import/events", json=doc, headers=ORG).status_code == 409
    assert client.get("/api/v1/projects?event=evt_rt").json()["count"] == 41
    n = client.post("/api/v1/events/evt_rt/normalize", json={"k": 3}, headers=ORG).json()
    assert abs(n["global_mean"] - 3.568254) < 1e-6
    assert client.post("/api/v1/import/events", json={"nope": 1}, headers=ORG).status_code == 400
    assert client.post("/api/v1/import/events", json=doc, headers=PARTICIPANT).status_code == 403


def test_bradley_terry_recovers_order():
    pairs = [("a", "b")] * 6 + [("b", "c")] * 6 + [("a", "c")] * 6 + [("c", "b")] * 1
    fit = bradley_terry(pairs)
    assert fit["a"]["strength"] > fit["b"]["strength"] > fit["c"]["strength"]
    assert fit["a"]["wins"] == 12 and fit["b"]["comparisons"] == 13
    assert bradley_terry([]) == {}


def test_pairwise_flow(client):
    j1 = login(client, "jdg_demo_1@rubrica.local")
    nx = client.get("/api/v1/events/evt_02/pairwise/next", headers=j1).json()["pair"]
    if nx:
        a, b = nx[0]["id"], nx[1]["id"]
        assert client.post("/api/v1/events/evt_02/pairwise", json={"project_a": a, "project_b": b, "winner": a}, headers=j1).status_code == 201
        assert client.post("/api/v1/events/evt_02/pairwise", json={"project_a": a, "project_b": "prj_01", "winner": a}, headers=j1).status_code == 403
        rk = client.get("/api/v1/events/evt_02/pairwise", headers=ORG).json()["ranking"]
        assert rk[0]["project_id"] == a
    assert client.get("/judge/pairwise/evt_02", headers=j1).status_code == 200
    assert client.get("/organizer/events/evt_02/pairwise", headers=ORG).status_code == 200
    assert client.get("/api/v1/events/evt_02/pairwise", headers=j1).status_code == 403
