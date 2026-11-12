"""Typed proposals and immutable accepted revisions. AI cannot accept facts."""
import hashlib
import json
from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field
from sqlalchemy import (
    JSON,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    update,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import db
from .auth import Identity, identity, project_access
from .contracts import BatchDecisionInput, DecisionInput, InputModel
from .documents import Document, DocumentVersion, Job, ReparseInput, accessible_document

router = APIRouter(tags=['knowledge'])


class FactData(InputModel):
    title: str = Field(min_length=1, max_length=500)
    text: str = Field(default='', max_length=12000)
    scope: str = Field(default='', max_length=1000)
    due_date: date | None = None
    source_date: date | None = None
    owner: str = Field(default='', max_length=200)
    status: Literal['active', 'cancelled'] = 'active'


class Record(db.Base):
    __tablename__ = 'records'
    __table_args__ = (UniqueConstraint('id', 'project_id'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=db.uid)
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    kind: Mapped[str] = mapped_column(String(20), default='requirement')
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[str] = mapped_column(String(40), default=db.utcnow)


class Revision(db.Base):
    __tablename__ = 'record_revisions'
    __table_args__ = (UniqueConstraint('record_id', 'number'), ForeignKeyConstraint(
        ['record_id', 'project_id'], ['records.id', 'records.project_id']))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=db.uid)
    record_id: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    number: Mapped[int] = mapped_column()
    data: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    author_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    reason: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[str] = mapped_column(String(40), default=db.utcnow)


class Proposal(db.Base):
    __tablename__ = 'proposals'
    __table_args__ = (UniqueConstraint('project_id', 'fingerprint'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=db.uid)
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    kind: Mapped[str] = mapped_column(String(20), default='requirement')
    target_id: Mapped[str | None] = mapped_column(ForeignKey('records.id'), nullable=True)
    base_version: Mapped[int] = mapped_column(default=0)
    data: Mapped[dict] = mapped_column(JSON)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    source_version_id: Mapped[str | None] = mapped_column(ForeignKey('document_versions.id'), nullable=True)
    source_document_version: Mapped[int] = mapped_column(default=0)
    conflict: Mapped[bool] = mapped_column(default=False)
    note: Mapped[str] = mapped_column(Text, default='')
    origin: Mapped[str] = mapped_column(String(40), default='manual')
    fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default='pending', index=True)
    version: Mapped[int] = mapped_column(default=1)
    author_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    reviewer_id: Mapped[str | None] = mapped_column(ForeignKey('users.id'), nullable=True)
    reason: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[str] = mapped_column(String(40), default=db.utcnow)
    decided_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


class Audit(db.Base):
    __tablename__ = 'audit_events'
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=db.uid)
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'))
    action: Mapped[str] = mapped_column(String(40))
    entity_id: Mapped[str] = mapped_column(String(32))
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[str] = mapped_column(String(40), default=db.utcnow)


class ProposalInput(InputModel):
    kind: Literal['requirement'] = 'requirement'
    target_id: str | None = Field(default=None, max_length=32)
    base_version: int = Field(default=0, ge=0)
    data: FactData
    conflict: bool = False
    note: str = Field(default='', max_length=4000)


class ProposalEdit(ProposalInput):
    expected_version: int = Field(ge=1)


def serial(proposal):
    return {key: getattr(proposal, key) for key in ('id', 'project_id', 'kind', 'target_id', 'base_version',
        'data', 'evidence', 'source_version_id', 'conflict', 'note', 'origin', 'status', 'version',
        'created_at', 'decided_at', 'reason')}


def current(session, record):
    return session.scalar(select(Revision).where(Revision.record_id == record.id, Revision.number == record.version))


def validate_target(session, project_id, body):
    if body.target_id:
        target = session.get(Record, body.target_id)
        if not target or target.project_id != project_id or target.kind != body.kind:
            raise HTTPException(404, 'Цель изменения недоступна')
        if target.version != body.base_version:
            raise HTTPException(409, 'Цель уже изменилась')
    elif body.base_version:
        raise HTTPException(422, 'У нового требования нет базовой редакции')


