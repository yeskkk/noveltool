from importlib.resources import files
from fastapi import APIRouter,Request
from fastapi.responses import HTMLResponse
from . import __version__
from .generation import GenerationRequest
from .drafts import DraftInput,CommitDraft

router=APIRouter()

@router.get('/generation',response_class=HTMLResponse)
async def page():
    return files('noveltool').joinpath('static/generation.html').read_text(encoding='utf-8').replace('{{VERSION}}',__version__)

@router.get('/api/generation')
async def list_tasks(request:Request):
    return {'tasks':await request.app.state.session.generation.list()}

@router.post('/api/generation',status_code=202)
async def generate(body:GenerationRequest,request:Request):
    return await request.app.state.session.generation.start(body,transport=request.app.state.llm_transport)

@router.get('/api/generation/{task_id}')
async def view(task_id:str,request:Request):
    return await request.app.state.session.generation.view(task_id)

@router.get('/api/generation/{task_id}/context')
async def context(task_id:str,request:Request):
    return await request.app.state.session.generation.context_view(task_id)

@router.post('/api/generation/{task_id}/resume',status_code=202)
async def resume(task_id:str,request:Request):
    return await request.app.state.session.generation.resume(task_id,transport=request.app.state.llm_transport)

@router.post('/api/generation/{task_id}/regenerate/{index}',status_code=202)
async def regenerate(task_id:str,index:int,request:Request):
    return await request.app.state.session.generation.resume(task_id,index=index,transport=request.app.state.llm_transport)

@router.post('/api/generation/{task_id}/pause')
async def pause(task_id:str,request:Request):
    return await request.app.state.session.generation.pause(task_id)

@router.put('/api/generation/{task_id}/draft')
async def draft(task_id:str,body:DraftInput,request:Request):
    return await request.app.state.session.generation.drafts.save(task_id,body)

@router.post('/api/generation/{task_id}/commit')
async def commit(task_id:str,body:CommitDraft,request:Request):
    return await request.app.state.session.generation.drafts.commit(task_id,body)
