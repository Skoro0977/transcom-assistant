from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from .auth import Identity, identity, project_access, public_user
from .auth import router as auth_router
from .contracts import ProjectInput
from .db import Membership, Project, session_db
from .documents import router as documents_router
from .limits import BodySizeLimit
from .settings import settings
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
app.include_router(auth_router)
app.include_router(documents_router)


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
    return {'id': project.id, 'title': project.title, 'description': project.description,
            'version': project.version, 'members': [public_user(db.get(User, m.user_id)) for m in members]}
