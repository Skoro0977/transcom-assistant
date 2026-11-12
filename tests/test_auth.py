import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from apps.api import db, documents
from apps.api.main import app
from apps.api.manage import seed
from apps.api.settings import settings, validate_database_location


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, 'worker_enabled', False)
    engine = create_engine('sqlite:///' + str(tmp_path / 'auth.db'),
                           connect_args={'check_same_thread': False})

    @event.listens_for(engine, 'connect')
    def fk(connection, _):
        connection.execute('PRAGMA foreign_keys=ON')

    db.Base.metadata.create_all(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db, 'SessionLocal', sessions)
    with sessions() as session:
        accounts = seed(session, save_credentials=False)
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        yield client, accounts
    engine.dispose()


def test_sessions_csrf_origin_and_project_access(isolated):
    client, accounts = isolated
    assert len(accounts) == 15
    assert sum(a['role'] == 'admin' for a in accounts) == 10
    assert client.get('/api/projects').status_code == 401
    assert client.post('/api/auth/login', json={'username': 'admin01', 'password': 'wrong'}).status_code == 401
    first = client.post('/api/auth/login', json={k: accounts[0][k] for k in ('username', 'password')})
    assert first.status_code == 200
    assert 'password' not in first.text
    assert 'HttpOnly' in first.headers['set-cookie']
    csrf = first.json()['csrf_token']
    assert client.post('/api/projects', json={'title': 'Закрытый'}).status_code == 403
    assert client.post('/api/projects', json={'title': 'Закрытый'},
                       headers={'X-CSRF-Token': csrf, 'Origin': 'https://attacker.invalid'}).status_code == 403
    project = client.post('/api/projects', json={'title': 'Закрытый'},
                          headers={'X-CSRF-Token': csrf}).json()
    assert 'id' in project
    second = client.post('/api/auth/login', json={k: accounts[-1][k] for k in ('username', 'password')})
    assert second.status_code == 200
    assert client.get('/api/projects/' + project['id'] + '/overview').status_code == 404
    assert client.post('/api/auth/logout', headers={'X-CSRF-Token': second.json()['csrf_token']}).status_code == 200
    assert client.get('/api/auth/me').status_code == 401


@pytest.mark.skipif(os.name != 'nt', reason='Windows disk policy')
def test_sqlite_drivers_cannot_write_to_c():
    for url in ['sqlite:///C:/transcom.db', 'sqlite+pysqlite:///C:/transcom.db',
                'sqlite:///file:C:/transcom.db?uri=true']:
        with pytest.raises(RuntimeError):
            validate_database_location(url)


def test_upload_duplicates_versions_and_private_files(isolated, tmp_path, monkeypatch):
    client, accounts = isolated
    storage = tmp_path / 'blobs'
    storage.mkdir()
    monkeypatch.setattr(documents, 'BLOBS', storage)
    login = client.post('/api/auth/login', json={k: accounts[0][k] for k in ('username', 'password')})
    headers = {'X-CSRF-Token': login.json()['csrf_token']}
    project = client.post('/api/projects', json={'title': 'Документы проверки'}, headers=headers).json()['id']
    url = '/api/projects/' + project + '/documents'
    first = client.post(url, files={'file': ('first.txt', b'Original source')}, headers=headers)
    assert first.status_code == 201
    first = first.json()
    duplicate = client.post(url, files={'file': ('copy.txt', b'Original source')}, headers=headers).json()
    assert duplicate['duplicate'] and duplicate['id'] == first['id']
    revised = client.post(url, params={'document_id': first['id'], 'expected_version': 1},
                          files={'file': ('next.txt', b'Changed source')}, headers=headers)
    assert revised.status_code == 201 and revised.json()['number'] == 2
    stale = client.post(url, params={'document_id': first['id'], 'expected_version': 1},
                        files={'file': ('stale.txt', b'Unseen change')}, headers=headers)
    assert stale.status_code == 409
    assert client.get('/api/documents/' + first['id'] + '/content', params={'number': 1}).content == b'Original source'
    assert len(list(storage.iterdir())) == 2
    reverted = client.post(url, params={'document_id': first['id'], 'expected_version': 2},
                           files={'file': ('return.txt', b'Original source')}, headers=headers)
    assert reverted.status_code == 201 and reverted.json()['number'] == 3
    assert not reverted.json()['duplicate']
    assert client.post(url, files={'file': ('program.exe', b'MZ')}, headers=headers).status_code == 422
    assert client.post('/api/documents/' + first['id'] + '/archive',
        json={'expected_version': 3, 'state': 'archived'}, headers=headers).status_code == 200
    assert client.post('/api/documents/' + first['id'] + '/archive',
        json={'expected_version': 4, 'state': 'active'}, headers=headers).status_code == 200
    with db.SessionLocal() as session:
        from sqlalchemy import select
        assert all(j.status == 'queued' for j in session.scalars(select(documents.Job).where(documents.Job.kind == 'parse')))
    client.post('/api/auth/login', json={k: accounts[-1][k] for k in ('username', 'password')})
    assert client.get('/api/documents/' + first['id'] + '/content').status_code == 404


def test_chunked_body_is_limited_before_parsing(monkeypatch):
    monkeypatch.setattr(settings, 'worker_enabled', False)
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        chunks = iter([b'{"username":"', b'x' * (1024 * 1024), b'","password":"x"}'])
        result = client.post('/api/auth/login', content=chunks, headers={'Content-Type': 'application/json'})
        assert result.status_code == 413


def test_mail_attachments_and_uncached_formulas(tmp_path):
    from email.message import EmailMessage

    from openpyxl import Workbook

    from apps.api.parse_file import parse
    mail = EmailMessage()
    mail['Subject'] = 'Проверка основания'
    mail.set_content('Новый срок 15 декабря.\nFrom: old@example.org\nПредыдущая переписка')
    mail.add_attachment(b'original', maintype='application', subtype='octet-stream', filename='../source.txt')
    source = tmp_path / 'letter.eml'
    source.write_bytes(mail.as_bytes())
    result = parse(source, tmp_path)
    assert any(s['quoted'] for s in result['segments'])
    assert result['attachments'][0]['name'] == 'source.txt'
    assert Path(result['attachments'][0]['path']).read_bytes() == b'original'
    sheet = Workbook()
    sheet.active['A1'] = '=1+1'
    sheet.active['B2'] = 'Не в первой ячейке'
    book = tmp_path / 'formula.xlsx'
    sheet.save(book)
    sheet.close()
    result = parse(book, tmp_path)
    assert result['status'] == 'needs_preparation'
    assert result['metadata']['formulas_without_cache'] == 1
    mail = EmailMessage()
    mail.set_content('From: old@example.org\nТолько пересланная переписка')
    source.write_bytes(mail.as_bytes())
    assert all(s['quoted'] for s in parse(source, tmp_path)['segments'] if s['locator'] != 'Тема письма')


