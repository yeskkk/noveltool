"""Thin HTTP boundary for confirmed text; no LLM and no browser-draft storage."""
from __future__ import annotations
from importlib.resources import files
from typing import Literal
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator
from . import __version__
from .manuscript import MAX_TEXT_CHARS, check_text, range_for_lines

router = APIRouter()


class TextRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)
    expected_revision_no: int = Field(ge=0)


class TextBody(TextRequest):
    text: str = Field(max_length=MAX_TEXT_CHARS)

    @field_validator("text")
    @classmethod
    def validate_text(cls, text: str) -> str:
        check_text(text)
        return text.replace("\r\n", "\n").replace("\r", "\n")


class ImportBody(TextBody):
    paragraph_mode: Literal["auto", "blankline", "line"] = "auto"


class ReplaceBody(TextBody):
    start_cp: int = Field(ge=0)
    end_cp: int = Field(ge=0)
    selected_text: str = Field(max_length=MAX_TEXT_CHARS)
    instruction: str = Field(default="", max_length=4000)


class LineBody(TextRequest):
    first_line: int = Field(ge=1)
    last_line: int = Field(ge=1)


def minimal_change(before: str, after: str) -> tuple[int, int, str]:
    """One contiguous diff; preserve common prefix/suffix and untouched block IDs."""
    start = 0
    while start < min(len(before), len(after)) and before[start] == after[start]:
        start += 1
    tail = 0
    while (tail < len(before)-start and tail < len(after)-start
           and before[len(before)-tail-1] == after[len(after)-tail-1]):
        tail += 1
    end_before, end_after = len(before)-tail, len(after)-tail
    return start, end_before, after[start:end_after]


@router.get("/manuscript", response_class=HTMLResponse)
async def page():
    return files("noveltool").joinpath("static/manuscript.html").read_text(encoding="utf-8").replace("{{VERSION}}", __version__)


@router.get("/api/manuscript")
async def get_text(request: Request):
    return await request.app.state.session.manuscript_view()


@router.post("/api/manuscript/import")
async def import_text(body: ImportBody, request: Request):
    s = request.app.state.session
    await s.import_manuscript(body.text.removeprefix("\ufeff"), body.expected_revision_no, body.paragraph_mode)
    return await s.manuscript_view()


@router.post("/api/manuscript/append")
async def append(body: TextBody, request: Request):
    s = request.app.state.session
    await s.append_manuscript(body.text, body.expected_revision_no)
    return await s.manuscript_view()


@router.post("/api/manuscript/replace")
async def replace(body: ReplaceBody, request: Request):
    s = request.app.state.session
    await s.replace_manuscript(body.start_cp, body.end_cp, body.text, body.expected_revision_no,
                               body.instruction, selected_text=body.selected_text)
    return await s.manuscript_view()


@router.post("/api/manuscript/edit")
async def full_edit(body: TextBody, request: Request):
    s = request.app.state.session
    # Snapshot and minimal diff use the same event loop; the service checks the
    # expected revision again after acquiring its lock, closing any race.
    async with s.lock:
        s.manuscript.check_revision(body.expected_revision_no)
        old = s.manuscript.text
    start, end, replacement = minimal_change(old, body.text)
    await s.replace_manuscript(start, end, replacement, body.expected_revision_no,
                               "手工整篇编辑", selected_text=old[start:end])
    return await s.manuscript_view()


@router.post("/api/manuscript/line-range")
async def line_range(body: LineBody, request: Request):
    s = request.app.state.session
    async with s.lock:
        s.manuscript.check_revision(body.expected_revision_no)
        text = s.manuscript.text
        start, end = range_for_lines(text, body.first_line, body.last_line)
        return {"start_cp": start, "end_cp": end, "selected_text": text[start:end],
                "revision_no": s.manuscript.revision_no}


@router.post("/api/revisions/undo")
async def undo(body: TextRequest, request: Request):
    s = request.app.state.session
    await s.undo_manuscript(body.expected_revision_no)
    return await s.manuscript_view()


@router.get("/api/revisions")
async def history(request: Request):
    return {"revisions": await request.app.state.session.history()}


@router.get("/api/manuscript/export")
async def export_text(request: Request):
    result = await request.app.state.session.manuscript_view()
    return Response(result["text"].encode("utf-8"), media_type="text/plain; charset=utf-8",
                    headers={"Content-Disposition": 'attachment; filename="manuscript.txt"'})
