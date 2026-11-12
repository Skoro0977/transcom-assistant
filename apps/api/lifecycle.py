import difflib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import Field
from sqlalchemy import ForeignKey, String, UniqueConstraint, delete, select, update
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import db
from .auth import Identity, digest, identity, project_access
from .documents import Document, DocumentVersion, Job, ReparseInput, accessible_document, blob_path
from .knowledge import Audit, FactData, Proposal, Record, Revision
from .search import Chunk
from .worker import AttachmentLink

router = APIRouter(tags=['history'])


@router.get('/api/records/{record_id}/history')
def record_history(record_id: str, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    record = session.get(Record, record_id)
    if not record:
        raise HTTPException(404, 'Карточка не найдена')
    project_access(record.project_id, session, who)
    result, previous = [], {}
    for revision in session.scalars(select(Revision).where(Revision.record_id == record_id).order_by(Revision.number)):
        changes = {key: {'before': previous.get(key), 'after': value} for key, value in revision.data.items()
                   if previous.get(key) != value}
        result.append({'number': revision.number, 'data': revision.data, 'evidence': revision.evidence,
                       'reason': revision.reason, 'created_at': revision.created_at,
                       'author': session.get(db.User, revision.author_id).username, 'changes': changes})
        previous = revision.data
    return result


@router.post('/api/jobs/{job_id}/retry')
def retry_job(job_id: str, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    db.write_lock(session)
    job = session.get(Job, job_id)
    if not job:
        raise HTTPException(404, 'Задание не найдено')
    project_access(job.project_id, session, who, 'edit')
    if job.status != 'failed':
        raise HTTPException(409, 'Можно повторить только неуспешное задание')
    job.status, job.attempts, job.error, job.updated_at = 'queued', 0, '', db.utcnow()
    session.commit()
    return {'status': job.status}


class PurgedContent(db.Base):
    __tablename__ = 'purged_content'
    __table_args__ = (UniqueConstraint('project_id', 'sha256'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=db.uid)
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    created_at: Mapped[str] = mapped_column(String(40), default=db.utcnow)


class PurgeChallenge(db.Base):
    __tablename__ = 'purge_challenges'
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    document_id: Mapped[str] = mapped_column(ForeignKey('documents.id'))
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    document_version: Mapped[int] = mapped_column()
    expires_at: Mapped[str] = mapped_column(String(40))


class ArchiveInput(ReparseInput):
    state: Literal['active', 'archived']


class DocumentMetadata(ReparseInput):
    title: str = Field(min_length=1, max_length=240)
    category: str = Field(min_length=1, max_length=200)


@router.patch('/api/documents/{document_id}')
def edit_metadata(document_id: str, body: DocumentMetadata, who: Identity = Depends(identity),
                  session: Session = Depends(db.session_db)):
    db.write_lock(session)
    document = accessible_document(document_id, session, who)
    project_access(document.project_id, session, who, 'edit')
    if document.version != body.expected_version:
        raise HTTPException(409, 'Документ уже изменился')
    before = {'title': document.title, 'category': document.category}
    document.title, document.category, document.version = body.title, body.category, document.version + 1
    session.add(Audit(project_id=document.project_id, user_id=who.user.id, action='document_metadata_changed',
                      entity_id=document.id, details={'before': before,
                        'after': {'title': document.title, 'category': document.category}, 'document_version': document.version}))
    session.commit()
    return {'version': document.version}


class PurgeInput(ReparseInput):
    token: str = Field(min_length=20, max_length=128)
    confirmation: Literal['УДАЛИТЬ']


def cleanup_blob(job_id):
    with db.SessionLocal() as session:
        db.write_lock(session)
        job = session.get(Job, job_id)
        version = session.get(DocumentVersion, job.version_id)
        document = session.get(Document, version.document_id)
        if document.lifecycle != 'purged':
            job.status = 'cancelled'
        else:
            if version.blob_key:
                blob_path(version.blob_key).unlink(missing_ok=True)
            version.blob_key, version.size = '', 0
            job.status, job.error = 'done', ''
        job.updated_at = db.utcnow()
        session.commit()


@router.post('/api/documents/{document_id}/archive')
def archive(document_id: str, body: ArchiveInput, who: Identity = Depends(identity),
            session: Session = Depends(db.session_db)):
    db.write_lock(session)
    document = accessible_document(document_id, session, who)
    project_access(document.project_id, session, who, 'edit')
    if document.version != body.expected_version:
        raise HTTPException(409, 'Документ уже изменился')
    if document.lifecycle == body.state:
        return {'version': document.version, 'lifecycle': document.lifecycle}
    document.lifecycle, document.version = body.state, document.version + 1
    version_ids = select(DocumentVersion.id).where(DocumentVersion.document_id == document_id)
    if body.state == 'archived':
        session.execute(update(Chunk).where(Chunk.document_version_id.in_(version_ids),
                        Chunk.state.in_(['pending', 'running'])).values(state='paused', updated_at=db.utcnow()))
        session.execute(update(Job).where(Job.version_id.in_(version_ids), Job.status.in_(['queued', 'running']))
                        .values(status='cancelled'))
        session.execute(update(Proposal).where(Proposal.source_version_id.in_(version_ids), Proposal.status == 'pending')
                        .values(status='rejected', reason='Источник исключен из работы', version=Proposal.version + 1))
    else:
        session.execute(update(Chunk).where(Chunk.document_version_id.in_(version_ids), Chunk.state == 'paused')
                        .values(state='pending', attempts=0, error='', updated_at=db.utcnow()))
        cancelled_parse = list(session.scalars(select(Job).where(Job.version_id.in_(version_ids),
            Job.kind == 'parse', Job.status == 'cancelled')))
        for job in cancelled_parse:
            job.status, job.attempts, job.error, job.updated_at = 'queued', 0, '', db.utcnow()
            session.get(DocumentVersion, job.version_id).status = 'queued'
        session.execute(update(Job).where(Job.version_id.in_(version_ids), Job.kind == 'analyze')
                        .values(status='queued', cursor=0, attempts=0, error='', updated_at=db.utcnow()))
    session.add(Audit(project_id=document.project_id, user_id=who.user.id, action='document_' + body.state,
                      entity_id=document.id))
    session.commit()
    return {'version': document.version, 'lifecycle': document.lifecycle}


@router.post('/api/documents/{document_id}/purge-challenge')
def challenge(document_id: str, body: ReparseInput, who: Identity = Depends(identity),
              session: Session = Depends(db.session_db)):
    db.write_lock(session)
    document = accessible_document(document_id, session, who)
    project_access(document.project_id, session, who, 'edit')
    if document.lifecycle != 'archived' or document.version != body.expected_version:
        raise HTTPException(409, 'Сначала переместите актуальную редакцию документа в архив')
    token = secrets.token_urlsafe(32)
    session.execute(delete(PurgeChallenge).where(PurgeChallenge.expires_at < db.utcnow()))
    session.add(PurgeChallenge(token_hash=digest(token), document_id=document.id, user_id=who.user.id,
        document_version=document.version, expires_at=(datetime.now(UTC) + timedelta(minutes=5)).isoformat()))
    session.commit()
    return {'token': token, 'warning': 'Будут окончательно удалены оригиналы и извлеченный текст всех редакций '
        'этого документа, его цитаты и неподтвержденные выводы. Восстановления в приложении нет. '
        'Подтвержденные решения проекта сохранятся с отметкой удаленного источника. '
        'Самостоятельные документы-вложения удаляются отдельно. '
        'Исходная папка, письма и резервные копии могут содержать копии файла и очищаются отдельно. '
        'Автоматическое повторное поступление тех же байтов будет блокироваться.'}


@router.post('/api/documents/{document_id}/purge')
def purge(document_id: str, body: PurgeInput, who: Identity = Depends(identity),
          session: Session = Depends(db.session_db)):
    db.write_lock(session)
    document = accessible_document(document_id, session, who)
    project_access(document.project_id, session, who, 'edit')
    ticket = session.get(PurgeChallenge, digest(body.token))
    if (not ticket or ticket.user_id != who.user.id or ticket.document_id != document_id
            or ticket.expires_at < db.utcnow() or ticket.document_version != document.version
            or document.version != body.expected_version or document.lifecycle != 'archived'):
        raise HTTPException(409, 'Подтверждение недействительно, повторите предупреждение')
    versions = list(session.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document_id)))
    ids = {v.id for v in versions}
    session.execute(delete(Chunk).where(Chunk.document_version_id.in_(ids)))
    for version in versions:
        if not session.scalar(select(PurgedContent).where(PurgedContent.project_id == document.project_id,
                                                        PurgedContent.sha256 == version.sha256)):
            session.add(PurgedContent(project_id=document.project_id, sha256=version.sha256))
            session.flush()
        version.text, version.segments, version.metadata_json, version.warning = '', [], {}, ''
        version.filename, version.status = 'Удаленный источник', 'purged'
        session.execute(update(Job).where(Job.version_id == version.id).values(status='cancelled', error=''))
        session.add(Job(project_id=document.project_id, version_id=version.id, kind='purge'))
    session.execute(delete(AttachmentLink).where((AttachmentLink.parent_version_id.in_(ids)) |
                                                 (AttachmentLink.child_version_id.in_(ids))))
    from .documents import Occurrence
    for origin in session.scalars(select(Occurrence).where(Occurrence.version_id.in_(ids))):
        origin.source_name, origin.external_id = 'Удаленный источник', digest(origin.external_id)
    for proposal in session.scalars(select(Proposal).where(Proposal.project_id == document.project_id)):
        if (proposal.source_version_id in ids or any(e.get('version_id') in ids for e in proposal.evidence)
                or (proposal.kind == 'document' and proposal.data.get('document_id') == document_id)):
            proposal.data = FactData(title='Источник удален').model_dump(mode='json')
            proposal.evidence, proposal.note = [], 'Источник окончательно удален'
            proposal.version += 1
            if proposal.status == 'pending':
                proposal.status = 'rejected'
    for revision in session.scalars(select(Revision).where(Revision.project_id == document.project_id)):
        if any(e.get('version_id') in ids for e in revision.evidence):
            revision.evidence = [dict(e, quote='', locator='Удаленный источник', removed=True)
                                 if e.get('version_id') in ids else e for e in revision.evidence]
    from .assistant import ChatTurn
    for turn in session.scalars(select(ChatTurn).where(ChatTurn.project_id == document.project_id)):
        if set(turn.source_ids) & ids:
            turn.answer, turn.citations = 'Основание ответа окончательно удалено.', []
            turn.source_ids = []
            turn.notice = 'Повторите вопрос по оставшимся источникам.'
    document.lifecycle, document.version = 'purged', document.version + 1
    document.title, document.category = 'Удаленный документ', ''
    session.execute(delete(PurgeChallenge).where(PurgeChallenge.document_id == document_id))
    session.add(Audit(project_id=document.project_id, user_id=who.user.id, action='document_purged',
                      entity_id=document.id))
    session.commit()
    return {'status': 'purge_queued', 'message': 'Документ закрыт для доступа, физическая очистка поставлена в очередь'}


@router.get('/api/documents/{document_id}/compare')
def compare(document_id: str, before: int, after: int, who: Identity = Depends(identity),
            session: Session = Depends(db.session_db)):
    document = accessible_document(document_id, session, who)
    versions = {v.number: v for v in session.scalars(select(DocumentVersion).where(
        DocumentVersion.document_id == document.id, DocumentVersion.number.in_([before, after])))}
    if before not in versions or after not in versions:
        raise HTTPException(404, 'Редакция не найдена')
    left, right = versions[before], versions[after]
    lines = list(difflib.unified_diff(left.text[:200000].splitlines(), right.text[:200000].splitlines(),
                                     fromfile=f'Редакция {before}', tofile=f'Редакция {after}', lineterm=''))
    return {'text_diff': '\n'.join(lines[:2000]), 'truncated': len(lines) > 2000 or max(len(left.text), len(right.text)) > 200000,
            'note': 'Это разница извлеченного текста. Смысловые изменения подтверждаются отдельно в очереди предложений.'}
