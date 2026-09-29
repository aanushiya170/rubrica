"""Application factory. Mounts core, then (optionally) extensions.

core/ never imports this module or anything under extensions/. Set
``RUBRICA_EXTENSIONS=0`` to boot with T1–T3 only; the acceptance checks are
unaffected either way.
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .core import seed
from .core.api.routes import router as core_api
from .core.db import Database
from .core.errors import DomainError
from .core.http import domain_error_handler
from .core.web.routes_judge import router as judge_router
from .core.web.routes_organizer import router as organizer_router
from .core.web.routes_public import router as public_router


def extensions_enabled() -> bool:
    return os.environ.get("RUBRICA_EXTENSIONS", "1") not in ("0", "false", "no")


def create_app(database_path: str | None = None, run_seed: bool = True, quiet: bool = False) -> FastAPI:
    db = Database(database_path)
    ext_modules = []
    if extensions_enabled():
        from .extensions import load as load_extensions  # noqa: WPS433 — optional layer
        ext_modules = load_extensions()
        for m in ext_modules:
            db.extra_schema.append(m.SCHEMA)
    db.init()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_seed:
            seed.seed_all(db, quiet=quiet)
        for m in ext_modules:
            if hasattr(m, "on_startup"):
                m.on_startup(app)
        yield
        for m in ext_modules:
            if hasattr(m, "on_shutdown"):
                m.on_shutdown(app)
        db.close()

    app = FastAPI(title="Rubrica", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.db = db
    app.add_exception_handler(DomainError, domain_error_handler)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        return JSONResponse({"error": "internal error", "detail": str(exc)[:200]}, status_code=500)

    @app.get("/health", tags=["core"])
    def health():
        return {"ok": True, "service": "rubrica"}

    app.include_router(core_api)
    app.include_router(public_router)
    app.include_router(judge_router)
    app.include_router(organizer_router)
    for m in ext_modules:
        m.register(app)
    return app


app = None


def get_app() -> FastAPI:
    global app
    if app is None:
        app = create_app()
    return app
