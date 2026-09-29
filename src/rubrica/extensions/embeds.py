"""T4 — embeddable gallery widget.

* ``/embed/gallery?event=evt_01`` — a bare HTML gallery meant for an iframe.
* ``/embed/gallery.js?event=evt_01`` — a script tag that injects that iframe.
* ``/api/v1/embed/projects?event=`` — CORS-open JSON for custom widgets.
"""
from __future__ import annotations

import html
import json

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse

from ..core.http import get_db
from ..core.services import projects as projects_svc

SCHEMA = ""

EMBED_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Gallery</title>
<style>body{{margin:0;font:14px/1.4 -apple-system,Segoe UI,Roboto,sans-serif;color:#1a1a1a;background:transparent}}
.g{{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:.6rem;padding:.6rem}}.c{{border:1px solid #e3e0d8;border-radius:8px;padding:.6rem;background:#fff}}
.c a{{color:#0b5cad;text-decoration:none;font-weight:600}}.m{{color:#666;font-size:.8rem}}.f{{text-align:right;padding:.3rem .6rem;font-size:.75rem;color:#888}}</style></head>
<body><div class="g">{cards}</div><div class="f">powered by <a href="{base}" target="_top">Rubrica</a></div></body></html>"""


def register(app: FastAPI) -> None:
    from .api import v1

    @app.get("/embed/gallery", include_in_schema=False, response_class=HTMLResponse)
    def embed_gallery(request: Request, event: str = "", track: str = "", q: str = "", limit: int = 60):
        base = str(request.base_url).rstrip("/")
        rows = projects_svc.gallery(get_db(request), q=q, track_id=track, event_id=event)[:limit]
        cards = "".join(
            f'<div class="c"><a href="{base}/projects/{html.escape(p["id"])}" target="_top">{html.escape(p["title"])}</a>'
            f'<div>{html.escape(p["summary"] or "")}</div><div class="m">{html.escape(p["team_name"])} · {html.escape(p["track_name"] or "")}</div></div>'
            for p in rows)
        resp = HTMLResponse(EMBED_HTML.format(cards=cards, base=base))
        resp.headers["Content-Security-Policy"] = "frame-ancestors *"
        return resp

    @app.get("/embed/gallery.js", include_in_schema=False)
    def embed_js(request: Request, event: str = "", track: str = "", height: int = 520):
        base = str(request.base_url).rstrip("/")
        src = f"{base}/embed/gallery?event={event}&track={track}"
        js = (f"(function(){{var s=document.currentScript;var f=document.createElement('iframe');"
              f"f.src={json.dumps(src)};f.style.width='100%';f.style.height='{int(height)}px';f.style.border='0';"
              f"f.loading='lazy';f.title='Rubrica gallery';s.parentNode.insertBefore(f,s);}})();")
        return Response(js, media_type="application/javascript", headers={"Access-Control-Allow-Origin": "*"})

    @v1.get("/embed/projects", tags=["extensions"])
    def embed_projects(request: Request, event: str = "", track: str = "", q: str = ""):
        rows = projects_svc.gallery(get_db(request), q=q, track_id=track, event_id=event)
        slim = [{k: p[k] for k in ("id", "title", "summary", "team_name", "track_name", "repo_url", "demo_url")} for p in rows]
        return Response(json.dumps({"projects": slim}), media_type="application/json",
                        headers={"Access-Control-Allow-Origin": "*"})