def test_decisions_are_atomic_versioned_and_individual_for_conflicts(isolated):
    client, accounts = isolated
    login = client.post('/api/auth/login', json={k: accounts[0][k] for k in ('username', 'password')}).json()
    headers = {'X-CSRF-Token': login['csrf_token']}
    project = client.post('/api/projects', json={'title': 'Проверка решений'}, headers=headers).json()['id']
    url = '/api/projects/' + project + '/proposals'
    first = client.post(url, json={'data': {'title': 'Получить съемку', 'due_date': '2026-11-20'}}, headers=headers).json()
    assert client.get('/api/projects/' + project + '/requirements').json() == []
    accepted = client.post('/api/proposals/' + first['id'] + '/decision',
        json={'decision': 'accepted', 'expected_version': 1}, headers=headers)
    assert accepted.status_code == 200
    record = client.get('/api/projects/' + project + '/requirements').json()[0]
    assert record['version'] == 1
    assert client.post('/api/proposals/' + first['id'] + '/decision',
        json={'decision': 'accepted', 'expected_version': 1}, headers=headers).status_code == 409
    changes = []
    for deadline in ['2026-11-21', '2026-11-22']:
        changes.append(client.post(url, json={'target_id': record['id'], 'base_version': 1,
            'data': {'title': 'Получить съемку', 'due_date': deadline}}, headers=headers).json())
    normal = client.post(url, json={'data': {'title': 'Проверить исходные данные'}}, headers=headers).json()
    edited = client.patch('/api/proposals/' + normal['id'],
        json={'expected_version': 1, 'data': {'title': 'Уточнить исходные данные'}}, headers=headers)
    assert edited.status_code == 200
    original_again = client.post(url, json={'data': {'title': 'Проверить исходные данные'}}, headers=headers).json()
    assert original_again['id'] != normal['id'] and original_again['data']['title'] == 'Проверить исходные данные'
    edited_again = client.post(url, json={'data': {'title': 'Уточнить исходные данные'}}, headers=headers).json()
    assert edited_again['id'] == normal['id']
    normal = original_again
    batch = client.post('/api/projects/' + project + '/decisions', json={'decision': 'accepted',
        'items': [{'id': normal['id'], 'expected_version': 1}, {'id': changes[0]['id'], 'expected_version': 1}]}, headers=headers)
    assert batch.status_code == 422
    assert len(client.get('/api/projects/' + project + '/requirements').json()) == 1
    assert client.post('/api/proposals/' + changes[0]['id'] + '/decision',
        json={'decision': 'accepted', 'expected_version': 1}, headers=headers).status_code == 200
    assert client.post('/api/proposals/' + changes[1]['id'] + '/decision',
        json={'decision': 'accepted', 'expected_version': 1}, headers=headers).status_code == 409
    result = client.get('/api/projects/' + project + '/requirements').json()[0]
    assert result['version'] == 2 and result['data']['due_date'] == '2026-11-21'
    history = client.get('/api/records/' + record['id'] + '/history').json()
    assert [r['number'] for r in history] == [1, 2]
    assert history[1]['changes']['due_date'] == {'before': '2026-11-20', 'after': '2026-11-21'}
    current_hits = client.get('/api/projects/' + project + '/search', params={'q': 'съемку', 'semantic': False}).json()
    historical_hits = client.get('/api/projects/' + project + '/search', params={'q': 'съемку', 'semantic': False, 'mode': 'history'}).json()
    assert len(current_hits['items']) == 1 and len(historical_hits['items']) == 2
    with db.SessionLocal() as session:
        from sqlalchemy import select

        from apps.api.knowledge import Revision
        old = session.scalar(select(Revision).where(Revision.record_id == record['id'], Revision.number == 1))
        assert old.data['due_date'] == '2026-11-20'


def test_ai_must_ground_proposals_and_cannot_accept_them(isolated, tmp_path, monkeypatch):
    import json

    from sqlalchemy import select

    from apps.api.analyzer import analyze_job, giga
    client, accounts = isolated
    storage = tmp_path / 'blobs'
    storage.mkdir()
    monkeypatch.setattr(documents, 'BLOBS', storage)
    login = client.post('/api/auth/login', json={k: accounts[0][k] for k in ('username', 'password')}).json()
    headers = {'X-CSRF-Token': login['csrf_token']}
    project = client.get('/api/projects').json()[0]['id']
    doc = client.post('/api/projects/' + project + '/documents', files={'file': ('source.txt', b'source')}, headers=headers).json()
    with db.SessionLocal() as session:
        source = session.get(documents.DocumentVersion, doc['version_id'])
        source.status, source.text = 'ready', 'Передать съемку до 20 ноября.'
        source.segments = [{'text': source.text, 'locator': 'Текст', 'quoted': False}]
        session.commit()
    response = json.dumps({'items': [
        {'data': {'title': 'Передать съемку'}, 'evidence': [{'segment_index': 0, 'quote': 'Передать съемку'}]},
        {'data': {'title': 'Выдуманный факт'}, 'evidence': [{'segment_index': 0, 'quote': 'Этого текста нет'}]},
    ]})
    monkeypatch.setattr(giga, 'chat', lambda *args, **kwargs: response)
    job = client.post('/api/documents/' + doc['id'] + '/analyze', json={'expected_version': 1}, headers=headers).json()
    analyze_job(job['job_id'])
    repeated = client.post('/api/documents/' + doc['id'] + '/analyze', json={'expected_version': 1}, headers=headers).json()
    assert repeated['job_id'] == job['job_id'] and repeated['status'] == 'done'
    proposals = client.get('/api/projects/' + project + '/proposals').json()
    assert proposals['total'] == 1 and proposals['items'][0]['evidence'][0]['version_id'] == doc['version_id']
    assert client.get('/api/projects/' + project + '/requirements').json() == []
    with db.SessionLocal() as session:
        assert session.scalar(select(documents.Job).where(documents.Job.id == job['job_id'])).status == 'done'


