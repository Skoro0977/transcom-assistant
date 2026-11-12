import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from apps.api import db, documents
from apps.api.main import app
from apps.api.manage import seed
from apps.api.settings import validate_database_location


@pytest.fixture
def isolated(tmp_path, monkeypatch):
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
    client.post('/api/auth/login', json={k: accounts[-1][k] for k in ('username', 'password')})
    assert client.get('/api/documents/' + first['id'] + '/content').status_code == 404


def test_chunked_body_is_limited_before_parsing():
    with TestClient(app, base_url='http://127.0.0.1:8000') as client:
        chunks = iter([b'{"username":"', b'x' * (1024 * 1024), b'","password":"x"}'])
        result = client.post('/api/auth/login', content=chunks, headers={'Content-Type': 'application/json'})
        assert result.status_code == 413
