"""T4 — certificates and signed, publicly verifiable judge participation records.

* Participant certificate: printable HTML per (event, user).
* Judge record: canonical JSON {judge, event, reviews, issued_at} signed with an
  Ed25519 key generated on first boot (``data/signing_key.pem``). The public key
  is served at ``/api/v1/records/public-key`` so anyone can verify offline.
  Falls back to HMAC (server-verifiable only) if ``cryptography`` is missing —
  the response says which.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from ..core import auth
from ..core.errors import Forbidden, NotFound
from ..core.http import get_db
from ..core.util import iso, new_id
from . import _auth

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    HAVE_ED25519 = True
except Exception:  # pragma: no cover
    HAVE_ED25519 = False

SCHEMA = """
CREATE TABLE IF NOT EXISTS certificates (
  id         TEXT PRIMARY KEY,
  event_id   TEXT NOT NULL REFERENCES events(id) ON DELETE CASCADE,
  user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  kind       TEXT NOT NULL,
  record_json TEXT NOT NULL,
  signature  TEXT NOT NULL,
  algorithm  TEXT NOT NULL,
  issued_at  TEXT NOT NULL,
  UNIQUE (event_id, user_id, kind)
);
"""

_priv = None
_hmac_secret: bytes | None = None


def _key_path() -> Path:
    return Path(os.environ.get("SIGNING_KEY_PATH", str(Path(os.environ.get("DATABASE_PATH", "data/rubrica.db")).parent / "signing_key.pem")))


def _load_keys() -> None:
    global _priv, _hmac_secret
    p = _key_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    if HAVE_ED25519:
        if p.exists():
            _priv = serialization.load_pem_private_key(p.read_bytes(), password=None)
        else:
            _priv = Ed25519PrivateKey.generate()
            p.write_bytes(_priv.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                              serialization.NoEncryption()))
    else:
        hp = p.with_suffix(".hmac")
        if not hp.exists():
            hp.write_bytes(os.urandom(32))
        _hmac_secret = hp.read_bytes()


def public_key_pem() -> str | None:
    if _priv is None:
        return None
    return _priv.public_key().public_bytes(serialization.Encoding.PEM,
                                           serialization.PublicFormat.SubjectPublicKeyInfo).decode()


def canonical(record: dict) -> bytes:
    return json.dumps(record, sort_keys=True, separators=(",", ":")).encode()


def sign_record(record: dict) -> tuple[str, str]:
    body = canonical(record)
    if _priv is not None:
        return base64.b64encode(_priv.sign(body)).decode(), "ed25519"
    return hmac.new(_hmac_secret or b"", body, hashlib.sha256).hexdigest(), "hmac-sha256"


def verify_record(record: dict, signature: str, algorithm: str) -> bool:
    body = canonical(record)
    try:
        if algorithm == "ed25519" and _priv is not None:
            _priv.public_key().verify(base64.b64decode(signature), body)
            return True
        if algorithm == "hmac-sha256":
            return hmac.compare_digest(hmac.new(_hmac_secret or b"", body, hashlib.sha256).hexdigest(), signature)
    except Exception:
        return False
    return False


def issue(db, event_id: str, user_id: str, kind: str) -> dict:
    existing = db.one("SELECT * FROM certificates WHERE event_id = ? AND user_id = ? AND kind = ?", (event_id, user_id, kind))
    if existing:
        d = dict(existing)
        d["record"] = json.loads(d.pop("record_json"))
        return d
    ev = db.one("SELECT id, name, submissions_close FROM events WHERE id = ?", (event_id,))
    user = auth.get_user(db, user_id)
    if not ev or not user:
        raise NotFound("event or user not found")
    record = {"kind": kind, "event_id": ev["id"], "event_name": ev["name"], "user_id": user.id,
              "name": user.name, "issued_at": iso(), "issuer": "rubrica"}
    if kind == "judge":
        if user.role != "judge":
            raise Forbidden("not a judge")
        n = db.one("SELECT COUNT(*) AS n FROM scores WHERE event_id = ? AND judge_id = ?", (event_id, user_id))["n"]
        record["reviews_completed"] = n
        record["tracks"] = [r["track_id"] for r in db.all(
            "SELECT jt.track_id FROM judge_tracks jt JOIN tracks t ON t.id = jt.track_id WHERE jt.judge_id = ? AND t.event_id = ?",
            (user_id, event_id))]
    else:
        team = db.one("SELECT t.id, t.name FROM teams t JOIN team_members tm ON tm.team_id = t.id"
                      " WHERE tm.user_id = ? AND t.event_id = ?", (user_id, event_id))
        if not team:
            raise Forbidden("user did not take part in this event")
        record["team"] = team["name"]
        record["projects"] = [r["title"] for r in db.all(
            "SELECT title FROM projects WHERE team_id = ? AND status = 'submitted' ORDER BY id", (team["id"],))]
    sig, algo = sign_record(record)
    cid = new_id("crt")
    db.run("INSERT INTO certificates(id, event_id, user_id, kind, record_json, signature, algorithm, issued_at)"
           " VALUES (?,?,?,?,?,?,?,?)", (cid, event_id, user_id, kind, json.dumps(record, sort_keys=True), sig, algo, record["issued_at"]))
    return {"id": cid, "event_id": event_id, "user_id": user_id, "kind": kind, "record": record,
            "signature": sig, "algorithm": algo, "issued_at": record["issued_at"]}


CERT_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{font:16px/1.5 Georgia,serif;background:#f3f1ec;margin:0;padding:2rem}}.c{{max-width:760px;margin:auto;background:#fff;border:12px double #1a1a1a;padding:3rem;text-align:center}}
h1{{letter-spacing:.2em;font-size:1.1rem;margin:0 0 1.5rem;text-transform:uppercase}}.n{{font-size:2rem;margin:.5rem 0}}.s{{font-family:ui-monospace,Menlo,monospace;font-size:.7rem;word-break:break-all;color:#555;margin-top:2rem;text-align:left}}
@media print{{body{{background:#fff;padding:0}}}}</style></head><body><div class="c">
<h1>Rubrica · {kind_title}</h1><p>This certifies that</p><div class="n">{name}</div><p>{line}</p><p><b>{event}</b></p>
<p style="margin-top:2rem">{extra}</p><p class="s">record id {cid} · issued {issued} · {algo}<br>signature {sig}<br>verify: POST /api/v1/records/verify or GET /api/v1/records/{cid}</p></div></body></html>"""