def test_purge_requires_archive_and_confirmation_and_suppresses_reimport(isolated, tmp_path, monkeypatch):
    from sqlalchemy import select

    from apps.api.lifecycle import cleanup_blob
    client, accounts = isolated
    storage = tmp_path / 'blobs'
    storage.mkdir()
    monkeypatch.setattr(documents, 'BLOBS', storage)
    login = client.post('/api/auth/login', json={k: accounts[0][k] for k in ('username', 'password')}).json()
    headers = {'X-CSRF-Token': login['csrf_token']}
    project = client.get('/api/projects').json()[0]['id']
    url = '/api/projects/' + project + '/documents'
    item = client.post(url, files={'file': ('remove.txt', b'permanent-delete-test-only')}, headers=headers).json()
    base = '/api/documents/' + item['id']
    second = client.post(url, params={'document_id': item['id'], 'expected_version': 1},
                         files={'file': ('remove-v2.txt', b'second-permanent-test-version')}, headers=headers)
    assert second.status_code == 201 and len(list(storage.iterdir())) == 2
    proposal = client.post('/api/projects/' + project + '/proposals',
        json={'data': {'title': 'Решение для проверки удаления', 'text': 'Принятое решение'}}, headers=headers).json()
    with db.SessionLocal() as session:
        from apps.api.knowledge import Proposal
        accepted_source = session.get(Proposal, proposal['id'])
        accepted_source.source_version_id = second.json()['version_id']
        accepted_source.source_document_version = 2
        accepted_source.evidence = [{'version_id': second.json()['version_id'], 'document_id': item['id'],
                                    'locator': 'Текст', 'quote': 'Тестовая цитата'}]
        session.commit()
    assert client.post('/api/proposals/' + proposal['id'] + '/decision',
        json={'decision': 'accepted', 'expected_version': 1}, headers=headers).status_code == 200
    assert client.post(base + '/purge-challenge', json={'expected_version': 1}, headers=headers).status_code == 409
    archived = client.post(base + '/archive', json={'expected_version': 2, 'state': 'archived'}, headers=headers).json()
    assert archived['version'] == 3
    token = client.post(base + '/purge-challenge', json={'expected_version': 3}, headers=headers).json()['token']
    assert client.post(base + '/purge', json={'expected_version': 3, 'token': token, 'confirmation': 'нет'}, headers=headers).status_code == 422
    assert client.get(base + '/content').status_code == 200
    other = client.post('/api/auth/login', json={k: accounts[-1][k] for k in ('username', 'password')}).json()
    assert client.post(base + '/purge', json={'expected_version': 3, 'token': token, 'confirmation': 'УДАЛИТЬ'},
        headers={'X-CSRF-Token': other['csrf_token']}).status_code == 409
    login = client.post('/api/auth/login', json={k: accounts[0][k] for k in ('username', 'password')}).json()
    headers = {'X-CSRF-Token': login['csrf_token']}
    removed = client.post(base + '/purge', json={'expected_version': 3, 'token': token, 'confirmation': 'УДАЛИТЬ'}, headers=headers)
    assert removed.status_code == 200 and removed.json()['status'] == 'purge_queued'
    assert client.get(base + '/content').status_code == 404
    with db.SessionLocal() as session:
        job_ids = list(session.scalars(select(documents.Job.id).join(documents.DocumentVersion, documents.DocumentVersion.id == documents.Job.version_id).where(documents.DocumentVersion.document_id == item['id'], documents.Job.kind == 'purge')))
        assert session.get(documents.DocumentVersion, item['version_id']).text == ''
    for job_id in job_ids:
        cleanup_blob(job_id)
    assert len(job_ids) == 2
    retained = client.get('/api/projects/' + project + '/requirements').json()[0]
    assert retained['data']['text'] == 'Принятое решение'
    assert retained['evidence'][0]['removed'] and retained['evidence'][0]['quote'] == ''
    assert not list(storage.iterdir())
    assert client.post(url, files={'file': ('again.txt', b'permanent-delete-test-only')}, headers=headers).status_code == 409
    temporary = tmp_path / 'again.txt'
    temporary.write_bytes(b'permanent-delete-test-only')
    with db.SessionLocal() as session:
        result = documents.ingest(session, project, login['user']['id'],
                                  temporary, 'again.txt', channel='folder', external_id='test-suppression')
        assert result['suppressed']


def test_stale_workers_cannot_restore_purged_contents(isolated, tmp_path, monkeypatch):
    import json
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from types import SimpleNamespace

    from sqlalchemy import select

    from apps.api import analyzer, worker
    from apps.api.analyzer import analyze_job, giga
    from apps.api.knowledge import Proposal
    client, accounts = isolated
    storage = tmp_path / 'blobs'
    storage.mkdir()
    monkeypatch.setattr(documents, 'BLOBS', storage)
    login = client.post('/api/auth/login', json={k: accounts[0][k] for k in ('username', 'password')}).json()
    headers = {'X-CSRF-Token': login['csrf_token']}
    project = client.get('/api/projects').json()[0]['id']

    def remove(item):
        base = '/api/documents/' + item['id']
        assert client.post(base + '/archive', json={'expected_version': 1, 'state': 'archived'}, headers=headers).status_code == 200
        token = client.post(base + '/purge-challenge', json={'expected_version': 2}, headers=headers).json()['token']
        assert client.post(base + '/purge', json={'expected_version': 2, 'token': token, 'confirmation': 'УДАЛИТЬ'}, headers=headers).status_code == 200

    item = client.post('/api/projects/' + project + '/documents', files={'file': ('race.txt', b'parse-race-test')}, headers=headers).json()
    with db.SessionLocal() as session:
        parse_id = session.scalar(select(documents.Job.id).where(documents.Job.version_id == item['version_id']))

    def parser(args, **kwargs):
        remove(item)
        (Path(args[-1]) / 'result.json').write_text(json.dumps({'segments': [{'text': 'Removed text', 'locator': 'Text', 'quoted': False}],
            'metadata': {}, 'warning': '', 'status': 'ready', 'attachments': []}), encoding='utf-8')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(worker.subprocess, 'run', parser)
    worker.process(parse_id)
    with db.SessionLocal() as session:
        assert session.get(documents.DocumentVersion, item['version_id']).text == ''
    second = client.post('/api/projects/' + project + '/documents', files={'file': ('ai-race.txt', b'ai-race-test')}, headers=headers).json()
    with db.SessionLocal() as session:
        version = session.get(documents.DocumentVersion, second['version_id'])
        version.text, version.status = 'Explicit source requirement', 'ready'
        version.segments = [{'text': version.text, 'locator': 'Text', 'quoted': False}]
        session.commit()
    job = client.post('/api/documents/' + second['id'] + '/analyze', json={'expected_version': 1}, headers=headers).json()

    def response(*args, **kwargs):
        return json.dumps({'items': [{'data': {'title': 'Stale requirement'},
            'evidence': [{'segment_index': 0, 'quote': 'Explicit source requirement'}]}]})

    monkeypatch.setattr(giga, 'chat', response)
    publication, purge_attempt, purge_finished = Event(), Event(), Event()
    original_add = analyzer.add_proposal

    def guarded_add(*args, **kwargs):
        publication.set()
        assert purge_attempt.wait(5)
        assert not purge_finished.wait(0.2), 'Purge must wait for the publication transaction'
        return original_add(*args, **kwargs)

    def concurrent_remove():
        purge_attempt.set()
        try:
            remove(second)
        finally:
            purge_finished.set()

    monkeypatch.setattr(analyzer, 'add_proposal', guarded_add)
    with ThreadPoolExecutor(max_workers=2) as executor:
        analysis_future = executor.submit(analyze_job, job['job_id'])
        assert publication.wait(5)
        purge_future = executor.submit(concurrent_remove)
        analysis_future.result(timeout=10)
        purge_future.result(timeout=10)
    with db.SessionLocal() as session:
        assert session.get(documents.DocumentVersion, second['version_id']).text == ''
        assert not list(session.scalars(select(Proposal).where(Proposal.status == 'pending')))


