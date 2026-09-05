from importlib.resources import files
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from . import __version__
from .ideas import IdeaRequest, ProposalDraft, ProposalAccept

router = APIRouter()

@router.get('/ideas', response_class=HTMLResponse)
async def page():
    return files('noveltool').joinpath('static/ideas.html').read_text(encoding='utf-8').replace('{{VERSION}}',__version__)

@router.get('/api/ideas')
async def list_proposals(request: Request):
    return {'proposals': await request.app.state.session.ideas.list()}

@router.post('/api/ideas')
async def generate(body: IdeaRequest, request: Request):
    return await request.app.state.session.ideas.generate(body,transport=request.app.state.llm_transport)

@router.get('/api/ideas/{proposal_id}')
async def get_proposal(proposal_id: str, request: Request):
    return await request.app.state.session.ideas.get(proposal_id)

@router.put('/api/ideas/{proposal_id}/draft')
async def save_draft(proposal_id: str, body: ProposalDraft, request: Request):
    return await request.app.state.session.ideas.save_draft(proposal_id,body)

@router.post('/api/ideas/{proposal_id}/accept')
async def accept(proposal_id: str, body: ProposalAccept, request: Request):
    return await request.app.state.session.ideas.accept(proposal_id,body)
