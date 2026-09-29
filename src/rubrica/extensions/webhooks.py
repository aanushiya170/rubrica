"""T4 — outbound webhooks.

Subscribes to the core signal bus (every audit action is a signal), matches
subscriptions by event type (exact or prefix ``score.*``), and delivers JSON
with an HMAC-SHA256 signature from a background worker thread. Deliveries and
their outcome are recorded so an organizer can see what fired.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import queue
import threading

import httpx
from fastapi import FastAPI, Request
from pydantic import BaseModel

from ..core import auth, signals
from ..core.db import Database
from ..core.errors import NotFound
from ..core.http import get_db
from ..core.util import iso, new_id, token
from . import _auth

SCHEMA = """
CREATE TABLE IF NOT EXISTS webhooks (
  id          TEXT PRIMARY KEY,
  event_id    TEXT REFERENCES events(id) ON DELETE CASCADE,
  url         TEXT NOT NULL,
  event_types TEXT NOT NULL,
  secret      TEXT NOT NULL,
  active      INTEGER NOT NULL DEFAULT 1,
  created_by  TEXT REFERENCES users(id),
  created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
  id            TEXT PRIMARY KEY,
  webhook_id    TEXT NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
  event_type    TEXT NOT NULL,
  payload_json  TEXT NOT NULL,
  status        TEXT NOT NULL,
  response_code INTEGER,
  error         TEXT,
  created_at    TEXT NOT NULL,
  delivered_at  TEXT
);
"""

_queue: "queue.Queue[tuple[str, dict]]" = queue.Queue()
_worker: threading.Thread | None = None
_db: Database | None = None


class WebhookIn(BaseModel):
    url: str
    event_types: list[str] = ["*"]
    event_id: str | None = None


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def _matches(patterns: list[str], action: str) -> bool:
    return any(p == "*" or p == action or (p.endswith("*") and action.startswith(p[:-1])) for p in patterns)


def _deliver(action: str, payload: dict) -> None:
    assert _db is not None
    hooks = _db.all("SELECT * FROM webhooks WHERE active = 1")
    for h in hooks:
        if not _matches(json.loads(h["event_types"]), action):
            continue
        if h["event_id"] and payload.get("payload", {}).get("event_id") not in (None, h["event_id"]) \
                and payload.get("entity_id") != h["event_id"]:
            continue
        body = json.dumps({"type": action, "delivered_at": iso(), "data": payload}, sort_keys=True).encode()
        did = new_id("dlv")
        _db.run("INSERT INTO webhook_deliveries(id, webhook_id, event_type, payload_json, status, created_at)"
                " VALUES (?,?,?,?,?,?)", (did, h["id"], action, body.decode(), "pending", iso()))
        status, code, err = "failed", None, None
        try:
            r = httpx.post(h["url"], content=body, timeout=5.0,
                           headers={"Content-Type": "application/json", "X-Rubrica-Event": action,
                                    "X-Rubrica-Signature": sign(h["secret"], body), "X-Rubrica-Delivery": did})
            code = r.status_code
            status = "delivered" if 200 <= code < 300 else "failed"
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"[:300]
        _db.run("UPDATE webhook_deliveries SET status = ?, response_code = ?, error = ?, delivered_at = ? WHERE id = ?",
                (status, code, err, iso(), did))


def _run() -> None:
    while True:
        item = _queue.get()
        if item is None:
            return
        try:
            _deliver(*item)
        except Exception:  # noqa: BLE001
            pass


def _on_signal(action: str, payload: dict) -> None:
    if _db is not None and _db.one("SELECT 1 FROM webhooks WHERE active = 1 LIMIT 1"):
        _queue.put((action, payload))


def on_startup(app: FastAPI) -> None:
    global _worker, _db
    _db = app.state.db
    signals.on("*", _on_signal)
    _worker = threading.Thread(target=_run, name="webhooks", daemon=True)
    _worker.start()


def on_shutdown(app: FastAPI) -> None:
    _queue.put(None)


def register(app: FastAPI) -> None:
    from .api import v1

    @v1.get("/webhooks", tags=["extensions"])
    def list_hooks(request: Request):
        auth.require_staff(_auth.api_actor(request))
        db = get_db(request)
        out = []
        for h in db.all("SELECT id, event_id, url, event_types, active, created_at FROM webhooks ORDER BY created_at DESC"):
            d = dict(h)
            d["event_types"] = json.loads(d["event_types"])
            d["deliveries"] = [dict(x) for x in db.all(
                "SELECT id, event_type, status, response_code, error, created_at FROM webhook_deliveries"
                " WHERE webhook_id = ? ORDER BY created_at DESC LIMIT 10", (h["id"],))]
            out.append(d)
        return {"webhooks": out}

    @v1.post("/webhooks", tags=["extensions"], status_code=201)
    def create_hook(request: Request, body: WebhookIn):
        actor = auth.require_staff(_auth.api_actor(request))
        _auth.require_write(request)
        db = get_db(request)
        wid, secret = new_id("whk"), token(24)
        db.run("INSERT INTO webhooks(id, event_id, url, event_types, secret, created_by, created_at) VALUES (?,?,?,?,?,?,?)",
               (wid, body.event_id, body.url, json.dumps(body.event_types), secret, actor.id, iso()))
        return {"id": wid, "url": body.url, "event_types": body.event_types, "event_id": body.event_id,
                "secret": secret, "signature_header": "X-Rubrica-Signature (sha256=HMAC(secret, raw body))"}

    @v1.delete("/webhooks/{webhook_id}", tags=["extensions"])
    def delete_hook(request: Request, webhook_id: str):
        auth.require_staff(_auth.api_actor(request))
        db = get_db(request)
        if not db.one("SELECT 1 FROM webhooks WHERE id = ?", (webhook_id,)):
            raise NotFound("webhook not found")
        db.run("UPDATE webhooks SET active = 0 WHERE id = ?", (webhook_id,))
        return {"ok": True}

    @v1.post("/webhooks/{webhook_id}/test", tags=["extensions"])
    def test_hook(request: Request, webhook_id: str):
        auth.require_staff(_auth.api_actor(request))
        _queue.put(("webhook.test", {"entity_id": webhook_id, "payload": {"hello": "rubrica"}}))
        return {"queued": True}