def test_search_respects_versions_acl_archive_and_purge_during_embedding(isolated, tmp_path, monkeypatch):
    from sqlalchemy import select

    from apps.api.search import Chunk, embed_next, giga, index_version
    client, accounts = isolated
    storage = tmp_path / 'blobs'
    storage.mkdir()
    monkeypatch.setattr(documents, 'BLOBS', storage)
    monkeypatch.setattr(settings, 'gigachat_bulk_indexing_enabled', True)
    monkeypatch.setattr(giga, 'embeddings', lambda texts: [[1.0, 0.0] if '0.3' in text else [0.0, 1.0] for text in texts])
    login = client.post('/api/auth/login', json={k: accounts[0][k] for k in ('username', 'password')}).json()
    headers = {'X-CSRF-Token': login['csrf_token']}
    project = client.post('/api/projects', json={'title': 'Синтетический поиск'}, headers=headers).json()['id']
    url = '/api/projects/' + project
    item = client.post(url + '/documents', files={'file': ('source.txt', b'first')}, headers=headers).json()
    newer = client.post(url + '/documents', params={'document_id': item['id'], 'expected_version': 1},
        files={'file': ('source-v2.txt', b'second')}, headers=headers).json()
    with db.SessionLocal() as session:
        for key, text in [(item['version_id'], 'Давление газа 0.3 МПа'), (newer['version_id'], 'Давление газа 0.6 МПа')]:
            version = session.get(documents.DocumentVersion, key)
            version.text, version.status = text, 'ready'
            version.segments = [{'text': text, 'locator': 'Тестовый текст', 'quoted': False}]
            index_version(session, version)
        session.commit()
    assert embed_next()
    assert client.get(url + '/search', params={'q': 'давление', 'semantic': False}).json()['items'][0]['number'] == 2
    history = client.get(url + '/search', params={'q': 'давление', 'semantic': False, 'mode': 'history'}).json()
    assert len(history['items']) == 2
    semantic = client.get(url + '/search', params={'q': 'смысловой запрос', 'semantic': True, 'mode': 'history'}).json()
    assert len(semantic['items']) == 1 and semantic['items'][0]['number'] == 2
    base = '/api/documents/' + item['id']
    assert client.post(base + '/archive', json={'state': 'archived', 'expected_version': 2}, headers=headers).status_code == 200
    assert client.get(url + '/search', params={'q': 'давление', 'semantic': False}).json()['items'] == []
    assert client.post(base + '/archive', json={'state': 'active', 'expected_version': 3}, headers=headers).status_code == 200
    with db.SessionLocal() as session:
        chunk = session.scalar(select(Chunk).where(Chunk.document_version_id == newer['version_id']))
        chunk.state = 'pending'
        session.commit()

    def deleted_during_embedding(texts):
        client.post(base + '/archive', json={'state': 'archived', 'expected_version': 4}, headers=headers).raise_for_status()
        token = client.post(base + '/purge-challenge', json={'expected_version': 5}, headers=headers).json()['token']
        client.post(base + '/purge', json={'expected_version': 5, 'token': token, 'confirmation': 'УДАЛИТЬ'}, headers=headers).raise_for_status()
        return [[0.0, 1.0] for _ in texts]

    monkeypatch.setattr(giga, 'embeddings', deleted_during_embedding)
    assert embed_next()
    with db.SessionLocal() as session:
        assert not list(session.scalars(select(Chunk)))
    client.post('/api/auth/login', json={k: accounts[-1][k] for k in ('username', 'password')})
    assert client.get(url + '/search', params={'q': 'давление', 'semantic': False}).status_code == 404


def test_planning_typed_links_cycles_and_deadline_simulation(isolated):
    client, accounts = isolated
    login = client.post('/api/auth/login', json={k: accounts[0][k] for k in ('username', 'password')}).json()
    headers = {'X-CSRF-Token': login['csrf_token']}
    project = client.get('/api/projects').json()[0]['id']
    base = '/api/projects/' + project

    def accept(kind, data, target=None):
        response = client.post(base + '/proposals', headers=headers, json={'kind': kind, 'data': data,
            'target_id': target, 'base_version': 1 if target else 0})
        response.raise_for_status()
        proposal = response.json()
        result = client.post('/api/proposals/' + proposal['id'] + '/decision', headers=headers,
            json={'decision': 'accepted', 'expected_version': 1})
        result.raise_for_status()
        return result.json()['target_id']

    stage = accept('stage', {'title': 'Трасса', 'stage_state': 'correction'})
    task1 = accept('task', {'title': 'Проверить трассу', 'stage_id': stage, 'due_date': '2024-11-01'})
    task2 = accept('task', {'title': 'Согласовать трассу', 'depends_on': [task1]})
    cycle = client.post(base + '/proposals', headers=headers, json={'kind': 'task', 'target_id': task1,
        'base_version': 1, 'data': {'title': 'Неверный цикл', 'depends_on': [task2]}})
    assert cycle.status_code == 422
    wrong_kind = client.post(base + '/proposals', headers=headers, json={'kind': 'requirement',
        'data': {'title': 'Неверное поле', 'task_state': 'done'}})
    assert wrong_kind.status_code == 422
    tasks = client.get(base + '/records', params={'kind': 'task', 'as_of': '2024-10-01'}).json()
    assert next(r for r in tasks if r['id'] == task1)['deadline'] == 'planned'
    tasks = client.get(base + '/records', params={'kind': 'task', 'as_of': '2024-11-02'}).json()
    assert next(r for r in tasks if r['id'] == task1)['deadline'] == 'overdue'
    rows = client.get(base + '/timeline', params={'stage_id': stage, 'kind': 'task', 'review': 'accepted'}).json()
    assert rows['total'] == 1 and rows['items'][0]['title'] == 'Проверить трассу'
    other = client.post('/api/projects', json={'title': 'Другой объект'}, headers=headers).json()['id']
    assert client.post('/api/projects/' + other + '/proposals', headers=headers,
        json={'kind': 'task', 'data': {'title': 'Чужая связь', 'stage_id': stage}}).status_code == 422


def test_timeline_retains_every_receipt_of_identical_content(isolated, tmp_path, monkeypatch):
    from sqlalchemy import select
    client, accounts = isolated
    storage = tmp_path / 'blobs'
    storage.mkdir()
    monkeypatch.setattr(documents, 'BLOBS', storage)
    login = client.post('/api/auth/login', json={k: accounts[0][k] for k in ('username', 'password')}).json()
    headers = {'X-CSRF-Token': login['csrf_token']}
    project = client.get('/api/projects').json()[0]['id']
    base = '/api/projects/' + project
    first = client.post(base + '/documents', files={'file': ('first.txt', b'receipt-test')}, headers=headers).json()
    again = client.post(base + '/documents', files={'file': ('again.txt', b'receipt-test')}, headers=headers).json()
    assert first['version_id'] == again['version_id']
    with db.SessionLocal() as session:
        receipts = list(session.scalars(select(documents.Occurrence).where(documents.Occurrence.version_id == first['version_id'])))
        assert len(receipts) == 2
        receipts[0].received_at = '2024-01-01T12:00:00+00:00'
        receipts[1].received_at = '2024-02-01T12:00:00+00:00'
        session.commit()
    rows = client.get(base + '/timeline', params={'kind': 'document'}).json()
    assert rows['total'] == 2
    period = client.get(base + '/timeline', params={'kind': 'document', 'start': '2024-02-01', 'end': '2024-02-01'}).json()
    assert period['total'] == 1 and period['items'][0]['version_id'] == first['version_id']


