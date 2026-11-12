"""Project-bound incoming API and verified Telegram/VK callbacks."""
import hashlib
import hmac
import json
import re
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import PlainTextResponse
from pydantic import Field, SecretStr, model_validator
from sqlalchemy import JSON, ForeignKey, String, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column
from starlette.concurrency import run_in_threadpool

from . import db, vault
from .auth import Identity, digest, identity, project_access
from .contracts import InputModel
from .documents import MAX_BYTES, Document, DocumentVersion, Occurrence, filename_only, ingest
from .settings import DATA

router = APIRouter(tags=['integrations'])


class Integration(db.Base):
    __tablename__ = 'integrations'
    __table_args__ = (UniqueConstraint('project_id', 'name'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=db.uid)
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    owner_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(20))
    config: Mapped[dict] = mapped_column(JSON, default=dict)
    secret_ref: Mapped[str] = mapped_column(String(32))
    secret_hash: Mapped[str] = mapped_column(String(64))
    enabled: Mapped[bool] = mapped_column(default=True)
    version: Mapped[int] = mapped_column(default=1)
    window: Mapped[int] = mapped_column(default=0)
    count: Mapped[int] = mapped_column(default=0)


class IntegrationMessage(db.Base):
    __tablename__ = 'integration_messages'
    __table_args__ = (UniqueConstraint('integration_id', 'external_id'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=db.uid)
    integration_id: Mapped[str] = mapped_column(ForeignKey('integrations.id'), index=True)
    external_id: Mapped[str] = mapped_column(String(200))
    document_id: Mapped[str | None] = mapped_column(ForeignKey('documents.id'), nullable=True)


class Receipt(db.Base):
    __tablename__ = 'integration_receipts'
    integration_id: Mapped[str] = mapped_column(ForeignKey('integrations.id'), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    checksum: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default='running')
    updated_at: Mapped[str] = mapped_column(String(40), default=db.utcnow)
    reply: Mapped[dict] = mapped_column(JSON, default=dict, server_default='{}')


class BotActor(db.Base):
    __tablename__ = 'bot_actors'
    integration_id: Mapped[str] = mapped_column(ForeignKey('integrations.id'), primary_key=True)
    sender: Mapped[str] = mapped_column(String(40), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))


