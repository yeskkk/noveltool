"""HTTP imports use a bounded raw request body, not multipart/extra dependencies."""
from importlib.resources import files
from typing import Literal
from fastapi import APIRouter,Request,Query
from fastapi.responses import HTMLResponse,Response
from pydantic import Field
from . import __version__
from .import_text import decode_import
from .manuscript_routes import TextRequest
from .chunker import PlanSettings
router=APIRouter()

class CommitRequest(TextRequest):
    preview_id:str=Field(min_length=1,max_length=64)

class PlanRequest(TextRequest):
    settings:PlanSettings=Field(default_factory=PlanSettings)

@router.get('/import',response_class=HTMLResponse)
async def page():
    return files('noveltool').joinpath('static/import.html').read_text(encoding='utf-8').replace('{{VERSION}}',__version__)

@router.post('/api/import/preview')
async def preview(request:Request,filename:str=Query(min_length=1,max_length=255),
                  encoding:Literal['auto','utf-8','utf-8-sig','utf-16','utf-16-le','utf-16-be','gb18030','gbk']='auto',
                  mode:Literal['auto','line','blankline']='auto'):
    raw=await request.body()
    result=decode_import(raw,filename,encoding,mode)
    return await request.app.state.session.imports.remember(result)

@router.post('/api/import/commit')
async def commit(body:CommitRequest,request:Request):
    return await request.app.state.session.imports.commit(body.preview_id,body.expected_revision_no)

@router.post('/api/import/plan')
async def plan(body:PlanRequest,request:Request):
    return await request.app.state.session.imports.create_plan(body.settings,body.expected_revision_no)

@router.get('/api/import/plan')
async def current_plan(request:Request):
    return await request.app.state.session.imports.current_plan()

@router.get('/api/import/sources')
async def sources(request:Request):
    return {'sources':await request.app.state.session.imports.sources()}

@router.get('/api/import/sources/{source_id}/raw')
async def original(source_id:str,request:Request):
    raw=await request.app.state.session.imports.original(source_id)
    return Response(raw,media_type='application/octet-stream',
                    headers={'Content-Disposition':'attachment; filename="original-source.txt"'})
