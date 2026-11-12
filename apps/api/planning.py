"""Project work uses the same review and immutable history as requirements."""
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from . import db
from .auth import Identity, identity, project_access
from .documents import Document, DocumentVersion, Occurrence
from .knowledge import Audit, Proposal, ProposalInput, Record, add_proposal, current, serial

router = APIRouter(tags=['planning'])
STAGES = ['Съемка', 'Трасса', 'Гидравлика', 'План', 'Проектная и рабочая документация', 'Сметы', 'Экспертиза']


def deadline(data, as_of):
    if data.get('status') == 'cancelled' or data.get('task_state') == 'done' or data.get('stage_state') == 'approved':
        return 'closed'
    if not data.get('due_date'):
        return 'unknown'
    due = date.fromisoformat(data['due_date'])
    return 'overdue' if due < as_of else 'today' if due == as_of else 'planned'


def accepted_records(session, project_id, kind=None, as_of=None):
    as_of = as_of or datetime.now(timezone(timedelta(hours=3))).date()
    query = select(Record).where(Record.project_id == project_id)
    if kind:
        query = query.where(Record.kind == kind)
    items = []
    for record in session.scalars(query.order_by(Record.created_at, Record.id)):
        revision = current(session, record)
        items.append({'id': record.id, 'kind': record.kind, 'version': record.version, 'data': revision.data,
            'evidence': revision.evidence, 'confirmed_at': revision.created_at,
            'deadline': deadline(revision.data, as_of)})
    return items


@router.get('/api/projects/{project_id}/records')
def records(project_id: str, kind: Literal['requirement', 'stage', 'task', 'event'] | None = None,
            as_of: date | None = None, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    project_access(project_id, session, who)
    # as_of is a deadline simulation date, never a claim to reconstruct past record versions.
    return accepted_records(session, project_id, kind, as_of)


@router.post('/api/projects/{project_id}/stage-template')
def stage_template(project_id: str, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    project_access(project_id, session, who, 'edit')
    rows = [serial(add_proposal(session, project_id, who.user.id,
        ProposalInput(kind='stage', data={'title': title}), origin='template')) for title in STAGES]
    session.commit()
    return rows


@router.get('/api/projects/{project_id}/timeline')
def timeline(project_id: str, basis: Literal['registered', 'source', 'confirmed'] = 'registered',
             start: date | None = None, end: date | None = None,
             kind: Literal['document', 'requirement', 'stage', 'task', 'event', 'decision'] | None = None,
             stage_id: str | None = None, review: Literal['pending', 'accepted', 'rejected'] | None = None,
             limit: int = Query(50, ge=1, le=200), offset: int = Query(0, ge=0),
             who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    project_access(project_id, session, who)
    rows = []
    if start and end and start > end:
        raise HTTPException(422, 'Начало периода позже окончания')
    if not stage_id and not review and (kind is None or kind == 'document'):
        for source, document in session.execute(select(DocumentVersion, Document).join(Document,
                Document.id == DocumentVersion.document_id).where(Document.project_id == project_id,
                Document.lifecycle != 'purged')):
            from .search import source_date
            occurrences = list(session.scalars(select(Occurrence).where(Occurrence.version_id == source.id)
                                               .order_by(Occurrence.received_at)))
            for occurrence in occurrences or [None]:
                rows.append({'id': occurrence.id if occurrence else source.id,
                    'version_id': source.id, 'entity_id': document.id, 'kind': 'document',
                    'title': document.title, 'number': source.number, 'lifecycle': document.lifecycle,
                    'registered': occurrence.received_at if occurrence else source.created_at,
                    'source': source_date(source.metadata_json.get('date')), 'confirmed': None,
                    'review': None, 'detail': {'status': source.status, 'warning': source.warning,
                        'channel': occurrence.channel if occurrence else 'unknown',
                        'source_name': occurrence.source_name if occurrence else source.filename}})
    for proposal in session.scalars(select(Proposal).where(Proposal.project_id == project_id)):
        if kind and kind != proposal.kind or review and review != proposal.status:
            continue
        if stage_id and proposal.target_id != stage_id and proposal.data.get('stage_id') != stage_id:
            continue
        rows.append({'id': proposal.id, 'entity_id': proposal.target_id, 'kind': proposal.kind,
            'title': proposal.data.get('title', ''), 'number': proposal.base_version + 1,
            'registered': proposal.created_at, 'source': proposal.data.get('source_date'),
            'confirmed': proposal.decided_at if proposal.status == 'accepted' else None,
            'review': proposal.status, 'detail': serial(proposal)})
    if not stage_id and not review and (kind is None or kind == 'decision'):
        for event in session.scalars(select(Audit).where(Audit.project_id == project_id)):
            rows.append({'id': event.id, 'entity_id': event.entity_id, 'kind': 'decision', 'title': event.action,
                'registered': event.created_at, 'source': None, 'confirmed': event.created_at,
                'review': None, 'detail': event.details})
    def day(row):
        value = row[basis]
        if not value:
            return ''
        if len(value) == 10:
            return value
        from datetime import datetime, timedelta, timezone
        return datetime.fromisoformat(value).astimezone(timezone(timedelta(hours=3))).date().isoformat()
    rows = [row for row in rows if (not start or day(row) >= start.isoformat())
            and (not end or (day(row) and day(row) <= end.isoformat()))]
    rows.sort(key=lambda row: (row[basis] or '', row['id']), reverse=True)
    return {'items': rows[offset:offset + limit], 'total': len(rows),
            'unknown_dates': sum(not row[basis] for row in rows)}
