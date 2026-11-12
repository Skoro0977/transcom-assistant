import hashlib
import json
import sqlite3
from contextlib import closing

import pytest
from cryptography.fernet import Fernet

from scripts import backup


def test_backup_restore_preserves_originals_secrets_and_disables_connectors(tmp_path, monkeypatch):
    project = tmp_path / 'source'
    data = project / 'data'
    (data / 'blobs').mkdir(parents=True)
    (data / 'inbox' / 'pilot').mkdir(parents=True)
    local = data / 'local/source-secrets'
    local.mkdir(parents=True)
    original = b'Backup original'
    blob = 'a' * 32 + '.txt'
    (data / 'blobs' / blob).write_bytes(original)
    key = Fernet.generate_key()
    (local / 'key').write_bytes(key)
    (local / ('b' * 32 + '.bin')).write_bytes(Fernet(key).encrypt(b'synthetic source credentials'))
    (project / '.env').write_text('GIGACHAT_AUTH_KEY=synthetic\nWORKER_ENABLED=true\nGIGACHAT_BULK_INDEXING_ENABLED=true\n', encoding='utf-8')
    with closing(sqlite3.connect(data / 'transcom.db')) as connection, connection:
        connection.executescript('''CREATE TABLE documents(id TEXT,lifecycle TEXT);
CREATE TABLE document_versions(document_id TEXT,blob_key TEXT,sha256 TEXT);
CREATE TABLE auth_sessions(id TEXT); CREATE TABLE login_attempts(id TEXT); CREATE TABLE bot_tickets(id TEXT);
CREATE TABLE integrations(enabled INTEGER,version INTEGER);
CREATE TABLE sources(id TEXT,config TEXT,enabled INTEGER,version INTEGER);
CREATE TABLE jobs(status TEXT,updated_at TEXT);''')
        connection.execute("INSERT INTO documents VALUES('doc','active')")
        connection.execute('INSERT INTO document_versions VALUES(?,?,?)', ('doc', blob, hashlib.sha256(original).hexdigest()))
        connection.execute("INSERT INTO auth_sessions VALUES('old-session')")
        connection.execute('INSERT INTO integrations VALUES(1,1)')
        connection.execute('INSERT INTO sources VALUES(?,?,1,1)', ('source', json.dumps({'path': str(data / 'inbox/pilot')})))
        connection.execute("INSERT INTO jobs VALUES('running','old')")
    monkeypatch.setattr(backup, 'offline', lambda: None)  # Synthetic DB only; no application process uses it.
    result = backup.create_backup(project)
    target = tmp_path / 'restored'
    restored = backup.restore_backup(result['path'], target, result['sha256'])
    assert restored['documents'] == 1 and (target / 'data/blobs' / blob).read_bytes() == original
    assert Fernet((target / 'data/local/source-secrets/key').read_bytes()).decrypt(
        (target / 'data/local/source-secrets' / ('b' * 32 + '.bin')).read_bytes()) == b'synthetic source credentials'
    with closing(sqlite3.connect(target / 'data/transcom.db')) as connection:
        assert connection.execute('SELECT count(*) FROM auth_sessions').fetchone()[0] == 0
        assert connection.execute('SELECT enabled FROM integrations').fetchone()[0] == 0
        source = connection.execute('SELECT config,enabled FROM sources').fetchone()
        assert source[1] == 0 and json.loads(source[0])['path'] == str(target / 'data/inbox/pilot')
        assert connection.execute('SELECT status FROM jobs').fetchone()[0] == 'queued'
    env = (target / '.env').read_text(encoding='utf-8')
    assert 'GIGACHAT_AUTH_KEY=synthetic' in env and 'GIGACHAT_BULK_INDEXING_ENABLED="false"' in env
    with pytest.raises(ValueError):
        backup.restore_backup(result['path'], target, result['sha256'])
    with pytest.raises(ValueError):
        backup.restore_backup(result['path'], tmp_path / 'invalid', '0' * 64)
    for name in ['../outside', 'data/blobs/../../outside', 'C:/outside', 'data/blobs/name:stream']:
        with pytest.raises(ValueError):
            backup.archive_name(name)
    published = set((data / 'local/backups').glob('*.zip'))
    with monkeypatch.context() as limits:
        limits.setattr(backup, 'MAX_MANIFEST_BYTES', 1)
        with pytest.raises(ValueError, match='Манифест слишком большой'):
            backup.create_backup(project)
    assert set((data / 'local/backups').glob('*.zip')) == published
    assert not list((data / 'local/backups').glob('*.partial'))
    (data / 'blobs' / blob).write_bytes(b'Damaged original')
    with pytest.raises(ValueError, match='Контрольная сумма оригинала'):
        backup.create_backup(project)
