"""Local risk suggestions and an internal inbox, without external messaging."""
from datetime import UTC, date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, select
from sqlalchemy.orm import Mapped, Session, mapped_column

from . import db
from .auth import Identity, identity, project_access
from .documents import Document, DocumentVersion
from .knowledge import PolicyData, ProposalInput, Record, add_proposal, current, serial
from .planning import deadline
from .search import Chunk, eligible_query

router = APIRouter(tags=['risks'])
MOSCOW = timezone(timedelta(hours=3))


class Notification(db.Base):
    __tablename__ = 'notifications'
    __table_args__ = (UniqueConstraint('record_id', 'record_version', 'user_id'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=db.uid)
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    record_id: Mapped[str] = mapped_column(ForeignKey('records.id'), index=True)
    record_version: Mapped[int] = mapped_column()
    user_id: Mapped[str] = mapped_column(ForeignKey('users.id'), index=True)
    title: Mapped[str] = mapped_column(Text)
    scheduled_at: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[str] = mapped_column(String(40), default=db.utcnow)
    read_at: Mapped[str | None] = mapped_column(String(40), nullable=True)


def policy(session, project_id):
    record = session.scalar(select(Record).where(Record.project_id == project_id, Record.kind == 'policy'))
    if record:
        config = PolicyData.model_validate(current(session, record).data)
        return record, config if config.status != 'cancelled' else PolicyData(title='Правила проекта')
    return None, PolicyData(title='Правила проекта')


def reminder_at(due: date, config: PolicyData):
    day = due - timedelta(days=1)
    holidays, working = set(config.holidays), set(config.working_dates)
    while day not in working and (day.weekday() >= 5 or day in holidays):
        day -= timedelta(days=1)
    return datetime.combine(day, time(9), MOSCOW)


def notify_due(now=None):
    now = now or datetime.now(UTC)
    count = 0
    with db.SessionLocal() as session:
        db.write_lock(session)
        for project_id in session.scalars(select(db.Project.id)):
            _, config = policy(session, project_id)
            for record in session.scalars(select(Record).where(Record.project_id == project_id,
                    Record.kind.in_(['requirement', 'stage', 'task', 'risk']))):
                data = current(session, record).data
                if not data.get('due_date') or deadline(data, now.astimezone(MOSCOW).date()) == 'closed':
                    continue
                # date.min has no previous day and cannot be a practical project deadline.
                due = date.fromisoformat(data['due_date'])
                if due.year < 1900:
                    continue
                scheduled = reminder_at(due, config)
                if now < scheduled:
                    continue
                for user_id in session.scalars(select(db.Membership.user_id).where(db.Membership.project_id == project_id)):
                    existing = session.scalar(select(Notification.id).where(Notification.record_id == record.id,
                        Notification.record_version == record.version, Notification.user_id == user_id))
                    if not existing:
                        session.add(Notification(project_id=project_id, record_id=record.id,
                            record_version=record.version, user_id=user_id, title=f'{data["title"]}: срок {due.isoformat()}',
                            scheduled_at=scheduled.astimezone(UTC).isoformat()))
                        count += 1
        session.commit()
    return count


@router.get('/api/projects/{project_id}/risk-settings')
def risk_settings(project_id: str, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    project_access(project_id, session, who)
    record, config = policy(session, project_id)
    return {'id': record.id if record else None, 'version': record.version if record else 0,
        'data': config.model_dump(mode='json'),
        'available_categories': sorted(set(session.scalars(select(Document.category).where(
            Document.project_id == project_id, Document.lifecycle == 'active')))),
        'calendar_notice': 'Суббота и воскресенье нерабочие. Праздники и рабочие переносы задайте явно в календаре проекта.'}


@router.post('/api/projects/{project_id}/scan-risks')
def scan_risks(project_id: str, offset: int = Query(0, ge=0),
               who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    project_access(project_id, session, who, 'edit')
    db.write_lock(session)
    _, config = policy(session, project_id)
    chunks = list(session.scalars(eligible_query(project_id).where(Chunk.document_version_id.is_not(None))
                                 .order_by(Chunk.document_version_id, Chunk.ordinal)))
    items, seen, matched, more = [], set(), 0, False
    for chunk in chunks:
        for rule in config.pattern_rules:
            key = (chunk.document_version_id, rule.name)
            if not rule.enabled or key in seen or not any(word in chunk.text.lower() for word in rule.keywords):
                continue
            seen.add(key)
            matched += 1
            if matched <= offset:
                continue
            if len(items) >= 100:
                more = True
                break
            source = session.get(DocumentVersion, chunk.document_version_id)
            body = ProposalInput(kind='risk', data={'title': f'{rule.name}: {source.filename}'[:500],
                'text': 'Обнаружено совпадение с настроенным паттерном. Значимость и актуальность нужно проверить.',
                'category': rule.category, 'priority': rule.priority}, note='Локальный паттерн: ' + rule.name)
            result = add_proposal(session, project_id, who.user.id, body,
                [{'version_id': source.id, 'document_id': source.document_id, 'locator': chunk.locator, 'quote': chunk.text}],
                source, origin='pattern')
            items.append(serial(result))
        if more:
            break
    present = set(session.scalars(select(Document.category).where(Document.project_id == project_id,
                                                                  Document.lifecycle == 'active')))
    for category in config.required_categories:
        if category in present:
            continue
        items.append(serial(add_proposal(session, project_id, who.user.id, ProposalInput(kind='risk',
            data={'title': 'Не найден раздел: ' + category, 'category': 'Полнота', 'priority': 'high',
                  'text': 'В активной библиотеке нет документа с этой категорией. Проверьте классификацию и комплектность.'},
            note='Проверка настроенного перечня обязательных категорий'), origin='pattern')))
    session.commit()
    return {'items': items, 'next_offset': offset + 100 if more else None,
            'notice': 'Показаны кандидаты на риски. Подтвердите актуальные в разделе проверки.'}


@router.get('/api/projects/{project_id}/notifications')
def notifications(project_id: str, limit: int = Query(100, ge=1, le=200),
                  who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    project_access(project_id, session, who)
    rows = session.execute(select(Notification, Record).join(Record, Record.id == Notification.record_id).where(
        Notification.project_id == project_id, Notification.user_id == who.user.id,
        Record.project_id == project_id,
        Notification.record_version == Record.version).order_by(Notification.created_at.desc()).limit(limit))
    return [{key: getattr(n, key) for key in ('id', 'title', 'record_id', 'record_version', 'scheduled_at', 'created_at', 'read_at')}
        for n, record in rows if deadline(current(session, record).data, datetime.now(MOSCOW).date()) != 'closed']


@router.post('/api/notifications/{notification_id}/read')
def read_notification(notification_id: str, who: Identity = Depends(identity), session: Session = Depends(db.session_db)):
    row = session.get(Notification, notification_id)
    if not row or row.user_id != who.user.id:
        raise HTTPException(404, 'Уведомление недоступно')
    project_access(row.project_id, session, who)
    row.read_at = row.read_at or db.utcnow()
    session.commit()
    return {'read_at': row.read_at}
