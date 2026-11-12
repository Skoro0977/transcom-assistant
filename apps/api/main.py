from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .assistant import router as assistant_router
from .auth import Identity, identity, project_access, public_user
from .auth import router as auth_router
from .contracts import ProjectInput
from .db import Membership, Project, session_db
from .documents import Document, DocumentVersion
from .documents import router as documents_router
from .knowledge import router as knowledge_router
from .lifecycle import router as lifecycle_router
from .limits import BodySizeLimit
from .planning import router as planning_router
from .risks import router as risks_router
from .search import router as search_router
from .settings import ROOT, settings
from .sources import router as sources_router
from .worker import start as start_worker


@asynccontextmanager
async def lifespan(app):
    worker = start_worker() if settings.worker_enabled else None
    yield
    if worker:
        worker[0].set()
        worker[1].join(timeout=5)


app = FastAPI(title='Транскоминжиниринг', version='0.1.0', lifespan=lifespan)
app.add_middleware(BodySizeLimit)


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError):
    # Pydantic's input can contain the whole request, including passwords/tokens.
    return JSONResponse({'detail': [{key: item[key] for key in ('type', 'loc', 'msg') if key in item}
                                    for item in error.errors()]}, status_code=422)


app.include_router(auth_router)
app.include_router(documents_router)
app.include_router(knowledge_router)
app.include_router(lifecycle_router)
app.include_router(search_router)
app.include_router(planning_router)
app.include_router(risks_router)
app.include_router(assistant_router)
app.include_router(sources_router)


@app.middleware('http')
async def guard(request: Request, call_next):
    origin = request.headers.get('origin')
    if (request.method not in {'GET', 'HEAD', 'OPTIONS'} and origin
            and origin not in settings.allowed_origins.split(',')):
        return JSONResponse({'detail': 'Источник запроса не разрешен'}, status_code=403)
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'same-origin'
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response

@app.get('/api/health')
def health():
    return {'status': 'ok', 'version': app.version}


@app.get('/api/projects')
def projects(who: Identity = Depends(identity), db: Session = Depends(session_db)):
    rows = db.scalars(select(Project).join(Membership).where(Membership.user_id == who.user.id)).all()
    return [{'id': p.id, 'title': p.title, 'description': p.description, 'version': p.version} for p in rows]


@app.post('/api/projects', status_code=201)
def create_project(body: ProjectInput, who: Identity = Depends(identity), db: Session = Depends(session_db)):
    project = Project(**body.model_dump())
    db.add(project)
    db.flush()
    db.add(Membership(project_id=project.id, user_id=who.user.id, can_edit=True, can_review=True))
    db.commit()
    return {'id': project.id, 'title': project.title, 'version': project.version}


@app.get('/api/projects/{project_id}/overview')
def overview(project_id: str, who: Identity = Depends(identity), db: Session = Depends(session_db)):
    project = project_access(project_id, db, who)
    members = db.scalars(select(Membership).where(Membership.project_id == project_id)).all()
    from .db import User
    counts = dict(db.execute(select(DocumentVersion.status, func.count()).join(Document,
        Document.id == DocumentVersion.document_id).where(Document.project_id == project_id,
        Document.lifecycle == 'active', DocumentVersion.number == Document.current_number)
        .group_by(DocumentVersion.status)).all())
    return {'id': project.id, 'title': project.title, 'description': project.description,
            'version': project.version, 'members': [public_user(db.get(User, m.user_id)) for m in members],
            'document_counts': counts, 'gigachat_configured': bool(settings.gigachat_auth_key.get_secret_value())}


frontend = ROOT / 'apps' / 'web' / 'dist'
if frontend.is_dir():
    app.mount('/', StaticFiles(directory=frontend, html=True), name='frontend')