def test_risk_patterns_human_review_and_business_day_reminders(isolated, tmp_path, monkeypatch):
    from datetime import UTC, date, datetime

    from apps.api.knowledge import PolicyData
    from apps.api.risks import notify_due, reminder_at
    from apps.api.search import index_version
    client, accounts = isolated
    storage = tmp_path / 'blobs'
    storage.mkdir()
    monkeypatch.setattr(documents, 'BLOBS', storage)
    login = client.post('/api/auth/login', json={k: accounts[0][k] for k in ('username', 'password')}).json()
    headers = {'X-CSRF-Token': login['csrf_token']}
    project = client.get('/api/projects').json()[0]['id']
    base = '/api/projects/' + project
    config = PolicyData(title='Правила', holidays=['2024-11-01'])
    assert reminder_at(date(2024, 11, 4), config).isoformat() == '2024-10-31T09:00:00+03:00'
    config.working_dates = [date(2024, 11, 2)]
    assert reminder_at(date(2024, 11, 4), config).isoformat() == '2024-11-02T09:00:00+03:00'

    def proposal(kind, data):
        response = client.post(base + '/proposals', headers=headers, json={'kind': kind, 'data': data})
        response.raise_for_status()
        return response.json()

    def accept(p):
        response = client.post('/api/proposals/' + p['id'] + '/decision', headers=headers,
            json={'decision': 'accepted', 'expected_version': 1})
        response.raise_for_status()
        return response.json()

    accept(proposal('policy', {'title': 'Правила', 'pattern_rules': [{'name': 'Отказ', 'keywords': ['отказ'], 'priority': 'high'}]}))
    source = client.post(base + '/documents', headers=headers, files={'file': ('letter.txt', 'Получен отказ в согласовании'.encode())}).json()
    with db.SessionLocal() as session:
        version = session.get(documents.DocumentVersion, source['version_id'])
        version.text, version.status = 'Получен отказ в согласовании', 'ready'
        version.segments = [{'text': version.text, 'locator': 'Текст', 'quoted': False}]
        index_version(session, version)
        session.commit()
    risks = client.post(base + '/scan-risks', headers=headers).json()['items']
    assert len(risks) == 1 and risks[0]['data']['priority'] == 'high' and risks[0]['evidence']
    assert client.get(base + '/records?kind=risk').json() == []
    assert client.post(base + '/scan-risks', headers=headers).json()['items'][0]['id'] == risks[0]['id']
    accept(proposal('task', {'title': 'Контрольный срок', 'due_date': '2024-11-04'}))
    assert notify_due(datetime(2024, 11, 1, 5, 59, tzinfo=UTC)) == 0
    assert notify_due(datetime(2024, 11, 1, 6, 0, tzinfo=UTC)) == 15
    assert notify_due(datetime(2024, 11, 1, 7, 0, tzinfo=UTC)) == 0
    notices = client.get(base + '/notifications').json()
    assert len(notices) == 1
    other = client.post('/api/auth/login', json={k: accounts[-1][k] for k in ('username', 'password')}).json()
    assert client.post('/api/notifications/' + notices[0]['id'] + '/read',
        headers={'X-CSRF-Token': other['csrf_token']}).status_code == 404


def test_chat_purge_late_response_and_summary_dates(isolated, tmp_path, monkeypatch):
    import json

    from sqlalchemy import select

    from apps.api.assistant import giga
    from apps.api.knowledge import Proposal, Revision
    from apps.api.search import index_version
    client, accounts = isolated
    storage = tmp_path / 'blobs'
    storage.mkdir()
    monkeypatch.setattr(documents, 'BLOBS', storage)
    login = client.post('/api/auth/login', json={k: accounts[0][k] for k in ('username', 'password')}).json()
    headers = {'X-CSRF-Token': login['csrf_token']}
    project = client.get('/api/projects').json()[0]['id']
    base = '/api/projects/' + project
    source = client.post(base + '/documents', headers=headers, files={'file': ('grounding.txt', 'Тестовое давление 0.3 МПа'.encode())}).json()
    with db.SessionLocal() as session:
        version = session.get(documents.DocumentVersion, source['version_id'])
        version.text, version.status = 'Тестовое давление 0.3 МПа', 'ready'
        version.segments = [{'text': version.text, 'locator': 'Тест', 'quoted': False}]
        index_version(session, version)
        session.commit()
    local = client.post(base + '/chat', json={'text': 'давление', 'use_ai': False}, headers=headers)
    assert local.status_code == 200 and local.json()['citations'][0]['reference'] == 1
    p = client.post(base + '/proposals', headers=headers, json={'kind': 'task',
        'data': {'title': 'Тестовая задача', 'due_date': '2024-11-04'}}).json()
    assert client.post('/api/proposals/' + p['id'] + '/decision', headers=headers,
        json={'decision': 'accepted', 'expected_version': 1}).status_code == 200
    with db.SessionLocal() as session:
        proposal = session.get(Proposal, p['id'])
        proposal.created_at, proposal.decided_at = '2024-01-01T12:00:00+00:00', '2024-02-01T12:00:00+00:00'
        revision = session.scalar(select(Revision).where(Revision.record_id == proposal.target_id))
        revision.created_at = proposal.decided_at
        session.commit()
    params = {'start': '2024-01-01', 'end': '2024-01-01'}
    assert len(client.get(base + '/summary', params=params).json()['changes']) == 1
    assert client.get(base + '/summary', params=dict(params, basis='confirmed')).json()['changes'] == []

    def removed_while_answering(*args, **kwargs):
        path = '/api/documents/' + source['id']
        client.post(path + '/archive', json={'expected_version': 1, 'state': 'archived'}, headers=headers).raise_for_status()
        token = client.post(path + '/purge-challenge', json={'expected_version': 2}, headers=headers).json()['token']
        client.post(path + '/purge', json={'expected_version': 2, 'token': token, 'confirmation': 'УДАЛИТЬ'}, headers=headers).raise_for_status()
        return json.dumps({'answer': 'Запоздалый ответ 0.3 МПа', 'references': [], 'insufficient': True})
    monkeypatch.setattr(giga, 'chat', removed_while_answering)
    late = client.post(base + '/chat', json={'text': 'давление', 'use_ai': True}, headers=headers)
    assert late.status_code == 409
    history = client.get(base + '/chat').json()
    assert len(history) == 1 and history[0]['citations'] == [] and '0.3' not in history[0]['answer']


