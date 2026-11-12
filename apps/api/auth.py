import hashlib
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from argon2 import PasswordHasher
from argon2.exceptions import VerificationError
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from .contracts import LoginInput
from .db import AuthSession, LoginAttempt, Membership, Project, User, session_db, utcnow
from .settings import settings

router = APIRouter(prefix='/api/auth', tags=['auth'])
passwords = PasswordHasher()
dummy_hash = passwords.hash(secrets.token_urlsafe(24))
COOKIE = 'transcom_session'


def digest(value: str):
    return hashlib.sha256(value.encode()).hexdigest()


def public_user(user: User):
    return {'id': user.id, 'username': user.username, 'role': user.role}


@dataclass
class Identity:
    user: User
    session: AuthSession


def identity(request: Request, db: Session = Depends(session_db)):
    token = request.cookies.get(COOKIE, '')
    record = db.get(AuthSession, digest(token)) if token else None
    user = db.get(User, record.user_id) if record and record.expires_at > utcnow() else None
    if not user or not user.active:
        raise HTTPException(401, 'Нужен вход в систему')
    if (request.method not in {'GET', 'HEAD', 'OPTIONS'}
            and not hmac.compare_digest(request.headers.get('X-CSRF-Token', ''), record.csrf_token)):
        raise HTTPException(403, 'Недействительное подтверждение сессии')
    return Identity(user, record)


def project_access(project_id: str, db: Session, who: Identity, permission: str = 'view'):
    if not who.user.active:
        raise HTTPException(401, 'Учетная запись отключена')
    membership = db.get(Membership, (who.user.id, project_id))
    if not membership:
        raise HTTPException(404, 'Проект не найден')
    if permission == 'edit' and not membership.can_edit:
        raise HTTPException(403, 'Нет права изменения проекта')
    if permission == 'review' and not membership.can_review:
        raise HTTPException(403, 'Нет права подтверждения')
    project = db.get(Project, project_id)
    if project is None:
        raise HTTPException(404, 'Проект не найден')
    return project


def login_limit(db: Session, host: str, username: str):
    if db.get_bind().dialect.name == 'sqlite':
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    window = int(time.time()) // 300
    for key, limit in [(host, 40), (host + ':' + username, 8)]:
        hashed = digest(key)
        row = db.get(LoginAttempt, hashed)
        if row is None:
            row = LoginAttempt(key=hashed, count=0, window=window)
            db.add(row)
        if row.window != window:
            row.count, row.window = 0, window
        if row.count >= limit:
            raise HTTPException(429, 'Слишком много попыток входа, повторите позже')
        row.count += 1
    db.commit()


@router.post('/login')
def login(body: LoginInput, request: Request, response: Response, db: Session = Depends(session_db)):
    login_limit(db, request.client.host if request.client else 'local', body.username)
    user = db.scalar(select(User).where(User.username == body.username))
    try:
        valid = passwords.verify(user.password_hash if user else dummy_hash, body.password)
    except VerificationError:
        valid = False
    if not valid or user is None or not user.active:
        raise HTTPException(401, 'Неверный логин или пароль')
    token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    expiry = datetime.now(UTC) + timedelta(hours=settings.session_hours)
    old = request.cookies.get(COOKIE)
    old_record = db.get(AuthSession, digest(old)) if old else None
    if old_record:
        db.delete(old_record)
    db.add(AuthSession(token_hash=digest(token), csrf_token=csrf, user_id=user.id, expires_at=expiry.isoformat()))
    db.commit()
    response.set_cookie(COOKIE, token, httponly=True, secure=settings.cookie_secure,
                        samesite='strict', max_age=settings.session_hours * 3600, path='/')
    return {'user': public_user(user), 'csrf_token': csrf}


@router.get('/me')
def me(who: Identity = Depends(identity)):
    return {'user': public_user(who.user), 'csrf_token': who.session.csrf_token}


@router.post('/logout')
def logout(response: Response, who: Identity = Depends(identity), db: Session = Depends(session_db)):
    db.delete(who.session)
    db.commit()
    response.delete_cookie(COOKIE, path='/', secure=settings.cookie_secure, httponly=True, samesite='strict')
    return {'ok': True}
