from importlib.resources import files
from fastapi import APIRouter, Request, Query
from fastapi.responses import HTMLResponse
from . import __version__
from .context import ContextRequest

router=APIRouter()

@router.get('/context',response_class=HTMLResponse)
async def page():
    return files('noveltool').joinpath('static/context.html').read_text(encoding='utf-8').replace('{{VERSION}}',__version__)

@router.get('/api/state')
async def state(request:Request,at_cp:int|None=Query(default=None,ge=0)):
    return await request.app.state.session.state_reducer.view(at_cp)

@router.post('/api/context/preview')
async def preview(body:ContextRequest,request:Request):
    return await request.app.state.session.context.preview(body)