def test_sources_project_folders_reversions_and_readonly_mail_cursors(isolated, tmp_path, monkeypatch):
    import time

    from sqlalchemy import func, select

    from apps.api import sources, vault
    client, accounts = isolated
    storage = tmp_path / 'blobs'
    storage.mkdir()
    monkeypatch.setattr(documents, 'BLOBS', storage)
    monkeypatch.setattr(sources, 'INBOX', tmp_path / 'inbox')
    monkeypatch.setattr(vault, 'VAULT', tmp_path / 'vault')
    login = client.post('/api/auth/login', json={k: accounts[0][k] for k in ('username', 'password')}).json()
    headers = {'X-CSRF-Token': login['csrf_token']}
    project = client.get('/api/projects').json()[0]['id']
    base = '/api/projects/' + project
    assert client.post(base + '/sources', json={'name': 'Неверный путь', 'kind': 'folder', 'path': str(tmp_path)}, headers=headers).status_code == 422
    root = Path(client.get(base + '/sources').json()['inbox_path'])
    feed = client.post(base + '/sources', json={'name': 'Входящие', 'kind': 'folder'}, headers=headers).json()
    assert feed['version'] == 1
    file = root / 'revision.txt'
    for n, content in enumerate((b'A', b'B', b'A')):
        file.write_bytes(content)
        os.utime(file, (time.time() - 10 - n, time.time() - 10 - n))
        assert client.post('/api/sources/' + feed['id'] + '/sync', headers=headers).status_code == 200
        assert sources.poll_next()
    with db.SessionLocal() as session:
        version = session.scalar(select(documents.DocumentVersion).order_by(documents.DocumentVersion.number.desc()))
        assert version.number == 3
        assert session.scalar(select(func.count()).select_from(documents.Document)) == 1
        before = session.scalar(select(func.count()).select_from(documents.Occurrence))
    client.post('/api/sources/' + feed['id'] + '/sync', headers=headers).raise_for_status()
    sources.poll_next()
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(documents.Occurrence)) == before

    mail = client.post(base + '/sources', headers=headers, json={'name': 'Тестовая почта', 'kind': 'gmail',
        'username': 'synthetic@example.test', 'password': 'synthetic-app-password'}).json()
    assert 'secret_ref' not in mail and mail['credentials_configured']
    assert b'synthetic-app-password' not in b''.join(p.read_bytes() for p in (tmp_path / 'vault').glob('*'))
    fail_second = True
    mailbox = 'INBOX'

    class FakeIMAP:
        def __init__(self, host, port, ssl_context, timeout):
            assert host == 'imap.gmail.com' and port == 993 and ssl_context.check_hostname

        def login(self, username, password):
            assert username == 'synthetic@example.test' and password == 'synthetic-app-password'

        def select(self, folder, readonly):
            assert readonly and folder == '"' + mailbox + '"'
            return 'OK', [b'2']

        def response(self, key):
            assert key == 'UIDVALIDITY'
            return key, [b'123']

        def uid(self, operation, uid, query):
            if operation == 'search':
                return 'OK', [b'1 2']
            if query == '(RFC822.SIZE)':
                if uid == '2' and fail_second:
                    return 'NO', []
                return 'OK', [b'1 (RFC822.SIZE 100)']
            assert query == '(BODY.PEEK[])'
            return 'OK', [(b'body', ('From: synthetic@example.test\r\nSubject: ' + uid + '\r\n\r\nSynthetic mail ' + mailbox + uid).encode())]

        def logout(self):
            return 'BYE', []

    monkeypatch.setattr(sources.imaplib, 'IMAP4_SSL', FakeIMAP)
    assert sources.poll_next()
    with db.SessionLocal() as session:
        record = session.get(sources.Source, mail['id'])
        assert record.status == 'error' and record.state['uid'] == 1
    fail_second = False
    client.post('/api/sources/' + mail['id'] + '/sync', headers=headers).raise_for_status()
    assert sources.poll_next()
    with db.SessionLocal() as session:
        record = session.get(sources.Source, mail['id'])
        assert record.state == {'uidvalidity': '123', 'uid': 2}
        assert session.scalar(select(func.count()).select_from(documents.Occurrence).where(documents.Occurrence.channel == 'imap')) == 2
        old_epoch = record.config['epoch']
    invalid = client.post(base + '/sources', headers=headers, json={'name': 'Ошибка', 'kind': 'gmail',
        'username': 'invalid', 'password': 'synthetic-sensitive-value', 'refresh_token': 'synthetic-sensitive-token'})
    assert invalid.status_code == 422 and 'synthetic-sensitive' not in invalid.text
    mailbox = 'OTHER'
    changed = client.patch('/api/sources/' + mail['id'], headers=headers, json={'name': mail['name'], 'kind': 'gmail',
        'username': 'synthetic@example.test', 'folder': mailbox, 'expected_version': mail['version']}).json()
    assert 'epoch' not in changed['config']
    assert sources.poll_next()
    with db.SessionLocal() as session:
        record = session.get(sources.Source, mail['id'])
        assert record.status == 'ready' and record.state['uid'] == 2 and record.config['epoch'] != old_epoch
        assert session.scalar(select(func.count()).select_from(documents.Occurrence).where(documents.Occurrence.channel == 'imap')) == 4
        new_epoch = record.config['epoch']
    client.patch('/api/sources/' + mail['id'], headers=headers, json={'name': 'Обновленное имя', 'kind': 'gmail',
        'username': 'synthetic@example.test', 'folder': mailbox, 'expected_version': changed['version']}).raise_for_status()
    with db.SessionLocal() as session:
        record = session.get(sources.Source, mail['id'])
        assert record.config['epoch'] == new_epoch and record.state['uid'] == 2
    from fastapi import HTTPException
    monkeypatch.setattr(sources, 'poll_mail', lambda _: (_ for _ in ()).throw(HTTPException(409, 'private details')))
    assert sources.poll_next()
    with db.SessionLocal() as session:
        record = session.get(sources.Source, mail['id'])
        assert record.status == 'error' and 'private details' not in record.notice
    client.post('/api/auth/login', json={k: accounts[-1][k] for k in ('username', 'password')})
    # This account shares the pilot, so verify another project rather than roles.
    with db.SessionLocal() as session:
        other = db.Project(title='Чужой проект')
        session.add(other)
        session.commit()
        other_id = other.id
    assert client.get('/api/projects/' + other_id + '/sources').status_code == 404