def fingerprint_for(body, evidence, source_id, source_epoch=0):
    content = body.model_dump(mode='json', exclude={'evidence', 'expected_version'})
    content['conflict'] = body.conflict or bool(body.target_id)
    parts = [content, evidence or [], source_id]
    if source_epoch:
        parts.append(source_epoch)
    return hashlib.sha256(json.dumps(parts,
                                     sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def add_proposal(session, project_id, author_id, body, evidence=None, source=None, origin='manual'):
    db.write_lock(session)
    validate_target(session, project_id, body)
    document = session.get(Document, source.document_id) if source else None
    fingerprint = fingerprint_for(body, evidence, source.id if source else None, document.version if document else 0)
    existing = session.scalar(select(Proposal).where(Proposal.project_id == project_id,
                                                     Proposal.fingerprint == fingerprint))
    if existing:
        return existing
    proposal = Proposal(project_id=project_id, author_id=author_id, kind=body.kind, target_id=body.target_id,
        base_version=body.base_version, data=body.data.model_dump(mode='json'), evidence=evidence or [],
        source_version_id=source.id if source else None, source_document_version=document.version if document else 0,
        conflict=body.conflict or bool(body.target_id), note=body.note, origin=origin, fingerprint=fingerprint)
    session.add(proposal)
    session.flush()
    return proposal


def decide(session, proposal, body, user_id):
    if proposal.status != 'pending' or proposal.version != body.expected_version:
        raise HTTPException(409, 'Предложение уже изменилось, обновите список')
    changed = session.execute(update(Proposal).where(Proposal.id == proposal.id,
        Proposal.version == body.expected_version, Proposal.status == 'pending').values(
            version=body.expected_version + 1, status=body.decision, reviewer_id=user_id,
            reason=body.reason, decided_at=db.utcnow()))
    if changed.rowcount != 1:
        raise HTTPException(409, 'Решение уже принято другим участником')
    if body.decision == 'accepted':
        if proposal.source_version_id:
            source = session.get(DocumentVersion, proposal.source_version_id)
            document = session.get(Document, source.document_id)
            if document.lifecycle != 'active' or document.current_number != source.number:
                raise HTTPException(409, 'Источник изменился или исключен из работы; нужен повторный анализ')
        if proposal.target_id:
            record = session.get(Record, proposal.target_id)
            if record.project_id != proposal.project_id or record.kind != proposal.kind:
                raise HTTPException(409, 'Цель изменения недоступна')
            advanced = session.execute(update(Record).where(Record.id == record.id,
                Record.version == proposal.base_version).values(version=proposal.base_version + 1))
            if advanced.rowcount != 1:
                raise HTTPException(409, 'Принятая редакция уже изменилась')
            number = proposal.base_version + 1
        else:
            record = Record(project_id=proposal.project_id, kind=proposal.kind)
            session.add(record)
            session.flush()
            proposal.target_id = record.id
            number = 1
        data = FactData.model_validate(proposal.data).model_dump(mode='json')
        session.add(Revision(record_id=record.id, project_id=proposal.project_id, number=number,
            data=data, evidence=proposal.evidence, author_id=user_id, reason=body.reason))
    session.add(Audit(project_id=proposal.project_id, user_id=user_id, action='proposal_' + body.decision,
        entity_id=proposal.id, details={'target_id': proposal.target_id, 'base_version': proposal.base_version}))


@router.post('/api/projects/{project_id}/proposals', status_code=201)
def manual_proposal(project_id: str, body: ProposalInput, who: Identity = Depends(identity),
                    session: Session = Depends(db.session_db)):
    project_access(project_id, session, who, 'edit')
    proposal = add_proposal(session, project_id, who.user.id, body)
    session.commit()
    return serial(proposal)


@router.patch('/api/proposals/{proposal_id}')
def edit_proposal(proposal_id: str, body: ProposalEdit, who: Identity = Depends(identity),
                  session: Session = Depends(db.session_db)):
    db.write_lock(session)
    proposal = session.get(Proposal, proposal_id)
    if not proposal:
        raise HTTPException(404, 'Предложение не найдено')
    project_access(proposal.project_id, session, who, 'edit')
    validate_target(session, proposal.project_id, body)
    fingerprint = fingerprint_for(body, proposal.evidence, proposal.source_version_id, proposal.source_document_version)
    existing = session.scalar(select(Proposal).where(Proposal.project_id == proposal.project_id,
        Proposal.fingerprint == fingerprint, Proposal.id != proposal.id))
    if existing:
        raise HTTPException(409, 'Такое предложение уже существует в этом проекте')
    changed = session.execute(update(Proposal).where(Proposal.id == proposal_id, Proposal.status == 'pending',
        Proposal.version == body.expected_version).values(data=body.data.model_dump(mode='json'),
        target_id=body.target_id, base_version=body.base_version, conflict=body.conflict or bool(body.target_id),
        note=body.note, fingerprint=fingerprint, version=body.expected_version + 1))
    if changed.rowcount != 1:
        raise HTTPException(409, 'Предложение уже изменилось')
    session.add(Audit(project_id=proposal.project_id, user_id=who.user.id, action='proposal_edited',
                      entity_id=proposal.id))
    session.commit()
    return serial(proposal)


@router.get('/api/projects/{project_id}/proposals')
def proposals(project_id: str, status: Literal['pending', 'accepted', 'rejected'] = 'pending',
              limit: int = Query(100, ge=1, le=200), offset: int = Query(0, ge=0),
              who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    project_access(project_id, session, who)
    query = select(Proposal).where(Proposal.project_id == project_id, Proposal.status == status)
    return {'items': [serial(p) for p in session.scalars(query.order_by(Proposal.created_at.desc())
                                                      .limit(limit).offset(offset))],
            'total': session.scalar(select(func.count()).select_from(query.subquery()))}


@router.post('/api/proposals/{proposal_id}/decision')
def decision(proposal_id: str, body: DecisionInput, who: Identity = Depends(identity),
             session: Session = Depends(db.session_db)):
    proposal = session.get(Proposal, proposal_id)
    if not proposal:
        raise HTTPException(404, 'Предложение не найдено')
    project_access(proposal.project_id, session, who, 'review')
    decide(session, proposal, body, who.user.id)
    session.commit()
    return serial(proposal)


@router.post('/api/projects/{project_id}/decisions')
def batch_decision(project_id: str, body: BatchDecisionInput, who: Identity = Depends(identity),
                   session: Session = Depends(db.session_db)):
    project_access(project_id, session, who, 'review')
    if len({item.id for item in body.items}) != len(body.items):
        raise HTTPException(422, 'Повторяющиеся предложения в наборе')
    for item in body.items:
        proposal = session.get(Proposal, item.id)
        if not proposal or proposal.project_id != project_id:
            raise HTTPException(404, 'Предложение недоступно')
        if proposal.conflict:
            raise HTTPException(422, 'Противоречие проверяется только индивидуально')
        decide(session, proposal, DecisionInput(decision=body.decision,
            expected_version=item.expected_version, reason=body.reason), who.user.id)
    session.commit()
    return {'count': len(body.items)}


@router.get('/api/projects/{project_id}/requirements')
def requirements(project_id: str, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    project_access(project_id, session, who)
    return [{'id': r.id, 'kind': r.kind, 'version': r.version, 'data': current(session, r).data,
             'evidence': current(session, r).evidence} for r in session.scalars(select(Record).where(
                 Record.project_id == project_id, Record.kind == 'requirement').order_by(Record.created_at))]


@router.post('/api/documents/{document_id}/analyze')
def enqueue_analysis(document_id: str, body: ReparseInput, who: Identity = Depends(identity),
                     session: Session = Depends(db.session_db)):
    db.write_lock(session)
    document = accessible_document(document_id, session, who)
    project_access(document.project_id, session, who, 'edit')
    if document.lifecycle != 'active' or document.version != body.expected_version:
        raise HTTPException(409, 'Источник изменился или исключен из работы')
    source = session.scalar(select(DocumentVersion).where(DocumentVersion.document_id == document.id,
        DocumentVersion.number == document.current_number))
    if not source.text or source.status in {'queued', 'parsing'}:
        raise HTTPException(422, 'Сначала необходимо извлечь текст документа')
    job = session.scalar(select(Job).where(Job.version_id == source.id, Job.kind == 'analyze'))
    if job is None:
        job = Job(project_id=document.project_id, version_id=source.id, kind='analyze')
        session.add(job)
    elif job.status in {'failed', 'cancelled'}:
        job.status, job.error, job.attempts = 'queued', '', 0
    session.commit()
    return {'job_id': job.id, 'status': job.status}


@router.get('/api/projects/{project_id}/jobs')
def jobs(project_id: str, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    project_access(project_id, session, who)
    return [{'id': j.id, 'kind': j.kind, 'status': j.status, 'cursor': j.cursor, 'error': j.error,
             'version_id': j.version_id} for j in session.scalars(select(Job).where(Job.project_id == project_id,
        Job.kind != 'parse').order_by(Job.updated_at.desc()).limit(100))]
