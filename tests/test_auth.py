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
