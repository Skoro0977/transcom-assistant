import os

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from apps.api import db
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
