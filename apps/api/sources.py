"""Read-only folder and IMAP feeds into the existing ingestion pipeline."""
import base64
import hashlib
import imaplib
import os
import re
import ssl
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

import httpx
from cryptography.fernet import InvalidToken
from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field, SecretStr, model_validator
from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint, delete, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import db, vault
from .auth import Identity, identity, project_access
from .contracts import InputModel
from .documents import EXTENSIONS, MAX_BYTES, Document, ingest
from .settings import DATA

router = APIRouter(tags=['sources'])
INBOX = DATA / 'inbox'
IMAP_HOSTS = {'gmail': 'imap.gmail.com', 'yandex': 'imap.yandex.ru'}


class SourceError(Exception):
    pass


class Source(db.Base):
    __tablename__ = 'sources'
    __table_args__ = (UniqueConstraint('project_id', 'name'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=db.uid)
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    secret_ref: Mapped[str] = mapped_column(String(32), default='')
    enabled: Mapped[bool] = mapped_column(default=True)
    version: Mapped[int] = mapped_column(default=1)
    status: Mapped[str] = mapped_column(String(20), default='queued')
    state: Mapped[dict] = mapped_column(JSON, default=dict)
    notice: Mapped[str] = mapped_column(Text, default='')
    last_run: Mapped[str | None] = mapped_column(String(40), nullable=True)
    next_run: Mapped[str] = mapped_column(String(40), default=db.utcnow)
    updated_at: Mapped[str] = mapped_column(String(40), default=db.utcnow)


class SourceFile(db.Base):
    __tablename__ = 'source_files'
    __table_args__ = (UniqueConstraint('source_id', 'relative_path'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=db.uid)
    source_id: Mapped[str] = mapped_column(ForeignKey('sources.id'), index=True)
    relative_path: Mapped[str] = mapped_column(Text)
    signature: Mapped[str] = mapped_column(String(100))
    checksum: Mapped[str] = mapped_column(String(64))
    document_id: Mapped[str | None] = mapped_column(ForeignKey('documents.id'), nullable=True)


class SourceInput(InputModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal['folder', 'gmail', 'yandex']
    path: str = Field(default='', max_length=1000)
    username: str = Field(default='', max_length=254)
    folder: str = Field(default='INBOX', min_length=1, max_length=255)
    auth_mode: Literal['app_password', 'gmail_oauth'] = 'app_password'
    password: SecretStr = SecretStr('')
    client_id: str = Field(default='', max_length=500)
    client_secret: SecretStr = SecretStr('')
    refresh_token: SecretStr = SecretStr('')
    enabled: bool = True

    @model_validator(mode='after')
    def valid(self):
        if any(ord(c) < 32 for c in self.username + self.folder):
            raise ValueError('Недопустимые символы имени ящика или папки')
        if self.kind != 'folder' and (not self.username or '@' not in self.username):
            raise ValueError('Укажите полный адрес ящика')
        if self.auth_mode == 'gmail_oauth' and self.kind != 'gmail':
            raise ValueError('Этот режим OAuth поддерживается только для Gmail')
        if any(len(value.get_secret_value()) > 4000 or '\r' in value.get_secret_value() or '\n' in value.get_secret_value()
               for value in (self.password, self.client_secret, self.refresh_token)):
            raise ValueError('Некорректный формат секрета')
        return self


class SourceEdit(SourceInput):
    expected_version: int = Field(ge=1)


def inbox_path(project_id):
    requested = (INBOX / project_id).absolute()
    root = requested.resolve()
    if root != requested or (os.name == 'nt' and root.drive.upper() != 'D:'):
        raise HTTPException(422, 'Входящая папка проекта не должна перенаправляться ссылкой в другой каталог')
    root.mkdir(parents=True, exist_ok=True)
    return root


def folder_path(project_id, raw=''):
    root = inbox_path(project_id)
    path = Path(raw) if raw else root
    if not path.is_absolute():
        path = root / path
    path = path.resolve()
    if not path.is_relative_to(root) or (os.name == 'nt' and path.drive.upper() != 'D:'):
        raise HTTPException(422, 'Папка должна находиться внутри входящей папки этого проекта на D')
    return path


def public(source):
    return {**{key: getattr(source, key) for key in ('id', 'name', 'kind', 'enabled', 'version',
            'status', 'notice', 'last_run', 'next_run')},
            'config': {key: value for key, value in source.config.items() if key != 'epoch'},
            'credentials_configured': bool(source.secret_ref)}


def save_source(session, project_id, who, body, source=None):
    db.write_lock(session)
    existing = session.scalar(select(Source).where(Source.project_id == project_id, Source.name == body.name))
    if existing and (source is None or existing.id != source.id):
        raise HTTPException(409, 'Источник с этим названием уже существует')
    if source and (source.version != body.expected_version or source.kind != body.kind):
        raise HTTPException(409, 'Источник изменился или выбран другой тип')
    if body.kind == 'folder':
        path = folder_path(project_id, body.path)
        path.mkdir(parents=True, exist_ok=True)
        config = {'path': str(path)}
    else:
        config = {'username': body.username, 'folder': body.folder, 'auth_mode': body.auth_mode}
    identity_keys = ('path',) if body.kind == 'folder' else ('username', 'folder')
    same_location = source and all(source.config.get(key) == config.get(key) for key in identity_keys)
    config['epoch'] = source.config.get('epoch', db.uid()) if same_location else db.uid()
    old_ref = source.secret_ref if source else ''
    new_ref = old_ref
    if body.kind != 'folder':
        same_account = source and source.config.get('username') == body.username and source.config.get('auth_mode') == body.auth_mode
        provided = ({'password': body.password.get_secret_value()} if body.auth_mode == 'app_password' else
                    {'client_id': body.client_id, 'client_secret': body.client_secret.get_secret_value(),
                     'refresh_token': body.refresh_token.get_secret_value()})
        if body.kind == 'gmail' and body.auth_mode == 'app_password':
            provided['password'] = provided['password'].replace(' ', '')
        if any(provided.values()) or not same_account or not old_ref:
            credentials = {}
            if same_account and old_ref and not all(provided.values()):
                try:
                    credentials = vault.get(old_ref)
                except (OSError, ValueError, InvalidToken):
                    raise HTTPException(422, 'Учетные данные недоступны. Заполните все поля подключения заново.') from None
            credentials.update({key: value for key, value in provided.items() if value})
            if not all(credentials.get(key) for key in provided):
                raise HTTPException(422, 'Нужно указать учетные данные для этого подключения')
            new_ref = vault.put(credentials)
    if source is None:
        source = Source(project_id=project_id, owner_id=who.user.id, name=body.name, kind=body.kind)
        session.add(source)
    elif source.config.get('epoch') != config['epoch']:
        session.execute(delete(SourceFile).where(SourceFile.source_id == source.id))
        source.state = {}
    source.name, source.config, source.secret_ref, source.enabled = body.name, config, new_ref, body.enabled
    source.owner_id = who.user.id
    source.version = (source.version or 0) + 1
    source.status, source.notice, source.next_run, source.updated_at = 'queued', '', db.utcnow(), db.utcnow()
    try:
        from .knowledge import Audit
        session.flush()
        session.add(Audit(project_id=project_id, user_id=who.user.id, action='source_configured',
            entity_id=source.id, details={'kind': source.kind, 'enabled': source.enabled, 'version': source.version}))
        session.commit()
    except Exception:
        if new_ref != old_ref:
            vault.discard(new_ref)
        raise
    if new_ref != old_ref:
        try:
            vault.discard(old_ref)
        except OSError:
            pass
    return public(source)


@router.get('/api/projects/{project_id}/sources')
def list_sources(project_id: str, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    project_access(project_id, session, who)
    return {'items': [public(s) for s in session.scalars(select(Source).where(Source.project_id == project_id))],
            'inbox_path': str(inbox_path(project_id))}


@router.post('/api/projects/{project_id}/sources', status_code=201)
def create_source(project_id: str, body: SourceInput, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    project_access(project_id, session, who, 'edit')
    return save_source(session, project_id, who, body)


@router.patch('/api/sources/{source_id}')
def edit_source(source_id: str, body: SourceEdit, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    db.write_lock(session)
    source = session.get(Source, source_id)
    if not source:
        raise HTTPException(404, 'Источник не найден')
    project_access(source.project_id, session, who, 'edit')
    return save_source(session, source.project_id, who, body, source)


@router.post('/api/sources/{source_id}/sync')
def sync_source(source_id: str, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    db.write_lock(session)
    source = session.get(Source, source_id)
    if not source:
        raise HTTPException(404, 'Источник не найден')
    project_access(source.project_id, session, who, 'edit')
    if not source.enabled:
        raise HTTPException(409, 'Источник выключен')
    if source.status != 'running':
        source.status, source.next_run = 'queued', db.utcnow()
        if source.kind == 'folder':
            source.state = {'force_hash': True}
    session.commit()
    return public(source)


def permitted(session, source):
    user = session.get(db.User, source.owner_id)
    member = session.get(db.Membership, (source.owner_id, source.project_id))
    return user and user.active and member and member.can_edit


def active_source(session, source_id, version, stamp):
    source = session.get(Source, source_id)
    return source if (source and source.enabled and source.version == version and source.status == 'running'
                      and source.updated_at == stamp and permitted(session, source)) else None


def folder_files(root):
    visited = 0
    for parent, directories, files in os.walk(root, followlinks=False):
        directories[:] = sorted(d for d in directories if not d.startswith('.') and safe_entry(Path(parent) / d, root))
        for name in sorted(files):
            visited += 1
            if visited > 10000:
                raise SourceError('В папке больше 10000 файлов. Разделите ее на несколько входящих папок.')
            path = Path(parent) / name
            if path.suffix.lower() in EXTENSIONS and safe_entry(path, root):
                yield path


def safe_entry(path, root):
    return (not path.is_symlink() and not (getattr(path.lstat(), 'st_file_attributes', 0) & 0x400)
            and path.resolve().is_relative_to(root))


def poll_folder(snapshot):
    source_id, version, project_id, owner_id, config, state, stamp = snapshot
    root = folder_path(project_id, config['path'])
    if not root.is_dir():
        raise SourceError('Входящая папка не существует')
    processed, more, notice = 0, False, ''
    for path in folder_files(root):
        stat = path.stat()
        signature = f'{stat.st_size}:{stat.st_mtime_ns}'
        if stat.st_size > MAX_BYTES:
            notice = 'Пропущены файлы больше 100 МиБ'
            continue
        if datetime.now(UTC).timestamp() - stat.st_mtime < 3:
            continue
        relative = path.relative_to(root).as_posix()
        with db.SessionLocal() as session:
            source_file = session.scalar(select(SourceFile).where(SourceFile.source_id == source_id, SourceFile.relative_path == relative))
            if source_file and source_file.signature == signature and not state.get('force_hash'):
                continue
            if source_file and source_file.document_id:
                document = session.get(Document, source_file.document_id)
                if document.lifecycle == 'archived':
                    notice = 'Измененный файл связан с архивным документом. Сначала восстановите документ в работу.'
                    continue
        if processed >= 5:
            more = True
            break
        fd, raw = tempfile.mkstemp(dir=DATA / 'tmp', suffix=path.suffix)
        os.close(fd)
        temporary = Path(raw)
        try:
            with path.open('rb') as incoming, temporary.open('wb') as copied:
                size = 0
                while block := incoming.read(1024 * 1024):
                    size += len(block)
                    if size > MAX_BYTES:
                        raise SourceError('Файл вырос больше 100 МиБ при чтении. Подготовьте меньший файл.')
                    copied.write(block)
            after = path.stat()
            if f'{after.st_size}:{after.st_mtime_ns}' != signature or temporary.stat().st_size > MAX_BYTES:
                continue
            with temporary.open('rb') as stream:
                checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
            with db.SessionLocal() as session:
                db.write_lock(session)
                source = active_source(session, source_id, version, stamp)
                if not source:
                    return False, notice
                source_file = session.scalar(select(SourceFile).where(SourceFile.source_id == source_id, SourceFile.relative_path == relative))
                document = session.get(Document, source_file.document_id) if source_file and source_file.document_id else None
                if document and document.lifecycle == 'archived':
                    continue
                target = document if document and document.lifecycle == 'active' else None
                if source_file and source_file.checksum == checksum:
                    source_file.signature = signature
                    session.commit()
                    continue
                if source_file is None:
                    source_file = SourceFile(source_id=source_id, relative_path=relative)

                def receipt(result, row=source_file, sig=signature, sha=checksum, transaction=session):
                    row.signature, row.checksum = sig, sha
                    row.document_id = result.get('id')
                    transaction.add(row)

                event = hashlib.sha256(f'{config.get("epoch", "legacy")}:{relative}:{signature}:{checksum}:{target.version if target else 0}'.encode()).hexdigest()
                ingest(session, project_id, owner_id, temporary, path.name, channel='folder',
                    external_id=f'{source_id}:{event}', source_name=relative,
                    document_id=target.id if target else None, expected_version=target.version if target else None,
                    before_commit=receipt)
            processed += 1
        finally:
            temporary.unlink(missing_ok=True)
    return more, notice


def utf7(value):
    def encode(match):
        return '&' + base64.b64encode(match[0].encode('utf-16-be')).decode().rstrip('=').replace('/', ',') + '-'
    return re.sub(r'[^\x20-\x7e]+', encode, value.replace('&', '&-'))


def mail_token(credentials):
    with httpx.Client(trust_env=False, timeout=20, follow_redirects=False) as client:
        response = client.post('https://oauth2.googleapis.com/token', data={'grant_type': 'refresh_token',
            'refresh_token': credentials['refresh_token'], 'client_id': credentials['client_id'], 'client_secret': credentials['client_secret']})
    if response.status_code != 200 or len(response.content) > 64000:
        raise ValueError('OAuth failed')
    token = response.json().get('access_token')
    if not isinstance(token, str) or not token or len(token) > 16000:
        raise ValueError('Invalid OAuth response')
    return token


def poll_mail(snapshot):
    source_id, version, project_id, owner_id, config, old_state, stamp = snapshot
    with db.SessionLocal() as session:
        source = active_source(session, source_id, version, stamp)
        if not source:
            return False, '', old_state
        credentials, kind = vault.get(source.secret_ref), source.kind
    mail = imaplib.IMAP4_SSL(IMAP_HOSTS[kind], 993, ssl_context=ssl.create_default_context(), timeout=15)
    try:
        if config['auth_mode'] == 'gmail_oauth':
            token = mail_token(credentials)
            mail.authenticate('XOAUTH2', lambda challenge: b'' if challenge else
                              f'user={config["username"]}\x01auth=Bearer {token}\x01\x01'.encode())
        else:
            mail.login(config['username'], credentials['password'])
        folder = utf7(config['folder']).replace('\\', '\\\\').replace('"', '\\"')
        if mail.select('"' + folder + '"', readonly=True)[0] != 'OK':
            raise ValueError('Mailbox select failed')
        _, data = mail.response('UIDVALIDITY')
        validity = data[0].decode() if data and data[0] else ''
        if not validity.isdigit():
            raise ValueError('Missing UIDVALIDITY')
        last_uid = old_state.get('uid', 0) if old_state.get('uidvalidity') == validity else 0
        typ, data = mail.uid('search', None, f'UID {last_uid + 1}:*')
        if typ != 'OK' or not data:
            raise ValueError('Mailbox search failed')
        uids = sorted(int(item) for item in data[0].split() if int(item) > last_uid)
        notice = ''
        for uid in uids[:5]:
            typ, sizes = mail.uid('fetch', str(uid), '(RFC822.SIZE)')
            size = re.search(rb'RFC822.SIZE (\d+)', b' '.join(item for item in sizes if isinstance(item, bytes)))
            if typ != 'OK' or not size:
                raise ValueError('Mailbox size failed')
            if int(size[1]) > MAX_BYTES:
                notice = 'Письмо больше 100 МиБ пропущено, требуется отдельная подготовка'
            else:
                typ, messages = mail.uid('fetch', str(uid), '(BODY.PEEK[])')
                raw = next((item[1] for item in messages if isinstance(item, tuple)), None)
                if typ != 'OK' or not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
                    raise ValueError('Mailbox fetch failed')
                target = DATA / 'tmp' / (db.uid() + '.eml')
                try:
                    target.write_bytes(raw)
                    with db.SessionLocal() as session:
                        db.write_lock(session)
                        source = active_source(session, source_id, version, stamp)
                        if not source:
                            return False, '', old_state

                        def receipt(_, row=source, source_uid=uid):
                            row.state = {'uidvalidity': validity, 'uid': source_uid}

                        ingest(session, project_id, owner_id, target, f'Письмо {uid}.eml', channel='imap',
                            external_id=f'{source_id}:{config.get("epoch", "legacy")}:{validity}:{uid}',
                            source_name=config['folder'], before_commit=receipt)
                finally:
                    target.unlink(missing_ok=True)
            last_uid = uid
            old_state = {'uidvalidity': validity, 'uid': last_uid}
            with db.SessionLocal() as session:
                db.write_lock(session)
                source = active_source(session, source_id, version, stamp)
                if not source:
                    return False, '', old_state
                source.state = old_state
                session.commit()
        return len(uids) > 5, notice, {'uidvalidity': validity, 'uid': last_uid}
    finally:
        try:
            mail.logout()
        except (OSError, imaplib.IMAP4.error):
            pass


def poll_next():
    with db.SessionLocal() as session:
        db.write_lock(session)
        stale = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        source = session.scalar(select(Source).where(Source.enabled.is_(True), Source.next_run <= db.utcnow(),
            (Source.status != 'running') | (Source.updated_at < stale)).order_by(Source.next_run).limit(1))
        if not source:
            return False
        if not permitted(session, source):
            source.enabled, source.status, source.notice = False, 'error', 'Владелец источника утратил доступ к проекту'
            session.commit()
            return True
        source.status, source.updated_at = 'running', db.utcnow()
        key, version = source.id, source.version
        snapshot = (key, version, source.project_id, source.owner_id, dict(source.config), dict(source.state), source.updated_at)
        kind = source.kind
        session.commit()
    try:
        if kind == 'folder':
            more, notice = poll_folder(snapshot)
            state = snapshot[5] if more else {}
        else:
            more, notice, state = poll_mail(snapshot)
        status = 'ready'
    except (OSError, ValueError, KeyError, SourceError, HTTPException, httpx.HTTPError, imaplib.IMAP4.error, SQLAlchemyError, InvalidToken) as error:
        more, state, status = False, None, 'error'
        notice = str(error) if isinstance(error, SourceError) else 'Не удалось прочитать источник. Проверьте доступ, настройки папки, пароль приложения или OAuth.'
    with db.SessionLocal() as session:
        db.write_lock(session)
        source = active_source(session, key, version, snapshot[-1])
        if source:
            source.status, source.notice, source.last_run = status, notice, db.utcnow()
            if state is not None:
                source.state = state
            source.next_run = (datetime.now(UTC) + timedelta(seconds=1 if more else 60)).isoformat()
            session.commit()
    return True
