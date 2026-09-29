#!/usr/bin/env python3
"""Rubrica extended acceptance checker — T3 and T4, verified against a live portal.

The official DOGFOOD ``run.py`` only contains T1/T2 checks, so it can never
print "verified T3 T4" for any team. This script fills that gap in the same
spirit: plain HTTP requests against ``base_url`` from ``.dogfood.toml``,
one PASS/FAIL line per behaviour, standard library only.

Usage:  python3 check_extended.py .dogfood.toml > acceptance-report-extended.txt

It is read-mostly but does create a few throwaway rows (test accounts, a vote,
a comment, a webhook, an imported event) on the demo data. It never touches
the official fixture event's projects or scores.
"""
import base64
import hashlib
import hmac
import http.server
import json
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, ".")
from run import load_config  # noqa: E402  (reuse the official TOML loader)

TIMEOUT = 10
RUN_ID = str(int(time.time()))[-6:]


def request(url, headers=None, method="GET", body=None, form=None, raw=False):
    """Plain HTTP via http.client: no redirect following, all Set-Cookie headers kept."""
    import http.client
    u = urllib.parse.urlsplit(url)
    data = None
    hdrs = dict(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        hdrs["Content-Type"] = "application/json"
    elif form is not None:
        data = urllib.parse.urlencode(form).encode()
        hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    if data is not None and method == "GET":
        method = "POST"
    conn_cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    try:
        conn = conn_cls(u.hostname, u.port, timeout=TIMEOUT)
        path = u.path or "/"
        if u.query:
            path += "?" + u.query
        conn.request(method, path, body=data, headers=hdrs)
        r = conn.getresponse()
        text = r.read().decode("utf-8", "replace")
        h = {k.lower(): v for k, v in r.getheaders()}
        h["set-cookie"] = "; ".join(r.msg.get_all("Set-Cookie") or [])
        conn.close()
        return r.status, text, h
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {e}", {}


def header_dict(h):
    name, _, value = (h or "").partition(":")
    return {name.strip(): value.strip()} if name else {}


class Check:
    def __init__(self, tier, label):
        self.tier, self.label, self.ok, self.detail = tier, label, False, []

    def note(self, s):
        self.detail.append(s)


def register(base, email, name):
    st, _, h = request(base + "/register", form={"email": email, "name": name, "password": "secret1", "next": "/"})
    m = re.search(r"session=([^;]+)", h.get("set-cookie", ""))
    return {"Cookie": f"session={m.group(1)}"} if (st == 303 and m) else None


def login(base, email, password="dogfood"):
    st, _, h = request(base + "/login", form={"email": email, "password": password, "next": "/"})
    m = re.search(r"session=([^;]+)", h.get("set-cookie", ""))
    return {"Cookie": f"session={m.group(1)}"} if (st == 303 and m) else None


def jget(url, headers=None):
    st, body, _ = request(url, headers)
    try:
        return st, json.loads(body)
    except Exception:
        return st, None


def build_checks(cfg):
    base = cfg["portal"]["base_url"].rstrip("/")
    auth = cfg.get("auth", {})
    org = header_dict(auth.get("organizer"))
    judge_a = header_dict(auth.get("judge_a"))
    judge_b = header_dict(auth.get("judge_b"))
    participant = header_dict(auth.get("participant"))
    checks = []

    def add(tier, label, ok, *notes):
        c = Check(tier, label)
        c.ok = bool(ok)
        if not ok:
            for n in notes:
                c.note(n)
        checks.append(c)

    # Find an event with an open voting window and a submitted project not owned by our test voter.
    st, ev = jget(base + "/api/v1/events")
    open_event = next((e for e in (ev or {}).get("events", []) if e.get("phase") == "voting"
                       or (e.get("voting_open") and e.get("voting_close") and e.get("phase") != "published")), None)
    eid = open_event["id"] if open_event else None
    st, pj = jget(base + f"/api/v1/projects?event={eid}") if eid else (0, None)
    pid = (pj or {}).get("projects", [{}])[0].get("id") if pj else None

    voter = register(base, f"voter{RUN_ID}@check.local", "Check Voter")
    voter2 = register(base, f"voter{RUN_ID}b@check.local", "Check Voter B")

    # ---------------- T3 -----------------------------------------------------
    add("T3", "anonymous vote refused", request(base + f"/api/projects/{pid}/vote", method="POST")[0] == 401,
        f"POST {base}/api/projects/{pid}/vote with no session", "wanted 401")

    st_vote = request(base + f"/api/projects/{pid}/vote", voter, method="POST")[0] if (voter and pid) else 0
    add("T3", "authenticated vote accepted", st_vote in (200, 201), f"got {st_vote}, wanted 200/201 (event {eid}, project {pid})")

    st_dup = request(base + f"/api/projects/{pid}/vote", voter, method="POST")[0] if (voter and pid) else 0
    add("T3", "duplicate vote rejected", st_dup == 409, f"got {st_dup}, wanted 409")

    st_closed = request(base + "/api/projects/prj_01/vote", voter, method="POST")[0] if voter else 0
    add("T3", "vote outside voting window refused", st_closed == 403, f"got {st_closed}, wanted 403 (fixture event has no voting window)")

    st_hidden = request(base + f"/api/events/{eid}/results")[0] if eid else 0
    st_page, page, _ = request(base + f"/events/{eid}/results") if eid else (0, "", {})
    add("T3", "results hidden during voting window", st_hidden == 403 and st_page == 200 and "hidden" in page.lower(),
        f"GET /api/events/{eid}/results → {st_hidden} (wanted 403); page mentions hidden: {'hidden' in page.lower()}")

    st_org = request(base + f"/api/events/{eid}/results", org)[0] if eid else 0
    add("T3", "organizer can see results while hidden", st_org in (200, 404), f"got {st_org}, wanted 200 (or 404 if nothing published)")

    b1 = jget(base + f"/api/v1/events/evt_01/ballot", voter)[1] or {}
    b2 = jget(base + f"/api/v1/events/evt_01/ballot", voter2)[1] or {}
    b1b = jget(base + f"/api/v1/events/evt_01/ballot", voter)[1] or {}
    o1 = [p["id"] for p in b1.get("projects", [])]
    o2 = [p["id"] for p in b2.get("projects", [])]
    o1b = [p["id"] for p in b1b.get("projects", [])]
    add("T3", "ballot randomized per voter, stable per voter", o1 and o1 == o1b and sorted(o1) == sorted(o2) and o1 != o2,
        f"voter1 stable: {o1 == o1b}; same set: {sorted(o1) == sorted(o2)}; different order: {o1 != o2}")

    st_c = request(base + f"/api/projects/{pid}/comments", voter, method="POST", body={"body": f"extended check {RUN_ID}"})[0]
    add("T3", "authenticated comment accepted", st_c == 201, f"got {st_c}, wanted 201")
    body = request(base + f"/projects/{pid}")[1]
    add("T3", "comment visible on project page", f"extended check {RUN_ID}" in body, "comment text not found on project page")

    codes = [request(base + f"/api/projects/{pid}/comments", voter, method="POST", body={"body": f"spam {i}"})[0] for i in range(8)]
    add("T3", "rate limit on comments (429)", 429 in codes, f"codes: {codes}; wanted a 429 within 8 rapid comments")

    st_a, audit, _ = request(base + "/api/export/audit.csv", org)
    first = audit.splitlines()[0] if audit else ""
    add("T3", "audit trail exportable as CSV", st_a == 200 and first.startswith("seq,") and "vote.cast" in audit and "vote.duplicate_rejected" in audit,
        f"status {st_a}; header {first!r}; has vote.cast: {'vote.cast' in audit}; has duplicate_rejected: {'vote.duplicate_rejected' in audit}")
    add("T3", "audit trail not readable by participants", request(base + "/api/export/audit.csv", participant)[0] == 403,
        "participant could read the audit CSV")

    # ---------------- T4 -----------------------------------------------------
    st_o, spec = jget(base + "/api/v1/openapi.json")
    add("T4", "OpenAPI document served", st_o == 200 and spec and spec.get("openapi", "").startswith("3.") and len(spec.get("paths", {})) >= 30,
        f"status {st_o}; paths: {len((spec or {}).get('paths', {}))}")
    st_d, docs, _ = request(base + "/api/v1/docs")
    add("T4", "API docs page works offline (no CDN)", st_d == 200 and "cdn" not in docs.lower() and "unpkg" not in docs.lower(),
        f"status {st_d}; page references a CDN: {'cdn' in docs.lower()}")

    st_k, key = jget(base + "/api/v1/keys") if False else request(base + "/api/v1/keys", org, method="POST", body={"label": "check", "scopes": "read write"})[:2]
    try:
        key = json.loads(key)
    except Exception:
        key = {}
    bearer = {"Authorization": "Bearer " + key.get("key", "")}
    st_me, me = jget(base + "/api/v1/me", bearer)
    add("T4", "API key authenticates as its owner", st_me == 200 and (me or {}).get("role") == "organizer", f"status {st_me}; me: {me}")
    ro = request(base + "/api/v1/keys", org, method="POST", body={"label": "ro"})[1]
    try:
        ro = json.loads(ro)
    except Exception:
        ro = {}
    st_ro = request(base + "/api/v1/events/evt_01/normalize", {"Authorization": "Bearer " + ro.get("key", "")}, method="POST", body={"k": 3})[0]
    add("T4", "read-only API key cannot write", st_ro == 403, f"got {st_ro}, wanted 403")
    add("T4", "API v1 enforces judge isolation too", request(base + "/api/v1/judge/scores?judge=jdg_24", judge_b)[0] == 403
        and request(base + "/api/v1/judge/scores", judge_a)[0] == 200, "v1 peer scores not refused / own scores not 200")

    # webhooks: local receiver
    got = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("content-length", 0))
            got.append((self.headers.get("X-Rubrica-Event"), self.headers.get("X-Rubrica-Signature"), self.rfile.read(n)))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    hook_url = f"http://127.0.0.1:{srv.server_address[1]}/hook"
    st_w, w, _ = request(base + "/api/v1/webhooks", bearer, method="POST", body={"url": hook_url, "event_types": ["normalization.*"]})
    try:
        w = json.loads(w)
    except Exception:
        w = {}
    request(base + "/api/v1/events/evt_01/normalize", bearer, method="POST", body={"k": 3})
    for _ in range(20):
        if got:
            break
        time.sleep(0.25)
    sig_ok = bool(got) and got[0][1] == "sha256=" + hmac.new(w.get("secret", "").encode(), got[0][2], hashlib.sha256).hexdigest()
    add("T4", "webhook delivered with valid HMAC signature", st_w == 201 and got and sig_ok,
        f"create: {st_w}; deliveries received: {len(got)}; signature valid: {sig_ok}")
    st_wl, wl = jget(base + "/api/v1/webhooks", org)
    deliv = next((x for x in (wl or {}).get("webhooks", []) if x["id"] == w.get("id")), {}).get("deliveries", [])
    add("T4", "webhook delivery recorded", any(d["status"] == "delivered" for d in deliv), f"deliveries: {deliv[:2]}")
    request(base + f"/api/v1/webhooks/{w.get('id')}", org, method="DELETE")
    srv.shutdown()

    # certificates / signed records
    st_r, recs = jget(base + "/api/v1/events/evt_01/records", org)
    if not (recs or {}).get("records"):
        request(base + "/api/v1/events/evt_01/records/judges", bearer, method="POST")
        st_r, recs = jget(base + "/api/v1/events/evt_01/records", org)
    rid = next((r["id"] for r in (recs or {}).get("records", []) if r["kind"] == "judge"), None)
    st_rec, rec = jget(base + f"/api/v1/records/{rid}") if rid else (0, None)
    add("T4", "signed judge record verifies", st_rec == 200 and rec and rec.get("valid") and rec.get("algorithm") == "ed25519",
        f"status {st_rec}; record: {rec and {k: rec.get(k) for k in ('valid', 'algorithm')}}")
    tam = dict((rec or {}).get("record", {}), reviews_completed=999)
    st_t, tv = jget(base + "/api/v1/records/verify") if False else request(base + "/api/v1/records/verify", method="POST",
                                                                             body={"record": tam, "signature": (rec or {}).get("signature", ""), "algorithm": "ed25519"})[:2]
    try:
        tv = json.loads(tv)
    except Exception:
        tv = {}
    add("T4", "tampered record rejected", tv.get("valid") is False, f"verify returned {tv}")
    st_pk, pk = jget(base + "/api/v1/records/public-key")
    add("T4", "public verification key published", st_pk == 200 and "BEGIN PUBLIC KEY" in ((pk or {}).get("public_key_pem") or ""), f"status {st_pk}")
    add("T4", "participant certificate renders", request(base + "/certificates/evt_01/participant", participant)[0] == 200, "expected 200")
    add("T4", "judge certificate renders", request(base + "/certificates/evt_01/judge", judge_a)[0] == 200, "expected 200")

    # embeds
    st_e, emb, _ = request(base + "/embed/gallery?event=evt_01")
    st_js, js, hj = request(base + "/embed/gallery.js?event=evt_01")
    st_ej, _, he = request(base + "/api/v1/embed/projects?event=evt_01")
    add("T4", "embeddable gallery (iframe page, script tag, CORS JSON)",
        st_e == 200 and "Glass Signal" in emb and st_js == 200 and "iframe" in js and st_ej == 200
        and he.get("access-control-allow-origin") == "*",
        f"page {st_e}, js {st_js}, json {st_ej}, CORS header: {he.get('access-control-allow-origin')}")

    # bulk export / import round trip
    st_x, ex = jget(base + "/api/v1/export/events/evt_01.json", org)
    add("T4", "bulk export of a whole event (fixture-shaped JSON)", st_x == 200 and ex and len(ex.get("projects", [])) == 41 and len(ex.get("scores", [])) == 126,
        f"status {st_x}; projects {len((ex or {}).get('projects', []))}; scores {len((ex or {}).get('scores', []))}")
    new_id = f"evt_chk{RUN_ID}"
    doc = json.loads(json.dumps(ex or {}).replace("evt_01", new_id).replace("prj_", f"p{RUN_ID}_").replace("tm_", f"t{RUN_ID}_")
                     .replace("trk_", f"k{RUN_ID}_").replace("scr_", f"s{RUN_ID}_"))
    doc["historical"] = True
    doc.setdefault("event", {})["name"] = f"Import check {RUN_ID}"
    st_i = request(base + "/api/v1/import/events", bearer, method="POST", body=doc)[0]
    st_i2 = request(base + "/api/v1/import/events", bearer, method="POST", body=doc)[0]
    st_g, g = jget(base + f"/api/v1/projects?event={new_id}")
    add("T4", "bulk import round-trips and is idempotent", st_i == 201 and st_i2 == 409 and (g or {}).get("count") == 41,
        f"import {st_i} (wanted 201), re-import {st_i2} (wanted 409), imported projects {(g or {}).get('count')}")
    add("T4", "bulk import refused for participants", request(base + "/api/v1/import/events", participant, method="POST", body=doc)[0] == 403,
        "participant was allowed to import")

    # pairwise
    demo_judge = login(base, "jdg_demo_1@rubrica.local")
    st_n, nx = jget(base + "/api/v1/events/evt_02/pairwise/next", demo_judge) if demo_judge else (0, None)
    pair = (nx or {}).get("pair")
    st_cmp = request(base + "/api/v1/events/evt_02/pairwise", demo_judge, method="POST",
                     body={"project_a": pair[0]["id"], "project_b": pair[1]["id"], "winner": pair[0]["id"]})[0] if pair else 0
    st_bt, bt = jget(base + "/api/v1/events/evt_02/pairwise", org)
    add("T4", "pairwise comparison + Bradley-Terry ranking", st_n == 200 and pair and st_cmp == 201 and st_bt == 200 and (bt or {}).get("ranking"),
        f"next {st_n}, compare {st_cmp}, ranking {st_bt}")
    add("T4", "pairwise refuses unassigned project", request(base + "/api/v1/events/evt_02/pairwise", demo_judge, method="POST",
        body={"project_a": pair[0]["id"] if pair else "x", "project_b": "prj_01", "winner": "prj_01"})[0] == 403, "expected 403")

    add("T4", "health endpoint", request(base + "/health")[0] == 200, "expected 200")
    if key.get("id"):
        request(base + f"/api/v1/keys/{key['id']}", org, method="DELETE")
    if ro.get("id"):
        request(base + f"/api/v1/keys/{ro['id']}", org, method="DELETE")
    return checks


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    cfg = load_config(sys.argv[1])
    print("Rubrica extended acceptance report (T3 + T4)")
    print(f"portal: {cfg['portal']['base_url']}")
    print("note: the official run.py has no T3/T4 checks; this file supplies them in the same style.")
    print()
    checks = build_checks(cfg)
    width = max(len(c.label) for c in checks) + 2
    for c in checks:
        print(f"{c.tier}  {c.label} {'.' * (width - len(c.label))} {'PASS' if c.ok else 'FAIL'}")
        for d in c.detail:
            print(f"       {d}")
    print()
    for t in ("T3", "T4"):
        n = sum(1 for c in checks if c.tier == t)
        ok = sum(1 for c in checks if c.tier == t and c.ok)
        print(f"{t}: {ok}/{n} {'verified' if ok == n else 'NOT verified'}")
    return 0 if all(c.ok for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
