"""T4 — bulk import/export: whole events as fixture-shaped JSON, plus CSVs.

Import reuses the exact loader the official fixture goes through, so anything
exported from one Rubrica (or hand-written in the DOGFOOD shape) can be
loaded into another. That is the migration path in and out.
"""
from __future__ import annotations

import json

from fastapi import FastAPI, Request, Response

from ..core import audit, auth, seed
from ..core.errors import Conflict, DomainError
from ..core.http import get_db
from ..core.services import export
from . import _auth

SCHEMA = ""


def register(app: FastAPI) -> None:
    from .api import v1

    @v1.get("/export/events/{event_id}.json", tags=["extensions"])
    def export_event(request: Request, event_id: str):
        auth.require_staff(_auth.api_actor(request))
        return export.event_json(get_db(request), event_id)

    @v1.get("/export/projects.csv", tags=["extensions"])
    def export_projects(request: Request, event: str | None = None):
        auth.require_staff(_auth.api_actor(request))
        return Response(export.projects_csv(get_db(request), event), media_type="text/csv")

    @v1.get("/export/audit.csv", tags=["extensions"])
    def export_audit(request: Request):
        auth.require_staff(_auth.api_actor(request))
        return Response(export.audit_csv(get_db(request)), media_type="text/csv")

    @v1.post("/import/events", tags=["extensions"], status_code=201,
             description="Import a fixtures.json-shaped document as a new event. Ids are used verbatim; "
                         "re-importing the same event id is a no-op (409).")
    async def import_event(request: Request):
        actor = auth.require_staff(_auth.api_actor(request))
        _auth.require_write(request)
        db = get_db(request)
        try:
            data = await request.json()
        except Exception as e:
            raise DomainError(f"body must be JSON: {e}", 400)
        if not isinstance(data, dict) or "event" not in data or "id" not in data["event"]:
            raise DomainError("document needs an `event` object with an `id`", 400)
        historical = bool(data.get("historical", False))
        out = seed.load_fixture(db, data=data, actor=actor, historical=historical)
        if out.get("skipped"):
            raise Conflict(f"event {data['event']['id']} already exists")
        audit.record(db, actor.id, "bulk.imported", "event", data["event"]["id"], {k: v for k, v in out.items() if k != "event"})
        return out
