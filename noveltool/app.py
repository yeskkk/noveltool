"""Local-only HTTP boundary. Business data lives in ProjectSession, not routes."""

from __future__ import annotations

from contextlib import asynccontextmanager
from importlib.resources import files
from pathlib import Path
import secrets
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import __version__
from .db import ProjectError, ProjectStore
from .domain import ConfigUpdate, TitleUpdate
from .runtime import EditConflictError, ProjectSession


def create_app(project_path: Path) -> FastAPI:
    project_path = Path(project_path).expanduser().resolve()
    csrf_token = secrets.token_urlsafe(32)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        store = ProjectStore.open(project_path)
        try:
            session = ProjectSession(store)
        except BaseException:
            store.close()
            raise
        app.state.session = session
        session.start_autosave()
        try:
            yield
        finally:
            await session.close()

    app = FastAPI(
        title="NovelTool", version=__version__, lifespan=lifespan,
        docs_url=None, redoc_url=None,
    )
    static_root = files("noveltool").joinpath("static")
    app.mount("/static", StaticFiles(directory=str(static_root)), name="static")

    @app.middleware("http")
    async def local_request_guard(request: Request, call_next):
        # Parse IPv6 correctly as well as IPv4. No permissive CORS is installed.
        host_header = request.headers.get("host", "")
        try:
            parsed = urlsplit("http://" + host_header)
            host = parsed.hostname
            _ = parsed.port
        except ValueError:
            return JSONResponse({"detail": "无效的 Host"}, status_code=400)
        if (host not in {"127.0.0.1", "localhost", "::1"} or parsed.username is not None
                or parsed.path or parsed.query or parsed.fragment):
            return JSONResponse({"detail": "只接受本机 Host"}, status_code=400)
        origin = request.headers.get("origin")
        if origin is not None and origin != f"{request.url.scheme}://{host_header}":
            return JSONResponse({"detail": "拒绝跨来源请求"}, status_code=403)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            supplied = request.headers.get("x-noveltool-token", "")
            if not supplied.isascii() or not secrets.compare_digest(supplied, csrf_token):
                return JSONResponse({"detail": "缺少或无效的本地会话令牌；请刷新页面"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self'; object-src 'none'; "
            "base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        )
        return response

    def session(request: Request) -> ProjectSession:
        return request.app.state.session

    @app.exception_handler(EditConflictError)
    async def edit_conflict(request: Request, exc: EditConflictError):
        return JSONResponse({"detail": str(exc), "code": "edit_conflict"}, status_code=409)

    @app.exception_handler(ProjectError)
    async def project_error(request: Request, exc: ProjectError):
        return JSONResponse({"detail": str(exc), "code": "storage_error"}, status_code=503)

    @app.get("/", response_class=HTMLResponse)
    async def home() -> str:
        return static_root.joinpath("index.html").read_text(encoding="utf-8").replace("{{VERSION}}", __version__)

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__, "milestone": "M1"}

    @app.get("/api/session")
    async def api_session() -> dict[str, str]:
        # This is CSRF protection, not an account/password authentication system.
        return {"csrf_token": csrf_token}

    @app.get("/api/status")
    async def status(request: Request):
        return {"version": __version__, "milestone": "M1", **await session(request).status()}

    @app.get("/api/config")
    async def config(request: Request):
        return await session(request).config_view()

    @app.put("/api/config")
    async def update_config(body: ConfigUpdate, request: Request):
        changed = await session(request).update_config(body.config, body.expected_memory_version)
        return {"changed": changed, **await session(request).status()}

    @app.put("/api/project")
    async def update_title(body: TitleUpdate, request: Request):
        changed = await session(request).update_title(body.title, body.expected_memory_version)
        return {"changed": changed, **await session(request).status()}

    @app.post("/api/save")
    async def save(request: Request):
        written = await session(request).save()
        return {"written": written, **await session(request).status()}

    return app
