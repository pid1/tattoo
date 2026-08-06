"""fastapi app: static mounts over the dist volume, the settings surface,
and lifespan wiring (init_db + the scheduler thread). only /settings and
/api/* are dynamic; the briefing itself is pre-rendered static files
(plan §6).

no healthcheck endpoint by decision (plan §9): logs are the observability
surface.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.staticfiles import StaticFiles

from tattoo import config, database
from tattoo.log import log
from tattoo.routers import actions, settings, sources
from tattoo.scheduler import default_scheduler

templates = Jinja2Templates(directory=config.REPO_ROOT / "templates")


class NoCacheStaticFiles(StaticFiles):
    """force etag revalidation instead of safari's heuristic caching -- the
    dashboard url is stable while its bytes change daily (rally pattern)."""

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-cache"
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    database.init_db()
    conn = database.connect()
    try:
        from tattoo import judge

        judge.ensure_prompts(conn)
    finally:
        conn.close()
    default_scheduler.start()
    log("main", "startup complete", dist=str(config.dist_path()))
    yield
    default_scheduler.stop()


def create_app() -> FastAPI:
    """app factory so tests can build a fresh instance against their own
    env-pointed paths; production uses the module-level `app`."""
    config.ensure_dirs()  # mounts need the directories to exist
    app = FastAPI(
        title="tattoo",
        description="daily feed digest with a usefulness gate",
        version="0.1.0",
        lifespan=lifespan,
    )

    @app.middleware("http")
    async def no_store_api(request: Request, call_next):
        # api responses are state, never cacheable (puffin's middleware)
        response = await call_next(request)
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    app.include_router(settings.router)
    app.include_router(sources.router)
    app.include_router(actions.router)

    dist = config.dist_path()
    app.mount(
        "/dashboard", NoCacheStaticFiles(directory=dist / "dashboard", html=True), name="dashboard"
    )
    app.mount("/archive", NoCacheStaticFiles(directory=dist / "archive", html=True), name="archive")
    app.mount("/static", NoCacheStaticFiles(directory=config.REPO_ROOT / "static"), name="static")

    # HEAD included so probes (curl -I and friends) see the redirect too
    @app.api_route("/", methods=["GET", "HEAD"])
    def root() -> RedirectResponse:
        return RedirectResponse("/dashboard/")

    @app.get("/settings")
    def settings_page(request: Request):
        return templates.TemplateResponse(request, "settings.html")

    return app


app = create_app()
