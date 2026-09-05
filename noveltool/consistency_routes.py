from importlib.resources import files
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from . import __version__
from .consistency import CheckRequest, IssueUpdate
from .manuscript import ManuscriptError
from .structured_llm import strict_loads
router=APIRouter()

@router.get('/consistency',response_class=HTMLResponse)
async def page():
    return files('noveltool').joinpath('static/consistency.html').read_text(encoding='utf-8').replace('{{VERSION}}',__version__)

@router.get('/api/consistency')
async def jobs(request:Request):
    s=request.app.state.session
    return {'jobs':await s.consistency.list(),'revision_no':s.manuscript.revision_no}

@router.post('/api/consistency',status_code=202)
async def start(body:CheckRequest,request:Request):
    return await request.app.state.session.consistency.start(body,transport=request.app.state.llm_transport)

@router.get('/api/consistency/{job_id}')
async def view(job_id:str,request:Request):
    return await request.app.state.session.consistency.view(job_id)

@router.post('/api/consistency/{job_id}/resume',status_code=202)
async def resume(job_id:str,request:Request):
    return await request.app.state.session.consistency.resume(job_id,transport=request.app.state.llm_transport)

@router.post('/api/consistency/{job_id}/pause')
async def pause(job_id:str,request:Request):
    return await request.app.state.session.consistency.pause(job_id)

@router.put('/api/consistency/issues/{issue_id}')
async def issue(issue_id:str,body:IssueUpdate,request:Request):
    return await request.app.state.session.consistency.update_issue(issue_id,body)

@router.get('/api/consistency/{job_id}/input/{ordinal}')
async def input_view(job_id:str,ordinal:int,request:Request):
    s=request.app.state.session
    async with s.lock:
        row=s.store.connection.execute('SELECT messages_json,refs_json,input_estimate FROM consistency_units WHERE job_id=? AND ordinal=?',(job_id,ordinal)).fetchone()
        if row is None:raise ManuscriptError('检查单元不存在')
        return {'messages':strict_loads(row['messages_json']),'refs':strict_loads(row['refs_json']),'input_estimate':row['input_estimate']}
