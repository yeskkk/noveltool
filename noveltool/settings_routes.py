"""Human-editable setting profiles, facts, states, notes, and narrative timeline."""
from importlib.resources import files
from typing import Literal
from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse
from . import __version__
from .settings import ProfileWrite, EntryWrite, ReviewWrite, Versioned
from .manuscript import ManuscriptError

router = APIRouter()

@router.get("/settings", response_class=HTMLResponse)
async def settings_page():
    return files("noveltool").joinpath("static/settings.html").read_text(encoding="utf-8").replace("{{VERSION}}", __version__)

@router.get("/timeline", response_class=HTMLResponse)
async def timeline_page():
    return files("noveltool").joinpath("static/timeline.html").read_text(encoding="utf-8").replace("{{VERSION}}", __version__)

@router.get("/api/settings")
async def settings_view(request: Request):
    return await request.app.state.session.knowledge.view()

@router.post("/api/settings/entities")
async def add_profile(body: ProfileWrite, request: Request):
    return await request.app.state.session.settings.put_profile(body)

@router.put("/api/settings/entities/{entity_id}")
async def edit_profile(entity_id: str, body: ProfileWrite, request: Request):
    return await request.app.state.session.settings.put_profile(body, entity_id)

@router.post("/api/settings/entries")
async def add_entry(body: EntryWrite, request: Request):
    return await request.app.state.session.settings.put_entry(body)

@router.put("/api/settings/entries/{entry_id}")
async def edit_entry(entry_id: str, body: EntryWrite, request: Request):
    return await request.app.state.session.settings.put_entry(body, entry_id)

@router.post("/api/settings/entries/{entry_id}/disable")
async def disable_entry(entry_id: str, body: Versioned, request: Request):
    return await request.app.state.session.settings.disable_entry(entry_id, body.expected_version)

@router.post("/api/settings/review")
async def review(body: ReviewWrite, request: Request):
    return await request.app.state.session.settings.review(body)

@router.post("/api/settings/review/accept-all")
async def accept_all_pending(body: Versioned, request: Request):
    return await request.app.state.session.settings.accept_all_pending(body)

@router.get("/api/settings/position")
async def position(request: Request, line: int = Query(ge=1), edge: Literal["start", "end"] = "end"):
    s = request.app.state.session
    async with s.lock:
        text = s.manuscript.text
        # split('\n') keeps the final empty logical line.
        lines = text.split("\n")
        if line > len(lines):
            raise ManuscriptError("逻辑行号超出正文")
        cp = sum(len(v)+1 for v in lines[:line-1])
        if edge == "end":
            cp += len(lines[line-1])
        return {"at_cp": cp, "revision_no": s.manuscript.revision_no, "excerpt": lines[line-1][:200]}

@router.get("/api/settings/history")
async def history(request: Request):
    s = request.app.state.session
    async with s.lock:
        return {"changes": [dict(row) for row in s.store.connection.execute(
            "SELECT * FROM setting_changes ORDER BY version DESC LIMIT 100")]}