def test_chat_changes_require_confirmation_and_reject_stale_provider_results(isolated, tmp_path, monkeypatch):
    from apps.api import changes
    client, accounts = isolated
    storage = tmp_path / 'blobs'
    storage.mkdir()
    monkeypatch.setattr(documents, 'BLOBS', storage)
    login = client.post('/api/auth/login', json={k: accounts[0][k] for k in ('username', 'password')}).json()
    headers = {'X-CSRF-Token': login['csrf_token']}
    project = client.get('/api/projects').json()[0]['id']
    root = '/api/projects/' + project
    url = root + '/chat/changes'
    def request(**values):
        return client.post(url, headers=headers, json={'kind': 'requirement', 'text': 'Уточнить срок', **values})
    draft = request(patch={'title': 'Техническое условие', 'due_date': '2026-12-01'}).json()['proposal']
    assert not client.get(root + '/records').json()
    def accept(p):
        return client.post('/api/proposals/' + p['id'] + '/decision', headers=headers,
            json={'decision': 'accepted', 'expected_version': p['version']})
    assert accept(draft).status_code == 200
    record = client.get(root + '/records').json()[0]
    identity = {'target_id': record['id'], 'expected_version': 1}
    first = request(**identity, patch={'due_date': '2026-12-02'}).json()['proposal']
    second = request(**identity, patch={'owner': 'Руководитель'}).json()['proposal']
    assert accept(first).status_code == 200
    assert accept(second).status_code == 409
    assert request(**identity, patch={'due_date': '2026-12-03'}).status_code == 409
    assert request(target_id=record['id'], expected_version=2, patch={'project_id': 'forbidden'}).status_code == 422
    def stale_ai(*args, **kwargs):
        fresh = request(target_id=record['id'], expected_version=2, patch={'owner': 'Другой участник'}).json()['proposal']
        assert accept(fresh).status_code == 200
        return '{"patch":{"due_date":"2026-12-04"},"clarification":""}'
    monkeypatch.setattr(changes.giga, 'chat', stale_ai)
    assert request(target_id=record['id'], expected_version=2, use_ai=True).status_code == 409
    document = client.post(root + '/documents', files={'file': ('original.txt', b'Unchanged original')}, headers=headers).json()
    proposal = request(kind='document', target_id=document['id'], expected_version=1,
        patch={'title': 'Новое название', 'category': 'ТУ'}).json()['proposal']
    assert accept(proposal).status_code == 200
    assert client.get('/api/documents/' + document['id']).json()['category'] == 'ТУ'
    assert client.get('/api/documents/' + document['id'] + '/content').content == b'Unchanged original'
    assert all(r['kind'] != 'document' for r in client.get(root + '/records').json())
    from sqlalchemy import select

    from apps.api.knowledge import Revision
    with db.SessionLocal() as session:
        revision = session.scalar(select(Revision).where(Revision.record_id == record['id'], Revision.number == 3))
        revision.evidence = [{'version_id': document['version_id'], 'document_id': document['id'],
                              'quote': 'synthetic-private-evidence', 'locator': 'fixture'}]
        session.commit()
    pending = request(target_id=record['id'], expected_version=3, patch={'owner': 'Следующий'}).json()['proposal']
    assert client.post('/api/documents/' + document['id'] + '/archive', headers=headers,
        json={'expected_version': 2, 'state': 'archived'}).status_code == 200
    ticket = client.post('/api/documents/' + document['id'] + '/purge-challenge', headers=headers,
        json={'expected_version': 3}).json()
    assert client.post('/api/documents/' + document['id'] + '/purge', headers=headers,
        json={'expected_version': 3, 'token': ticket['token'], 'confirmation': 'УДАЛИТЬ'}).status_code == 200
    assert accept(pending).status_code == 409
    assert 'synthetic-private-evidence' not in client.get(root + '/proposals?status=rejected').text

def test_legacy_word_bounds_and_local_ocr(tmp_path):
    import struct

    from PIL import Image, ImageDraw, ImageFont

    from apps.api.local_tools import word_pieces
    from apps.api.parse_file import parse
    word = bytearray(1024)
    struct.pack_into('<HH', word, 0, 0xA5EC, 0xC1)
    struct.pack_into('<H', word, 32, 14)
    struct.pack_into('<H', word, 62, 22)
    struct.pack_into('<H', word, 152, 93)
    phrase = 'Срок 15 декабря 2026'
    encoded = phrase.encode('utf-16-le')
    word[512:512 + len(encoded)] = encoded
    piece = struct.pack('<IIHIH', 0, len(phrase), 0, 512, 0)
    clx = b'\x02' + struct.pack('<I', len(piece)) + piece
    struct.pack_into('<II', word, 418, 0, len(clx))
    assert word_pieces(word, clx) == phrase
    bad = bytearray(clx)
    struct.pack_into('<I', bad, 15, 0x3FFFFFFF)
    with pytest.raises(ValueError):
        word_pieces(word, bad)
    image = Image.new('RGB', (1800, 400), 'white')
    ImageDraw.Draw(image).text((50, 70), phrase, fill='black', font=ImageFont.truetype('C:/Windows/Fonts/arial.ttf', 64))
    source = tmp_path / 'synthetic.png'
    image.save(source)
    result = parse(source, tmp_path)
    assert result['metadata']['ocr_confidence'] > 80
    assert '2026' in result['segments'][0]['text'] and result['metadata']['ocr_review_required']
    pdf = tmp_path / 'scan.pdf'
    image.save(pdf, resolution=150)
    scanned = parse(pdf, tmp_path)
    assert scanned['metadata']['ocr_pages'] == [1] and not scanned['metadata']['scan_pages']
    assert '2026' in scanned['segments'][0]['text']
    kml = tmp_path / 'labels.kml'
    kml.write_text('<kml xmlns="http://www.opengis.net/kml/2.2"><Placemark><name>Трасса</name></Placemark></kml>', encoding='utf-8')
    assert parse(kml, tmp_path)['segments'][0]['text'] == 'Трасса'
    kml.write_text('<!DOCTYPE kml [<!ENTITY leak SYSTEM "file:///C:/Windows/win.ini">]><kml>&leak;</kml>', encoding='utf-8')
    from defusedxml.common import DefusedXmlException
    with pytest.raises(DefusedXmlException):
        parse(kml, tmp_path)

