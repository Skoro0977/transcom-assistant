import hashlib
import math
import re
import time as monotonic_clock
from array import array
from datetime import UTC, date, datetime, time, timedelta, timezone
from email.utils import parsedate_to_datetime
from operator import mul
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import (
    CheckConstraint,
    ForeignKey,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    and_,
    func,
    or_,
    select,
    update,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import db
from .auth import Identity, identity, project_access
from .documents import Document, DocumentVersion
from .gigachat import ProviderError, giga
from .knowledge import Record, Revision
from .settings import settings

router = APIRouter(tags=['search'])
_embedding_backoff = 0.0


class Chunk(db.Base):
    __tablename__ = 'chunks'
    __table_args__ = (CheckConstraint('(document_version_id IS NULL) != (revision_id IS NULL)', name='one_chunk_source'),
        UniqueConstraint('document_version_id', 'ordinal'), UniqueConstraint('revision_id', 'ordinal'))
    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    document_version_id: Mapped[str | None] = mapped_column(ForeignKey('document_versions.id'), nullable=True, index=True)
    revision_id: Mapped[str | None] = mapped_column(ForeignKey('record_revisions.id'), nullable=True, index=True)
    ordinal: Mapped[int] = mapped_column()
    locator: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    quoted: Mapped[bool] = mapped_column(default=False)
    source_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    recorded_at: Mapped[str] = mapped_column(String(40))
    vector: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    model: Mapped[str] = mapped_column(String(100), default='')
    dimension: Mapped[int] = mapped_column(default=0)
    state: Mapped[str] = mapped_column(String(16), default='pending', index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str] = mapped_column(String(400), default='')
    updated_at: Mapped[str] = mapped_column(String(40), default=db.utcnow)


def bounded_text(text, max_chars=1400):
    value = text[:max_chars]
    while len(value.encode('utf-8')) > 3500:
        value = value[:len(value) // 2]
    if len(value) < len(text):
        boundary = max(value.rfind(' '), value.rfind('\n'))
        if boundary > len(value) - 250:
            value = value[:boundary + 1]
    return value


def parts(segments):
    result, text, locators, quoted = [], '', [], False
    for segment in segments:
        remaining = segment['text'].strip()
        while remaining:
            part = bounded_text(remaining)
            flag = bool(segment.get('quoted'))
            combined = text + ('\n\n' if text else '') + part
            if text and (len(combined) > 1400 or len(combined.encode('utf-8')) > 3500 or quoted != flag):
                result.append((text, '; '.join(dict.fromkeys(locators)), quoted))
                text, locators = '', []
            text += ('\n\n' if text else '') + part
            locators.append(segment['locator'])
            quoted = flag
            remaining = remaining[len(part):]
    if text:
        result.append((text, '; '.join(dict.fromkeys(locators)), quoted))
    return result


def source_date(value):
    if not value:
        return None
    value = str(value)[:200]
    try:
        return datetime.fromisoformat(value).date().isoformat()
    except ValueError:
        try:
            return parsedate_to_datetime(value).date().isoformat()
        except (ValueError, TypeError, OverflowError):
            return None


def build_chunks(session, project_id, source_id, source_kind, segments, recorded_at, dated):
    field = Chunk.document_version_id if source_kind == 'document' else Chunk.revision_id
    existing = list(session.scalars(select(Chunk).where(field == source_id)))
    prepared = parts(segments)
    ids = set()
    # Delete changed rows before inserting replacements with the same ordinal.
    for old in existing:
        content = prepared[old.ordinal] if old.ordinal < len(prepared) else None
        if content is None or (old.text, old.locator, old.quoted) != content:
            session.delete(old)
    session.flush()
    for ordinal, (text, locator, quoted) in enumerate(prepared):
        key = hashlib.sha256(f'{source_kind}:{source_id}:{ordinal}:{locator}:{text}'.encode()).hexdigest()
        ids.add(key)
        saved = session.get(Chunk, key)
        if saved is not None:
            saved.source_date, saved.recorded_at = dated, recorded_at
        else:
            session.add(Chunk(id=key, project_id=project_id, ordinal=ordinal, locator=locator, text=text,
                quoted=quoted, recorded_at=recorded_at, source_date=dated,
                document_version_id=source_id if source_kind == 'document' else None,
                revision_id=source_id if source_kind == 'revision' else None))
    return len(ids)


def index_version(session, version):
    return build_chunks(session, version.project_id, version.id, 'document', version.segments,
                        version.created_at, source_date(version.metadata_json.get('date')))


def index_revision(session, revision):
    data = revision.data
    record = session.get(Record, revision.record_id)
    text = '\n'.join(str(data.get(k) or '') for k in ('title', 'text', 'scope', 'owner', 'due_date', 'stage_state', 'task_state', 'category', 'priority', 'risk_state'))
    text += '\nСостояние: ' + ('Отменено' if data.get('status') == 'cancelled' else 'Действует')
    return build_chunks(session, revision.project_id, revision.id, 'revision',
        [{'text': text, 'locator': f'Подтвержденная запись ({record.kind}), редакция {revision.number}', 'quoted': False}],
        revision.created_at, data.get('source_date'))


def eligible_query(project_id, mode='current'):
    document_filter = and_(Document.project_id == project_id, Document.lifecycle == 'active')
    record_filter = Record.project_id == project_id
    if mode == 'current':
        document_filter = and_(document_filter, DocumentVersion.number == Document.current_number, Chunk.quoted.is_(False))
        record_filter = and_(record_filter, Revision.number == Record.version)
    return select(Chunk).outerjoin(DocumentVersion, Chunk.document_version_id == DocumentVersion.id)\
        .outerjoin(Document, DocumentVersion.document_id == Document.id)\
        .outerjoin(Revision, Chunk.revision_id == Revision.id).outerjoin(Record, Revision.record_id == Record.id)\
        .where(Chunk.project_id == project_id, or_(document_filter, record_filter))


def embed_next():
    global _embedding_backoff
    if not settings.gigachat_bulk_indexing_enabled or not settings.gigachat_auth_key.get_secret_value():
        return False
    if monotonic_clock.monotonic() < _embedding_backoff:
        return False
    with db.SessionLocal() as session:
        db.write_lock(session)
        stale = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        session.execute(update(Chunk).where(Chunk.state == 'running', Chunk.updated_at < stale, Chunk.attempts < 3)
                        .values(state='pending'))
        session.execute(update(Chunk).where(Chunk.state == 'running', Chunk.updated_at < stale, Chunk.attempts >= 3)
                        .values(state='failed', error='Превышено число попыток индексирования'))
        chunks = list(session.scalars(select(Chunk).where(Chunk.state == 'pending', Chunk.updated_at <= db.utcnow(),
            Chunk.attempts < 3).order_by(Chunk.updated_at).limit(16)))
        if not chunks:
            return False
        ids, texts, stamps = [], [], []
        model = settings.gigachat_embedding_model
        for chunk in chunks:
            if chunk.document_version_id:
                version = session.get(DocumentVersion, chunk.document_version_id)
                if session.get(Document, version.document_id).lifecycle != 'active':
                    chunk.state = 'paused'
                    continue
            chunk.state, chunk.attempts, chunk.updated_at = 'running', chunk.attempts + 1, db.utcnow()
            ids.append(chunk.id)
            texts.append(chunk.text)
            stamps.append(chunk.updated_at)
        session.commit()
    if not ids:
        return True
    try:
        vectors = giga.embeddings(texts)
        error = ''
    except ProviderError as failure:
        vectors, error = [], str(failure)[:400]
        _embedding_backoff = monotonic_clock.monotonic() + 30
    with db.SessionLocal() as session:
        db.write_lock(session)
        for index, key in enumerate(ids):
            chunk = session.get(Chunk, key)
            if chunk is None or chunk.state != 'running' or chunk.updated_at != stamps[index]:
                continue
            if chunk.document_version_id:
                version = session.get(DocumentVersion, chunk.document_version_id)
                if session.get(Document, version.document_id).lifecycle != 'active':
                    chunk.state = 'paused'
                    continue
            if error:
                chunk.state = 'pending' if chunk.attempts < 3 else 'failed'
                chunk.error = error
                chunk.updated_at = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
            else:
                vector = vectors[index]
                norm = math.hypot(*vector)
                if not norm or not math.isfinite(norm):
                    chunk.state, chunk.error = 'failed', 'Нулевой эмбеддинг'
                    continue
                chunk.vector = array('f', [x / norm for x in vector]).tobytes()
                chunk.model, chunk.dimension, chunk.state, chunk.error = model, len(vector), 'ready', ''
                chunk.updated_at = db.utcnow()
        session.commit()
    return True


def general_deadline_question(query):
    words = set(re.findall(r'\w+', query.casefold()))
    deadline_terms = ('срок', 'дедлайн')
    general_terms = (*deadline_terms, 'како', 'каки', 'актуальн', 'текущ', 'поручен',
                     'задач', 'проект', 'искусственн', 'пример', 'ближайш', 'предстоящ',
                     'подскаж', 'покаж', 'назов', 'установлен', 'действующ', 'выполнен')
    fillers = {'а', 'в', 'на', 'для', 'у', 'по', 'из', 'нас', 'как', 'данный', 'момент',
               'же', 'сейчас', 'теперь', 'пожалуйста', 'работы', 'работе', 'работ'}
    return any(word.startswith(deadline_terms) for word in words) and all(
        word in fillers or word.startswith(general_terms) for word in words)


def deadline_context(rows, ranked, by_id, scores, limit):
    top = set(ranked[:limit])
    matched_versions = {by_id[key].document_version_id for key in top} - {None}
    matched_documents = {doc_id for chunk, _, _, doc_id in rows if doc_id and chunk.id in top}
    selected = {}
    for chunk, revision, kind, _ in rows:
        if kind not in ('requirement', 'task', 'stage'):
            continue
        data = revision.data
        due = data.get('due_date')
        if (not due or data.get('status') == 'cancelled' or data.get('task_state') == 'done'
                or data.get('stage_state') == 'approved' or due not in chunk.text.splitlines()):
            continue
        evidence = [e for e in revision.evidence if not e.get('removed')]
        linked_version = any(e.get('version_id') in matched_versions for e in evidence)
        linked_document = any(e.get('document_id') in matched_documents for e in evidence)
        priority = (not linked_version, not linked_document, -scores.get(chunk.id, 0),
                    due, revision.record_id, chunk.ordinal, chunk.id)
        if revision.id not in selected or priority < selected[revision.id]:
            selected[revision.id] = priority
    # Reserve a few of the existing source slots for cards, leaving room for documents.
    preferred = sorted(selected.values())[:min(3, max(1, limit // 2))]
    preferred_keys = [item[-1] for item in preferred]
    preferred_revisions = {by_id[key].revision_id for key in preferred_keys}
    rest = [key for key in ranked if by_id[key].revision_id not in preferred_revisions]
    return [*preferred_keys, *rest][:limit], set(preferred_keys)


def retrieve(session, project_id, query, mode='current', start=None, end=None, basis='recorded',
             semantic=True, limit=10, *, chat_deadlines=False):
    search = eligible_query(project_id, mode)
    if basis == 'source':
        if start:
            search = search.where(Chunk.source_date >= start.isoformat())
        if end:
            search = search.where(Chunk.source_date <= end.isoformat())
    else:
        moscow = timezone(timedelta(hours=3))
        if start and start != date.min:
            search = search.where(Chunk.recorded_at >= datetime.combine(start, time.min, moscow).astimezone(UTC).isoformat())
        if end and end != date.max:
            search = search.where(Chunk.recorded_at < datetime.combine(end + timedelta(days=1), time.min, moscow).astimezone(UTC).isoformat())
    warning, vector = '', None
    ready = session.scalar(select(func.count()).select_from(search.where(Chunk.state == 'ready',
        Chunk.model == settings.gigachat_embedding_model).subquery())) if semantic else 0
    if semantic and ready:
        try:
            vector = giga.embeddings([bounded_text(query)])[0]
            norm = math.hypot(*vector)
            vector = [x / norm for x in vector] if norm and math.isfinite(norm) else None
        except ProviderError as error:
            warning = str(error) + '. Доступен поиск по словам.'
    use_deadline_context = (chat_deadlines and mode == 'current' and not start and not end and limit > 0
                            and general_deadline_question(query))
    if use_deadline_context:
        rows = list(session.execute(search.add_columns(Revision, Record.kind, DocumentVersion.document_id)))
        chunks = [row[0] for row in rows]
    else:
        chunks = list(session.scalars(search))
    terms = {word[:6] for word in re.findall(r'\w{3,}', query.lower())}
    terms -= {'какие', 'когда', 'почему', 'этого', 'которы', 'проек', 'найди'}
    lexical, semantic_scores = [], []
    for chunk in chunks:
        lowered = chunk.text.lower()
        score = sum(min(lowered.count(term), 3) for term in terms) / math.sqrt(1 + len(lowered) / 500)
        if score:
            lexical.append((chunk.id, score))
        if (vector and chunk.vector and chunk.model == settings.gigachat_embedding_model
                and chunk.dimension == len(vector) and len(chunk.vector) == chunk.dimension * 4):
            stored = array('f')
            stored.frombytes(chunk.vector)
            cosine = sum(map(mul, vector, stored))
            if cosine >= 0.3:
                semantic_scores.append((chunk.id, cosine))
    scores = {}
    for ranking in (lexical, semantic_scores):
        for rank, (key, _) in enumerate(sorted(ranking, key=lambda item: item[1], reverse=True)[:100], 1):
            scores[key] = scores.get(key, 0) + 1 / (60 + rank)
    if semantic and not semantic_scores and not warning:
        warning = 'Смысловой индекс готовится или не дал подходящих совпадений. Показан поиск по словам.'
    if basis == 'source' and (start or end):
        warning += ' Источники без известной даты не входят в период.'
    by_id, items = {c.id: c for c in chunks}, []
    ranked = sorted(scores, key=scores.get, reverse=True)
    keys, deadline_keys = ranked[:limit], set()
    if use_deadline_context:
        keys, deadline_keys = deadline_context(rows, ranked, by_id, scores, limit)
    confirmed_due_dates = {}
    for key in keys:
        chunk = by_id[key]
        item = {'id': key, 'text': chunk.text, 'locator': chunk.locator, 'quoted': chunk.quoted,
                'source_date': chunk.source_date, 'recorded_at': chunk.recorded_at}
        if chunk.document_version_id:
            version = session.get(DocumentVersion, chunk.document_version_id)
            item.update(kind='document', version_id=version.id, document_id=version.document_id,
                        title=version.filename, number=version.number)
        else:
            revision = session.get(Revision, chunk.revision_id)
            item.update(kind='accepted', record_id=revision.record_id, number=revision.number,
                        title=revision.data['title'], evidence=revision.evidence)
            if key in deadline_keys:
                confirmed_due_dates[key] = revision.data['due_date']
        items.append(item)
    result = {'items': items, 'warning': warning.strip(), 'mode': mode, 'date_basis': basis}
    if confirmed_due_dates:
        result['confirmed_due_dates'] = confirmed_due_dates
    return result


@router.get('/api/projects/{project_id}/search')
def search(project_id: str, q: str = Query(min_length=2, max_length=2000),
           mode: Literal['current', 'history'] = 'current', start: date | None = None, end: date | None = None,
           basis: Literal['recorded', 'source'] = 'recorded', semantic: bool = True,
           who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    project_access(project_id, session, who)
    q = q.strip()
    if len(q) < 2 or (start and end and start > end):
        raise HTTPException(422, 'Проверьте запрос и границы периода')
    return retrieve(session, project_id, q, mode, start, end, basis, semantic)


@router.post('/api/projects/{project_id}/reindex')
def reindex(project_id: str, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    db.write_lock(session)
    project_access(project_id, session, who, 'edit')
    for version in session.scalars(select(DocumentVersion).join(Document).where(Document.project_id == project_id,
                                                                            Document.lifecycle != 'purged')):
        index_version(session, version)
    for revision in session.scalars(select(Revision).where(Revision.project_id == project_id)):
        index_revision(session, revision)
    session.execute(update(Chunk).where(Chunk.project_id == project_id,
        or_(Chunk.state == 'failed', and_(Chunk.state == 'ready', Chunk.model != settings.gigachat_embedding_model)))
        .values(state='pending', attempts=0, error='', updated_at=db.utcnow()))
    session.commit()
    return {'status': 'queued'}


@router.get('/api/projects/{project_id}/index-status')
def index_status(project_id: str, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    project_access(project_id, session, who)
    return {'states': dict(session.execute(select(Chunk.state, func.count()).where(Chunk.project_id == project_id)
                                           .group_by(Chunk.state)).all()), 'model': settings.gigachat_embedding_model,
            'cloud_indexing_enabled': settings.gigachat_bulk_indexing_enabled}