class VerifyIn(BaseModel):
    record: dict
    signature: str
    algorithm: str = "ed25519"


def on_startup(app: FastAPI) -> None:
    _load_keys()


def register(app: FastAPI) -> None:
    from .api import v1

    @v1.get("/records/public-key", tags=["extensions"])
    def public_key():
        return {"algorithm": "ed25519" if _priv is not None else "hmac-sha256",
                "public_key_pem": public_key_pem(),
                "note": "Signature is over the canonical JSON of `record` (sorted keys, no whitespace)."}

    @v1.post("/records/verify", tags=["extensions"])
    def verify(body: VerifyIn):
        return {"valid": verify_record(body.record, body.signature, body.algorithm)}

    @v1.get("/records/{record_id}", tags=["extensions"])
    def get_record(request: Request, record_id: str):
        r = get_db(request).one("SELECT * FROM certificates WHERE id = ?", (record_id,))
        if not r:
            raise NotFound("record not found")
        d = dict(r)
        d["record"] = json.loads(d.pop("record_json"))
        d["valid"] = verify_record(d["record"], d["signature"], d["algorithm"])
        return d

    @v1.post("/events/{event_id}/records/judges", tags=["extensions"])
    def issue_all_judges(request: Request, event_id: str):
        actor = auth.require_staff(_auth.api_actor(request))
        _auth.require_write(request)
        db = get_db(request)
        ids = [r["judge_id"] for r in db.all("SELECT DISTINCT judge_id FROM scores WHERE event_id = ?", (event_id,))]
        out = [issue(db, event_id, j, "judge") for j in ids]
        return {"issued": len(out), "records": [{"id": o["id"], "user_id": o["user_id"]} for o in out]}

    @v1.post("/events/{event_id}/records/participants", tags=["extensions"])
    def issue_all_participants(request: Request, event_id: str):
        auth.require_staff(_auth.api_actor(request))
        _auth.require_write(request)
        db = get_db(request)
        ids = [r["user_id"] for r in db.all(
            "SELECT DISTINCT tm.user_id FROM team_members tm JOIN teams t ON t.id = tm.team_id"
            " JOIN projects p ON p.team_id = t.id AND p.status = 'submitted' WHERE t.event_id = ?", (event_id,))]
        out = [issue(db, event_id, u, "participant") for u in ids]
        return {"issued": len(out)}

    @v1.get("/events/{event_id}/records", tags=["extensions"])
    def list_records(request: Request, event_id: str):
        db = get_db(request)
        rows = db.all("SELECT id, user_id, kind, algorithm, issued_at FROM certificates WHERE event_id = ? ORDER BY kind, user_id", (event_id,))
        return {"records": [dict(r) for r in rows]}

    @app.get("/certificates/{event_id}/{kind}", include_in_schema=False, response_class=HTMLResponse)
    def my_certificate(request: Request, event_id: str, kind: str, user: str | None = None):
        db = get_db(request)
        actor = auth.require(_auth.api_actor(request))
        uid = user if (user and actor.is_staff) else actor.id
        c = issue(db, event_id, uid, "judge" if kind == "judge" else "participant")
        rec = c["record"]
        if c["kind"] == "judge":
            line, extra = "served as a judge for", f"{rec['reviews_completed']} independent reviews completed · tracks {', '.join(rec['tracks']) or '—'}"
        else:
            line, extra = f"took part with team {rec['team']} in", "Projects: " + (", ".join(rec["projects"]) or "—")
        return HTMLResponse(CERT_HTML.format(title=f"Certificate · {rec['name']}", kind_title=f"{c['kind']} record",
                                             name=rec["name"], line=line, event=rec["event_name"], extra=extra,
                                             cid=c["id"], issued=c["issued_at"], algo=c["algorithm"], sig=c["signature"]))
