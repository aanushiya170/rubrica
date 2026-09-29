"""HTTP plumbing shared by core web/API routers: actor resolution, error mapping, templates."""
from __future__ import annotations

from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from . import auth
from .db import Database
from .errors import DomainError
from .services import events as events_svc

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "web" / "templates"))


def get_db(request: Request) -> Database:
    """The Database lives on the root app; mounted sub-apps see it via scope."""
    db = getattr(request.app.state, "db", None)
    if db is None:
        db = request.scope.get("rubrica_db")
    if db is None:
        raise RuntimeError("database not attached to app")
    return db


def session_token(request: Request) -> str | None:
    tok = request.cookies.get("session")
    if tok:
        return tok
    authz = request.headers.get("authorization", "")
    if authz.lower().startswith("bearer "):
        return authz[7:].strip()
    return None


def current_actor(request: Request) -> auth.Actor | None:
    """Resolve the actor from the session. Cached per request."""
    if "actor" in request.scope:
        return request.scope["actor"]
    actor = auth.resolve_session(get_db(request), session_token(request))
    request.scope["actor"] = actor
    return actor


def wants_json(request: Request) -> bool:
    if request.url.path.startswith("/api/"):
        return True
    accept = request.headers.get("accept", "")
    ctype = request.headers.get("content-type", "")
    return "application/json" in ctype or ("application/json" in accept and "text/html" not in accept)


async def domain_error_handler(request: Request, exc: DomainError):
    if wants_json(request):
        return JSONResponse({"error": exc.message, "status": exc.status}, status_code=exc.status)
    if exc.status == 401:
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)
    return TEMPLATES.TemplateResponse(request, "error.html",
                                      {"status": exc.status, "message": exc.message, "actor": current_actor(request)},
                                      status_code=exc.status)


def render(request: Request, name: str, ctx: dict | None = None, status_code: int = 200):
    ctx = dict(ctx or {})
    ctx.setdefault("actor", current_actor(request))
    ctx.setdefault("flash", request.cookies.get("flash"))
    ctx.setdefault("phase", events_svc.phase)
    resp = TEMPLATES.TemplateResponse(request, name, ctx, status_code=status_code)
    if "flash" in request.cookies:
        resp.delete_cookie("flash")
    return resp


def redirect(url: str, flash: str | None = None) -> RedirectResponse:
    resp = RedirectResponse(url, status_code=303)
    if flash:
        resp.set_cookie("flash", flash, max_age=30, httponly=True, samesite="lax")
    return resp


async def body_fields(request: Request) -> dict:
    """Accept JSON or form bodies interchangeably."""
    ctype = request.headers.get("content-type", "")
    if "application/json" in ctype:
        try:
            data = await request.json()
        except Exception:
            return {}
        return data if isinstance(data, dict) else {}
    try:
        form = await request.form()
    except Exception:
        return {}
    return {k: v for k, v in form.items()}
