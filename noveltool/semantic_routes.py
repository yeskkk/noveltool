"""Explicit sync controls; starting/resuming is never a GET side effect."""
from importlib.resources import files
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from pydantic import Field
from . import __version__
from .settings import Strict
from .chunker import PlanSettings

router=APIRouter()

class SyncRequest(Strict):
    expected_revision_no:int=Field(ge=1)
    settings:PlanSettings|None=None

@router.get('/sync',response_class=HTMLResponse)
async def page():
    return files('noveltool').joinpath('static/sync.html').read_text(encoding='utf-8').replace('{{VERSION}}',__version__)

@router.get('/api/sync')
async def state(request:Request):
    s=request.app.state.session
    return {'coverage':await s.semantic.status(),**await s.jobs.view()}

@router.post('/api/sync',status_code=202)
async def start(body:SyncRequest,request:Request):
    return await request.app.state.session.semantic.start(body.expected_revision_no,
        settings=body.settings,transport=request.app.state.llm_transport)
