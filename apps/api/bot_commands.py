"""Linked messenger identities, scoped questions and one-time reviewed changes."""
import json
import secrets
from datetime import UTC, datetime, timedelta

from fastapi import HTTPException
from sqlalchemy import select

from . import db
from .assistant import ChatTurn, Question, ask
from .auth import Identity, digest, project_access
from .changes import ChangeRequest, prepare_change
from .contracts import DecisionInput
from .integrations import BotActor, BotTicket, active
from .knowledge import DATA_TYPES, Proposal, Record, current, decide


def command(snapshot, sender, peer, text):
    if not text.startswith('/'):
        return None
    operation, _, arguments = text.partition(' ')
    operation = operation.split('@')[0].lower()
    arguments = arguments.strip()
    with db.SessionLocal() as session:
        db.write_lock(session)
        active(session, snapshot)
        if operation == '/link':
            ticket = session.get(BotTicket, digest(arguments))
            if (not ticket or ticket.integration_id != snapshot['id'] or ticket.proposal_id or ticket.sender != sender
                    or ticket.used or ticket.expires_at < db.utcnow()):
                raise HTTPException(409, 'Код привязки недействителен')
            user = session.get(db.User, ticket.user_id)
            if not user or not user.active:
                raise HTTPException(403, 'Учетная запись отключена')
            project_access(snapshot['project_id'], session, Identity(user, None))
            actor = session.get(BotActor, (snapshot['id'], sender))
            if actor:
                if actor.user_id != ticket.user_id:
                    raise HTTPException(409, 'Отправитель уже связан с другим аккаунтом')
            else:
                session.add(BotActor(integration_id=snapshot['id'], sender=sender, user_id=user.id))
            ticket.used = True
            session.commit()
            return 'Аккаунт связан с проектом. /help покажет команды.'
        if operation == '/help':
            return ('В разделе Источники получите код и отправьте /link с этим кодом.\n'
                '/ask вопрос: источники проекта без облачного AI.\n/list requirement: карточки вида requirement, stage, task, event, risk, policy.\n'
                '/change ID текст: проект изменения выбранной карточки через GigaChat.\n'
                '/new task текст: проект новой карточки через GigaChat.\n'
                '/confirm код или /reject код: отдельное решение по показанному предложению.\n'
                'Обычные сообщения и документы принимаются как источники, а не подтвержденные требования.')
        actor = session.get(BotActor, (snapshot['id'], sender))
        user = session.get(db.User, actor.user_id) if actor else None
        if not user or not user.active:
            return 'Сначала свяжите аккаунт через /link с одноразовым кодом из раздела Источники.'
        who = Identity(user, None)
        project_access(snapshot['project_id'], session, who)
        if operation in {'/confirm', '/reject'}:
            project_access(snapshot['project_id'], session, who, 'review')
            ticket = session.get(BotTicket, digest(arguments))
            if (not ticket or ticket.integration_id != snapshot['id'] or ticket.user_id != user.id or ticket.sender != sender
                    or not ticket.proposal_id or ticket.used or ticket.expires_at < db.utcnow()):
                raise HTTPException(409, 'Подтверждение недействительно или устарело')
            proposal = session.get(Proposal, ticket.proposal_id)
            if not proposal or proposal.project_id != snapshot['project_id']:
                raise HTTPException(404, 'Предложение недоступно')
            decide(session, proposal, DecisionInput(decision='accepted' if operation == '/confirm' else 'rejected',
                expected_version=ticket.proposal_version, reason='Отдельное подтверждение в мессенджере'), user.id)
            ticket.used = True
            session.commit()
            return 'Решение сохранено в истории проекта.'
        # Provider and search operate without a transaction held by this dispatcher.
        session.rollback()
        if operation == '/ask':
            if not 2 <= len(arguments) <= 12000:
                return 'Укажите вопрос от2 до12000 символов.'
            result = ask(snapshot['project_id'], Question(text=arguments, use_ai=False), who, session)
            turn = session.get(ChatTurn, result['id'])
            return {'text': result['answer'] + '\nОснования доступны в личном чате веб-интерфейса.',
                    'source_ids': list(turn.source_ids), 'chat_turn_id': turn.id,
                    'answer_hash': digest(result['answer']), 'user_id': user.id}
        if operation == '/list':
            if arguments not in DATA_TYPES or arguments == 'document':
                return 'Выберите вид: requirement, stage, task, event, risk, policy.'
            rows = list(session.scalars(select(Record).where(Record.project_id == snapshot['project_id'], Record.kind == arguments)))
            return {'text': '\n'.join(row.id + ' | ' + current(session, row).data['title'] for row in rows) or 'Подтвержденных карточек этого вида пока нет.',
                    'user_id': user.id}
        if operation not in {'/change', '/new'}:
            return 'Неизвестная команда. /help покажет доступные действия.'
        project_access(snapshot['project_id'], session, who, 'edit')
        key, _, instruction = arguments.partition(' ')
        if len(instruction.strip()) < 2:
            return 'Укажите вид/ID карточки и конкретное изменение.'
        if operation == '/new':
            if key not in DATA_TYPES or key == 'document':
                return 'Неизвестный вид карточки.'
            request = ChangeRequest(kind=key, text=instruction, use_ai=True)
        else:
            if len(key) < 8 or len(key) > 32 or any(c not in '0123456789abcdef' for c in key):
                return 'Укажите ID карточки из /list, от8 символов.'
            records = list(session.scalars(select(Record).where(Record.project_id == snapshot['project_id'], Record.id.startswith(key))))
            if len(records) != 1:
                return 'ID не найден или неоднозначен. Уточните его через /list.'
            record = records[0]
            request = ChangeRequest(kind=record.kind, target_id=record.id, expected_version=record.version, text=instruction, use_ai=True)
        result = prepare_change(snapshot['project_id'], request, who, session, guard=lambda transaction: active(transaction, snapshot))
        if result['clarification']:
            return result['clarification']
        proposal = result['proposal']
        if proposal['status'] != 'pending':
            return 'Такое предложение уже рассмотрено. Текущее решение доступно в веб-интерфейсе.'
        db.write_lock(session)
        session.expire_all()
        active(session, snapshot)
        project_access(snapshot['project_id'], session, who, 'edit')
        token = secrets.token_urlsafe(18)
        session.add(BotTicket(token_hash=digest(token), integration_id=snapshot['id'], user_id=user.id, sender=sender,
            proposal_id=proposal['id'], proposal_version=proposal['version'],
            expires_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat()))
        session.commit()
        text = ('Предложение требует отдельного решения. Было:\n' + json.dumps(result.get('before', {}), ensure_ascii=False, indent=2)
            + '\nПредлагается:\n' + json.dumps(proposal['data'], ensure_ascii=False, indent=2)
            + '\nПодтвердить в течение10минут: /confirm ' + token + '\nОтклонить: /reject ' + token)
        return {'text': text, 'user_id': user.id, 'proposal_id': proposal['id'], 'proposal_version': proposal['version']}
