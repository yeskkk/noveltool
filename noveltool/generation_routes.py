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
    session=request.app.state.session
    receipt=await session.generation.drafts.commit(task_id,body)
    if body.sync_after_commit and not receipt['already_committed']:
        try:
            receipt['sync_job']=await session.semantic.start(receipt['current_revision_no'],transport=request.app.state.llm_transport)
            receipt['semantic_sync']='running'
            receipt['notice']='正文已经保存，增量设定同步已启动；各块保存检查点。同步失败也不会回滚正文。'
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('正文已保存，但无法启动语义同步: %s',exc)
            receipt['notice']='正文已经保存；设定同步尚未启动，请到同步页面检查模型配置或已有任务，再手动重试。'
    if body.check_after_commit and not receipt['already_committed'] and receipt['task']['task_type']=='rewrite':
        from .consistency import CheckRequest
        try:
            wait_for=session.jobs.task if receipt.get('sync_job') else None
            check=await session.consistency.start(CheckRequest(expected_revision_no=receipt['current_revision_no']),
                transport=request.app.state.llm_transport,wait_for=wait_for)
            receipt['consistency_job']=check['job']['id']
            receipt['notice']+=' 后文检查已安排，可在“一致性检查”查看；它不会自动改后文。'
        except Exception as exc:
            import logging
            logging.getLogger(__name__).warning('正文已保存，但检查尚未启动: %s',exc)
            receipt['notice']+=' 后文检查尚未启动；请在检查页面明确启动。'
    return receipt

@router.post('/api/generation/{task_id}/preview')
async def preview(task_id:str,body:CommitDraft,request:Request):
    return await request.app.state.session.generation.drafts.preview(task_id,body)
