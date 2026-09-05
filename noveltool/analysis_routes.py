"""Explicit single-chunk analysis; reading results never triggers model work."""
from importlib.resources import files
from typing import Literal
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, Field
from . import __version__

router = APIRouter()

class AnalyzeRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    plan_id: str = Field(min_length=1, max_length=64)
    expected_revision_no: int = Field(ge=1)
    pass_type: Literal["facts", "links", "narrative"] = "facts"

@router.get("/analysis", response_class=HTMLResponse)
async def page():
    return files("noveltool").joinpath("static/analysis.html").read_text(encoding="utf-8").replace("{{VERSION}}", __version__)

@router.get("/api/analysis/runs")
async def runs(request: Request, plan_id: str | None = None):
    return {"runs": await request.app.state.session.analysis.list_runs(plan_id)}

@router.get("/api/analysis/runs/{run_id}")
async def run_view(run_id: str, request: Request):
    return await request.app.state.session.analysis.run_view(run_id)

@router.post("/api/analysis/chunks/{ordinal}/preview")
async def preview(ordinal: int, body: AnalyzeRequest, request: Request):
    return await request.app.state.session.analysis.preview(body.plan_id, ordinal, body.expected_revision_no, body.pass_type)

@router.post("/api/analysis/chunks/{ordinal}/run")
async def analyze(ordinal: int, body: AnalyzeRequest, request: Request):
    return await request.app.state.session.analysis.run_chunk(body.plan_id, ordinal, body.expected_revision_no,
                                                           transport=request.app.state.llm_transport, pass_type=body.pass_type)


class JobRequest(AnalyzeRequest):
    passes: list[Literal["facts", "links", "narrative"]] = Field(default_factory=lambda: ["facts", "links", "narrative"], min_length=1, max_length=3)
    retry_failed: bool = True


@router.post("/api/analysis/jobs")
async def start_job(body: JobRequest, request: Request):
    return await request.app.state.session.jobs.start(body.plan_id, body.expected_revision_no, body.passes,
                      body.retry_failed, transport=request.app.state.llm_transport)


@router.get("/api/analysis/jobs/latest")
async def latest_job(request: Request):
    return await request.app.state.session.jobs.view()


@router.get("/api/analysis/jobs/{job_id}")
async def job(job_id: str, request: Request):
    return await request.app.state.session.jobs.view(job_id)


@router.post("/api/analysis/jobs/{job_id}/pause")
async def pause_job(job_id: str, request: Request):
    return await request.app.state.session.jobs.pause(job_id)


@router.get("/api/knowledge")
async def knowledge(request: Request):
    return await request.app.state.session.knowledge.view()