def test_incoming_api_and_linked_bots_are_scoped_idempotent_and_confirmed(isolated, tmp_path, monkeypatch):
    import re

    from sqlalchemy import func, select

    from apps.api import changes, integrations, vault
    from apps.api.knowledge import Proposal, Record, Revision
    client, accounts = isolated
    storage = tmp_path / 'blobs'; storage.mkdir()
    monkeypatch.setattr(documents, 'BLOBS', storage)
    monkeypatch.setattr(vault, 'VAULT', tmp_path / 'vault')
    login = client.post('/api/auth/login', json={k: accounts[0][k] for k in ('username', 'password')}).json()
    headers = {'X-CSRF-Token': login['csrf_token']}
    project = client.get('/api/projects').json()[0]['id']
    root = '/api/projects/' + project
    feed = client.post(root + '/integrations', headers=headers, json={'name': 'API', 'kind': 'api'}).json()
    url = '/api/incoming/' + feed['id'] + '/files'
    auth = {'Authorization': 'Bearer ' + feed['incoming_key']}
    assert client.post(url, files={'file': ('test.txt', b'A')}, data={'external_id': '1'}).status_code == 403
    first = client.post(url, files={'file': ('test.txt', b'A')}, data={'external_id': '1'}, headers=auth)
    assert first.status_code == 200
    assert client.post(url, files={'file': ('test.txt', b'A')}, data={'external_id': '1'}, headers=auth).json()['duplicate']
    assert client.post(url, files={'file': ('test.txt', b'B')}, data={'external_id': '1'}, headers=auth).status_code == 409
    assert 'incoming_key' not in client.get(root + '/integrations').text
    telegram = client.post(root + '/integrations', headers=headers, json={'name': 'Telegram', 'kind': 'telegram',
        'chat_ids': [10], 'bot_token': '123:synthetic', 'reply_enabled': True}).json()
    tgurl = '/api/incoming/' + telegram['id'] + '/telegram'
    tgh = {'X-Telegram-Bot-Api-Secret-Token': telegram['incoming_key']}
    responses = []
    monkeypatch.setattr(integrations, 'send_reply', lambda snapshot, peer, text: responses.append(text))
    def event(n, text, sender=21, message_id=None, edited=False):
        return {'update_id': n, 'edited_message' if edited else 'message': {'message_id': message_id or n,
            'date': 1794499200, 'chat': {'id': 10}, 'from': {'id': sender}, 'text': text}}
    assert client.post(tgurl, json=event(1, 'Original')).status_code == 403
    assert client.post(tgurl, json=event(1, 'Original'), headers=tgh).status_code == 200
    assert client.post(tgurl, json=event(1, 'Original'), headers=tgh).json()['duplicate']
    client.post(tgurl, json=event(2, 'Original'), headers=tgh).raise_for_status()
    client.post(tgurl, json=event(3, 'Changed', message_id=1, edited=True), headers=tgh).raise_for_status()
    with db.SessionLocal() as session:
        bound = list(session.scalars(select(integrations.IntegrationMessage)))
        assert len(bound) == 2 and bound[0].document_id != bound[1].document_id
        assert sorted(session.get(documents.Document, b.document_id).current_number for b in bound) == [1, 2]
    metadata_edit = event(30, 'Changed', message_id=1, edited=True)
    metadata_edit['edited_message']['edit_date'] = 1794499300
    client.post(tgurl, json=metadata_edit, headers=tgh).raise_for_status()
    with db.SessionLocal() as session:
        assert sorted(session.get(documents.Document, b.document_id).current_number for b in bound) == [1, 3]
    code = client.post('/api/integrations/' + telegram['id'] + '/link-code', headers=headers, json={'sender': '21'}).json()['command']
    client.post(tgurl, json=event(4, code, sender=22), headers=tgh).raise_for_status()
    assert 'не выполнено' in responses[-1]
    client.post(tgurl, json=event(5, code), headers=tgh).raise_for_status()
    assert 'связан' in responses[-1]
    monkeypatch.setattr(changes.giga, 'chat', lambda *a, **k: '{"patch":{"title":"Задача из чата","due_date":"2026-12-01"},"clarification":""}')
    import httpx
    fail_delivery = True
    def interrupted_delivery(snapshot, peer, text):
        nonlocal fail_delivery
        if fail_delivery:
            fail_delivery = False
            raise httpx.ConnectError('synthetic delivery failure')
        responses.append(text)
    monkeypatch.setattr(integrations, 'send_reply', interrupted_delivery)
    new_event = event(6, '/new task Назначить работу')
    assert client.post(tgurl, json=new_event, headers=tgh).status_code == 503
    assert client.post(tgurl, json=new_event, headers=tgh).json()['duplicate']
    confirmation = re.search(r'/confirm ([A-Za-z0-9_-]+)', responses[-1])[1]
    with db.SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Record)) == 0
        assert session.scalar(select(Proposal)).status == 'pending'
    client.post(tgurl, json=event(7, '/confirm ' + confirmation, sender=22), headers=tgh).raise_for_status()
    with db.SessionLocal() as session: assert session.scalar(select(func.count()).select_from(Record)) == 0
    client.post(tgurl, json=event(8, '/confirm ' + confirmation), headers=tgh).raise_for_status()
    client.post(tgurl, json=event(9, '/confirm ' + confirmation), headers=tgh).raise_for_status()
    with db.SessionLocal() as session: assert session.scalar(select(func.count()).select_from(Revision)) == 1
    vk = client.post(root + '/integrations', headers=headers, json={'name': 'VK', 'kind': 'vk', 'chat_ids': [10],
        'group_id': 1, 'bot_token': 'synthetic', 'confirmation_code': 'synthetic_confirmation'}).json()
    vkurl = '/api/incoming/' + vk['id'] + '/vk'
    assert client.post(vkurl, json={'type': 'confirmation', 'group_id': 2, 'secret': vk['incoming_key']}).status_code == 403
    assert client.post(vkurl, json={'type': 'confirmation', 'group_id': 1, 'secret': vk['incoming_key']}).text == 'synthetic_confirmation'
    payload = {'type': 'message_new', 'event_id': 'vk1', 'group_id': 1, 'secret': vk['incoming_key'],
        'object': {'message': {'id': 1, 'peer_id': 10, 'from_id': 21, 'date': 1794499200, 'text': 'VK original'}}}
    assert client.post(vkurl, json=payload).text == 'ok'
    assert client.post(vkurl, json=payload).text == 'ok'
    from apps.api.search import index_version
    with db.SessionLocal() as session:
        version = session.get(documents.DocumentVersion, first.json()['version_id'])
        version.text = 'sensitive-outbox-source'
        version.segments = [{'text': version.text, 'locator': 'fixture', 'quoted': False}]
        version.status = 'ready'
        index_version(session, version)
        session.commit()
    fail_delivery = True
    question_event = event(40, '/ask sensitive-outbox-source')
    assert client.post(tgurl, json=question_event, headers=tgh).status_code == 503
    with db.SessionLocal() as session:
        saved_reply = dict(session.get(integrations.Receipt, (telegram['id'], '40')).reply)
        assert 'sensitive-outbox-source' in saved_reply['text']
    doc_id = first.json()['id']
    client.post('/api/documents/' + doc_id + '/archive', headers=headers, json={'expected_version': 1, 'state': 'archived'}).raise_for_status()
    ticket = client.post('/api/documents/' + doc_id + '/purge-challenge', headers=headers, json={'expected_version': 2}).json()
    client.post('/api/documents/' + doc_id + '/purge', headers=headers,
        json={'expected_version': 2, 'token': ticket['token'], 'confirmation': 'УДАЛИТЬ'}).raise_for_status()
    before_delivery = len(responses)
    assert client.post(tgurl, json=question_event, headers=tgh).json()['duplicate']
    assert len(responses) == before_delivery
    with db.SessionLocal() as session:
        assert not session.get(integrations.Receipt, (telegram['id'], '40')).reply
    snapshot = integrations.authenticate(telegram['id'], telegram['incoming_key'], 'telegram')
    integrations.finish(snapshot, '40', 'done', reply=saved_reply, peer=10)
    with db.SessionLocal() as session:
        assert not session.get(integrations.Receipt, (telegram['id'], '40')).reply
    for url in ['http://127.0.0.1/private', 'https://attacker.invalid/file', 'https://userapi.com@127.0.0.1/file']:
        with pytest.raises(ValueError): integrations.download(url, tmp_path / 'forbidden')