class BotTicket(db.Base):
    __tablename__ = 'bot_tickets'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    integration_id: Mapped[str] = mapped_column(ForeignKey('integrations.id'), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    sender: Mapped[str] = mapped_column(String(40), default='')
    proposal_id: Mapped[str | None] = mapped_column(ForeignKey('proposals.id'), nullable=True)
    proposal_version: Mapped[int] = mapped_column(default=0)
    expires_at: Mapped[str] = mapped_column(String(40))
    used: Mapped[bool] = mapped_column(default=False)


class IntegrationInput(InputModel):
    name: str = Field(min_length=1, max_length=100)
    kind: Literal['api', 'telegram', 'vk']
    chat_ids: list[int] = Field(default_factory=list, max_length=100)
    sender_ids: list[int] = Field(default_factory=list, max_length=100)
    group_id: int = Field(default=0, ge=0)
    bot_token: SecretStr = SecretStr('')
    confirmation_code: SecretStr = SecretStr('')
    reply_enabled: bool = False
    enabled: bool = True

    @model_validator(mode='after')
    def valid(self):
        if self.kind != 'api' and not self.chat_ids:
            raise ValueError('Задайте разрешенные ID чатов')
        token = self.bot_token.get_secret_value()
        if self.kind != 'api' and (not token or len(token) > 1000 or not re.fullmatch(r'[A-Za-z0-9_:.-]+', token)):
            raise ValueError('Укажите корректный токен бота или сообщества')
        if self.kind == 'vk' and (not self.group_id or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}', self.confirmation_code.get_secret_value())):
            raise ValueError('Для ВК нужны ID сообщества и строка подтверждения')
        return self


class ToggleInput(InputModel):
    expected_version: int = Field(ge=1)
    enabled: bool


class LinkInput(InputModel):
    sender: str = Field(min_length=1, max_length=30, pattern=r'^[1-9][0-9]*$')


def public(row):
    return {key: getattr(row, key) for key in ('id', 'name', 'kind', 'config', 'enabled', 'version')}


@router.get('/api/projects/{project_id}/integrations')
def list_integrations(project_id: str, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    project_access(project_id, session, who)
    return [public(row) for row in session.scalars(select(Integration).where(Integration.project_id == project_id))]


@router.post('/api/projects/{project_id}/integrations', status_code=201)
def create_integration(project_id: str, body: IntegrationInput, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    project_access(project_id, session, who, 'edit')
    db.write_lock(session)
    if session.scalar(select(Integration.id).where(Integration.project_id == project_id, Integration.name == body.name)):
        raise HTTPException(409, 'Название подключения уже используется')
    key = secrets.token_urlsafe(32)
    reference = vault.put({'bot_token': body.bot_token.get_secret_value(), 'confirmation_code': body.confirmation_code.get_secret_value()})
    row = Integration(project_id=project_id, owner_id=who.user.id, name=body.name, kind=body.kind,
        config=body.model_dump(exclude={'name', 'kind', 'bot_token', 'confirmation_code', 'enabled'}),
        secret_ref=reference, secret_hash=digest(key), enabled=body.enabled)
    try:
        session.add(row)
        session.commit()
    except Exception:
        vault.discard(reference)
        raise
    return {**public(row), 'incoming_key': key}


@router.patch('/api/integrations/{integration_id}')
def toggle(integration_id: str, body: ToggleInput, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    db.write_lock(session)
    row = session.get(Integration, integration_id)
    if not row:
        raise HTTPException(404, 'Подключение не найдено')
    project_access(row.project_id, session, who, 'edit')
    if row.version != body.expected_version:
        raise HTTPException(409, 'Подключение изменилось')
    row.enabled, row.version = body.enabled, row.version + 1
    session.commit()
    return public(row)


@router.post('/api/integrations/{integration_id}/link-code')
def link_code(integration_id: str, body: LinkInput, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    row = session.get(Integration, integration_id)
    if not row or row.kind == 'api' or not row.enabled:
        raise HTTPException(404, 'Бот не найден')
    project_access(row.project_id, session, who)
    token = secrets.token_urlsafe(18)
    session.add(BotTicket(token_hash=digest(token), integration_id=row.id, user_id=who.user.id, sender=body.sender,
        expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat()))
    session.commit()
    return {'command': '/link ' + token, 'expires_in_minutes': 10}


def permitted(session, row):
    user = session.get(db.User, row.owner_id)
    member = session.get(db.Membership, (row.owner_id, row.project_id))
    return user and user.active and member and member.can_edit


def authenticate(integration_id, secret, kind):
    with db.SessionLocal() as session:
        db.write_lock(session)
        row = session.get(Integration, integration_id)
        if not row or not row.enabled or row.kind != kind or not hmac.compare_digest(row.secret_hash, digest(secret)) or not permitted(session, row):
            raise HTTPException(403, 'Подключение недоступно')
        window = int(time.time()) // 60
        if row.window != window:
            row.window, row.count = window, 0
        if row.count >= 120:
            raise HTTPException(429, 'Слишком много запросов')
        row.count += 1
        session.commit()
        return {'id': row.id, 'version': row.version, 'kind': row.kind, 'project_id': row.project_id,
                'owner_id': row.owner_id, 'config': dict(row.config), 'credentials': vault.get(row.secret_ref)}


def active(session, snapshot):
    row = session.get(Integration, snapshot['id'])
    if not row or not row.enabled or row.version != snapshot['version'] or not permitted(session, row):
        raise HTTPException(409, 'Подключение изменилось')
    return row


def claim(snapshot, event_id, raw):
    checksum = hashlib.sha256(raw).hexdigest()
    with db.SessionLocal() as session:
        db.write_lock(session)
        active(session, snapshot)
        row = session.get(Receipt, (snapshot['id'], event_id))
        if row:
            if row.checksum != checksum:
                raise HTTPException(409, 'ID события уже связан с другим содержимым')
            if row.status == 'done':
                return False
            if row.status == 'running' and row.updated_at > (datetime.now(UTC) - timedelta(minutes=5)).isoformat():
                raise HTTPException(409, 'Событие уже обрабатывается')
            row.status, row.updated_at = 'running', db.utcnow()
        else:
            session.add(Receipt(integration_id=snapshot['id'], event_id=event_id, checksum=checksum))
        session.commit()
    return True


def finish(snapshot, event_id, status, reply=None, peer=None):
    with db.SessionLocal() as session:
        db.write_lock(session)
        row = session.get(Receipt, (snapshot['id'], event_id))
        if row:
            row.status, row.updated_at = status, db.utcnow()
            if reply is not None:
                content = reply if isinstance(reply, dict) else {'text': reply}
                row.reply = ({**content, 'peer': peer, 'cursor': 0, 'state': 'pending', 'updated_at': db.utcnow()}
                             if valid_reply(session, snapshot, content) else {})
            session.commit()


def valid_reply(session, snapshot, reply):
    if reply.get('user_id'):
        user = session.get(db.User, reply['user_id'])
        if not user or not user.active:
            return False
        try:
            project_access(snapshot['project_id'], session, Identity(user, None))
        except HTTPException:
            return False
    if reply.get('chat_turn_id'):
        from .assistant import ChatTurn
        turn = session.get(ChatTurn, reply['chat_turn_id'])
        if (not turn or digest(turn.answer) != reply.get('answer_hash')
                or set(turn.source_ids) != set(reply.get('source_ids', []))):
            return False
    if reply.get('proposal_id'):
        from .knowledge import Proposal
        proposal = session.get(Proposal, reply['proposal_id'])
        if not proposal or proposal.status != 'pending' or proposal.version != reply['proposal_version']:
            return False
    for key in reply.get('source_ids', []):
        version = session.get(DocumentVersion, key)
        document = session.get(Document, version.document_id) if version else None
        if not document or document.project_id != snapshot['project_id'] or document.lifecycle == 'purged':
            return False
    return True


def deliver(snapshot, event_id):
    """Retry the saved reply without executing the command a second time."""
    lease = db.uid()
    with db.SessionLocal() as session:
        db.write_lock(session)
        active(session, snapshot)
        row = session.get(Receipt, (snapshot['id'], event_id))
        reply = dict(row.reply) if row else {}
        if not reply or reply['cursor'] >= len(reply['text']):
            return
        if not valid_reply(session, snapshot, reply):
            row.reply = {}
            session.commit()
            return
        if reply['state'] == 'sending' and reply['updated_at'] > (datetime.now(UTC) - timedelta(seconds=60)).isoformat():
            raise HTTPException(409, 'Ответ уже отправляется')
        reply.update(state='sending', lease=lease, updated_at=db.utcnow())
        row.reply = reply
        session.commit()
    try:
        for start in range(reply['cursor'], len(reply['text']), 3500):
            with db.SessionLocal() as session:
                active(session, snapshot)
                row = session.get(Receipt, (snapshot['id'], event_id))
                if row.reply.get('lease') != lease or not valid_reply(session, snapshot, row.reply):
                    raise HTTPException(409, 'Контекст ответа изменился')
            part = reply['text'][start:start + 3500]
            send_reply(snapshot, reply['peer'], part)
            with db.SessionLocal() as session:
                db.write_lock(session)
                active(session, snapshot)
                row = session.get(Receipt, (snapshot['id'], event_id))
                if row.reply.get('lease') != lease:
                    raise HTTPException(409, 'Отправка уже возобновлена другим обработчиком')
                row.reply = {**row.reply, 'cursor': start + len(part), 'updated_at': db.utcnow(), 'state': 'sending'}
                session.commit()
    except (OSError, ValueError, HTTPException, httpx.HTTPError):
        with db.SessionLocal() as session:
            db.write_lock(session)
            row = session.get(Receipt, (snapshot['id'], event_id))
            if row and row.reply.get('lease') == lease:
                row.reply = {**row.reply, 'state': 'pending', 'updated_at': db.utcnow()}
                session.commit()
        raise HTTPException(503, 'Ответ сохранен, доставка будет повторена без повторного выполнения команды') from None


def ingest_item(snapshot, event_id, temporary, name, logical_id=None, metadata=None, parent_id=None):
    with db.SessionLocal() as session:
        db.write_lock(session)
        active(session, snapshot)
        external_id = snapshot['id'] + ':' + event_id
        prior = session.scalar(select(Occurrence).where(Occurrence.project_id == snapshot['project_id'],
            Occurrence.channel == snapshot['kind'], Occurrence.external_id == external_id))
        if prior:
            version = session.get(DocumentVersion, prior.version_id)
            with temporary.open('rb') as stream:
                if hashlib.file_digest(stream, 'sha256').hexdigest() != version.sha256:
                    raise HTTPException(409, 'ID события уже связан с другим содержимым')
            return {'version_id': prior.version_id, 'duplicate': True}
        binding = session.scalar(select(IntegrationMessage).where(IntegrationMessage.integration_id == snapshot['id'],
            IntegrationMessage.external_id == logical_id)) if logical_id else None
        document = session.get(Document, binding.document_id) if binding and binding.document_id else None
        if document and document.lifecycle == 'archived':
            raise HTTPException(409, 'Сообщение связано с архивным документом')
        target = document if document and document.lifecycle == 'active' else None
        force_version = bool(target and metadata and session.scalar(select(DocumentVersion).where(
            DocumentVersion.document_id == target.id, DocumentVersion.number == target.current_number))
            .metadata_json.get('platform_message') != metadata.get('platform_message'))
        def receipt(result):
            if metadata and result.get('version_id') and not result.get('duplicate'):
                session.get(DocumentVersion, result['version_id']).metadata_json = metadata
            if logical_id:
                row = binding or IntegrationMessage(integration_id=snapshot['id'], external_id=logical_id)
                row.document_id = result.get('id')
                session.add(row)
        return ingest(session, snapshot['project_id'], snapshot['owner_id'], temporary, name,
            channel=snapshot['kind'], external_id=external_id, source_name=snapshot['kind'],
            document_id=target.id if target else None, expected_version=target.version if target else None,
            parent_version_id=parent_id, before_commit=receipt, deduplicate=not bool(logical_id), force_version=force_version)


@router.post('/api/incoming/{integration_id}/files')
async def incoming_file(integration_id: str, file: UploadFile, external_id: str = Form(min_length=1, max_length=100),
                        authorization: str = Header(default='')):
    snapshot = authenticate(integration_id, authorization.removeprefix('Bearer '), 'api')
    name = filename_only(file.filename or '')
    target = DATA / 'tmp' / (db.uid() + '.incoming')
    try:
        with target.open('xb') as stream:
            size = 0
            while block := await file.read(1024 * 1024):
                size += len(block)
                if size > MAX_BYTES:
                    raise HTTPException(413, 'Файл больше 100 МиБ')
                stream.write(block)
        # A repeated key with changed bytes must conflict, not silently acknowledge.
        return ingest_item(snapshot, external_id, target, name)
    finally:
        await file.close()
        target.unlink(missing_ok=True)


def download(url, target, telegram=False):
    parsed = urlsplit(url)
    host = parsed.hostname or ''
    allowed = host == 'api.telegram.org' if telegram else host == 'userapi.com' or host.endswith('.userapi.com') or host == 'vk.com'
    if parsed.scheme != 'https' or parsed.port not in {None, 443} or parsed.username or parsed.password or not allowed:
        raise ValueError('Attachment host forbidden')
    with httpx.Client(trust_env=False, timeout=20, follow_redirects=False) as client, client.stream('GET', url) as response:
        response.raise_for_status()
        with target.open('xb') as stream:
            size = 0
            for block in response.iter_raw(1024 * 1024):
                size += len(block)
                if size > MAX_BYTES:
                    raise ValueError('Attachment too large')
                stream.write(block)


def telegram_file(snapshot, item):
    token = snapshot['credentials']['bot_token']
    with httpx.Client(trust_env=False, timeout=15, follow_redirects=False) as client:
        response = client.post('https://api.telegram.org/bot' + token + '/getFile', json={'file_id': item['file_id']})
        response.raise_for_status()
        value = response.json()
    path = value.get('result', {}).get('file_path', '')
    if not value.get('ok') or not re.fullmatch(r'[A-Za-z0-9_./-]{1,500}', path) or '..' in path:
        raise ValueError('Invalid Telegram file path')
    return 'https://api.telegram.org/file/bot' + token + '/' + path


def send_reply(snapshot, peer, text):
    if not snapshot['config'].get('reply_enabled'):
        return
    with db.SessionLocal() as session:
        active(session, snapshot)
    token = snapshot['credentials']['bot_token']
    # Send the complete preview, then the one-time confirmation command.
    with httpx.Client(trust_env=False, timeout=15, follow_redirects=False) as client:
        for number, start in enumerate(range(0, len(text), 3500)):
            part = text[start:start + 3500]
            if snapshot['kind'] == 'telegram':
                response = client.post('https://api.telegram.org/bot' + token + '/sendMessage',
                    json={'chat_id': peer, 'text': part, 'link_preview_options': {'is_disabled': True}})
            else:
                response = client.post('https://api.vk.com/method/messages.send', data={'access_token': token, 'v': '5.199',
                    'peer_id': peer, 'random_id': int(hashlib.sha256((str(peer) + text + str(number)).encode()).hexdigest()[:7], 16), 'message': part})
            response.raise_for_status()
            value = response.json()
            if value.get('error') or value.get('ok') is False:
                raise ValueError('Reply rejected')


@router.post('/api/incoming/{integration_id}/telegram')
async def telegram_callback(integration_id: str, request: Request, x_telegram_bot_api_secret_token: str = Header(default='')):
    return await webhook(integration_id, request, x_telegram_bot_api_secret_token, 'telegram')


@router.post('/api/incoming/{integration_id}/vk')
async def vk_callback(integration_id: str, request: Request):
    return await webhook(integration_id, request, '', 'vk')


async def webhook(integration_id, request, secret, kind):
    raw = await request.body()
    return await run_in_threadpool(process_webhook, integration_id, raw, secret, kind)


def process_webhook(integration_id, raw, secret, kind):
    from .bot_commands import command
    claimed, completed = False, False
    try:
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise TypeError()
    except (TypeError, ValueError, RecursionError):
        raise HTTPException(422, 'Ожидается JSON события') from None
    snapshot = authenticate(integration_id, secret if kind == 'telegram' else str(value.get('secret', '')), kind)
    if kind == 'vk':
        if value.get('group_id') != snapshot['config']['group_id']:
            raise HTTPException(403, 'Чужое сообщество')
        if value.get('type') == 'confirmation':
            return PlainTextResponse(snapshot['credentials']['confirmation_code'])
        if value.get('type') not in {'message_new', 'message_edit'}:
            return PlainTextResponse('ok')
    try:
        if kind == 'telegram':
            message = value.get('message') or value.get('edited_message') or value.get('channel_post') or value.get('edited_channel_post')
            if not message:
                return {'ok': True}
            event_id, peer = str(int(value['update_id'])), int(message['chat']['id'])
            sender = str(int(message.get('from', {}).get('id', 0)))
            attachments = []
            if message.get('document'):
                attachments.append((message['document'], message['document'].get('file_name', 'document.bin')))
            if message.get('photo'):
                attachments.append((message['photo'][-1], 'photo.jpg'))
            unsupported = [key for key in ('audio', 'video', 'voice', 'animation', 'video_note', 'sticker') if message.get(key)]
        else:
            message = value['object']['message']
            event_id, peer, sender = str(value['event_id']), int(message['peer_id']), str(int(message['from_id']))
            attachments = [(item['doc'], item['doc']['title']) for item in message.get('attachments', []) if item.get('type') == 'doc']
            unsupported = []
            for item in message.get('attachments', []):
                if item.get('type') == 'photo':
                    sizes = item['photo'].get('sizes', [])
                    if sizes:
                        photo = max(sizes, key=lambda size: int(size.get('width', 0)) * int(size.get('height', 0)))
                        attachments.append(({'url': photo['url']}, 'vk-photo.jpg'))
                elif item.get('type') != 'doc':
                    unsupported.append(str(item.get('type', 'неизвестный'))[:100])
        if len(event_id) > 100 or len(attachments) > 10:
            raise ValueError()
        if peer not in snapshot['config']['chat_ids'] or (snapshot['config']['sender_ids'] and int(sender) not in snapshot['config']['sender_ids']):
            raise HTTPException(403, 'Чат или отправитель не разрешен')
        if not claim(snapshot, event_id, json.dumps(value, sort_keys=True, ensure_ascii=False).encode()):
            deliver(snapshot, event_id)
            return PlainTextResponse('ok') if kind == 'vk' else {'ok': True, 'duplicate': True}
        claimed = True
        text = str(message.get('text') or message.get('caption') or '')[:64000]
        try:
            reply = command(snapshot, sender, peer, text)
        except HTTPException as error:
            if error.status_code >= 500:
                raise
            reply = 'Действие не выполнено: ' + str(error.detail)
        if reply is not None:
            finish(snapshot, event_id, 'done', reply=reply, peer=peer)
            completed = True
            deliver(snapshot, event_id)
        else:
            target = DATA / 'tmp' / (db.uid() + '.txt')
            try:
                target.write_text(text or 'Сообщение с вложениями', encoding='utf-8')
                # Tokens and webhook secret never become document metadata.
                metadata = {'platform': kind, 'date': datetime.fromtimestamp(int(message['date']), UTC).isoformat() if message.get('date') else None,
                    'platform_message': message, 'peer_id': peer, 'sender_id': sender}
                parent = ingest_item(snapshot, event_id + ':text', target, kind + '-message.txt',
                    logical_id=str(peer) + ':' + str(message.get('message_id', message.get('id'))), metadata=metadata)
            finally:
                target.unlink(missing_ok=True)
            warnings = ['Тип вложения требует ручной загрузки: ' + item for item in unsupported]
            for index, (attachment, name) in enumerate(attachments):
                temporary = DATA / 'tmp' / (db.uid() + '.attachment')
                try:
                    name = filename_only(name)
                    url = telegram_file(snapshot, attachment) if kind == 'telegram' else attachment['url']
                    download(url, temporary, telegram=kind == 'telegram')
                    if not parent.get('suppressed'):
                        ingest_item(snapshot, event_id + ':file:' + str(index), temporary, name, parent_id=parent['version_id'])
                except (ValueError, KeyError, OSError, httpx.HTTPError, HTTPException):
                    warnings.append('Вложение требует ручной загрузки: ' + str(name)[:240])
                finally:
                    temporary.unlink(missing_ok=True)
            if warnings and parent.get('version_id'):
                with db.SessionLocal() as session:
                    db.write_lock(session)
                    active(session, snapshot)
                    version = session.get(DocumentVersion, parent['version_id'])
                    if session.get(Document, version.document_id).lifecycle == 'active':
                        version.metadata_json = {**version.metadata_json, 'connector_warnings': warnings}
                        version.warning = '; '.join(warnings)
                        version.status = 'needs_preparation'
                        session.commit()
            finish(snapshot, event_id, 'done')
            completed = True
    except HTTPException:
        if claimed and not completed:
            finish(snapshot, event_id, 'error')
        raise
    except (KeyError, TypeError, ValueError, OSError, OverflowError, httpx.HTTPError):
        if claimed and not completed:
            finish(snapshot, event_id, 'error')
        raise HTTPException(422, 'Событие не обработано. Проверьте формат и настройки подключения.') from None
    return PlainTextResponse('ok') if kind == 'vk' else {'ok': True}
