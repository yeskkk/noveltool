from importlib.resources import files
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse
from starlette.background import BackgroundTask
from . import __version__
from .manuscript_routes import TextRequest

router = APIRouter()

@router.get('/maintenance', response_class=HTMLResponse)
async def page():
    return files('noveltool').joinpath('static/maintenance.html').read_text(encoding='utf-8').replace('{{VERSION}}', __version__)

@router.get('/api/maintenance')
async def overview(request: Request):
    return await request.app.state.session.maintenance.overview()

@router.post('/api/maintenance/check')
async def check(body: TextRequest, request: Request):
    return await request.app.state.session.maintenance.check(body.expected_revision_no)

@router.post('/api/maintenance/backup')
async def backup(body: TextRequest, request: Request):
    path = await request.app.state.session.maintenance.backup(body.expected_revision_no)
    return FileResponse(path, filename=f'noveltool-revision-{body.expected_revision_no}-backup.sqlite3',
                        media_type='application/octet-stream', background=BackgroundTask(path.unlink, missing_ok=True))

@router.get('/api/maintenance/revisions/{number}')
async def revision(number: int, request: Request):
    return await request.app.state.session.maintenance.revision(number)

@router.get('/api/maintenance/runs/{run_id}')
async def run(run_id: str, request: Request):
    return await request.app.state.session.maintenance.run(run_id)
