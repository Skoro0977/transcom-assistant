"""Offline, verified application backup. Restores only into a new directory."""
import argparse
import hashlib
import json
import os
import socket
import sqlite3
import stat
import subprocess
import tempfile
import zipfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
MAX_TOTAL = 20 * 1024 ** 3
MAX_FILES = 50000
MAX_MANIFEST_BYTES = 8 * 1024 * 1024


def on_d(path):
    path = Path(path).absolute()
    resolved = path.resolve()
    if resolved != path or (os.name == 'nt' and resolved.drive.upper() != 'D:'):
        raise ValueError('Путь должен находиться на D и не перенаправляться ссылкой')
    return resolved


def private(path):
    if os.name == 'nt':
        import getpass
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r',
            f'{getpass.getuser()}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'], capture_output=True, check=True)
    else:
        path.chmod(0o700)


def offline():
    with socket.socket() as connection:
        connection.settimeout(1)
        if connection.connect_ex(('127.0.0.1', 8000)) == 0:
            raise ValueError('Сначала остановите локальный сервер и отдельные worker процессы')


def regular(path, root):
    if (not path.resolve().is_relative_to(root) or path.is_symlink()
            or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400 or not path.is_file()):
        raise ValueError('Недопустимый файл в резервной копии')
    return path


def digest_file(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def database_check(path, blob_root=None):
    blob_root = Path(blob_root or path.parent / 'blobs').resolve()
    with closing(sqlite3.connect(path)) as connection:
        if connection.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or connection.execute('PRAGMA foreign_key_check').fetchone():
            raise ValueError('Нарушена целостность базы')
        count = connection.execute('SELECT count(*) FROM documents').fetchone()[0]
        for blob, checksum in connection.execute("SELECT blob_key,sha256 FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE d.lifecycle!='purged'"):
            if Path(blob).name != blob:
                raise ValueError('Недопустимый ключ оригинала')
            file = regular(blob_root / blob, blob_root)
            if digest_file(file) != checksum:
                raise ValueError('Контрольная сумма оригинала не совпадает с базой')
        return count


def create_backup(project=ROOT, output=None):
    offline()
    project = on_d(project)
    data = on_d(project / 'data')
    source_db = regular(data / 'transcom.db', data)
    output = on_d(output or data / 'local' / 'backups')
    if output.is_relative_to(data) and not output.is_relative_to(data / 'local' / 'backups'):
        raise ValueError('Внутри данных копии сохраняются только в data/local/backups')
    if output.exists() and not output.is_relative_to(data / 'local' / 'backups'):
        raise ValueError('Для внешней папки копий выберите новую папку на D')
    output.mkdir(parents=True, exist_ok=True)
    private(output)
    temporary_root = on_d(data / 'tmp')
    temporary_root.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')
    target = output / ('transcom-' + stamp + '.zip')
    partial = target.with_suffix('.partial')
    try:
        with tempfile.TemporaryDirectory(prefix='backup-', dir=temporary_root) as temporary:
            snapshot = Path(temporary) / 'transcom.db'
            # Hold all application writes while collecting DB and immutable blobs.
            with closing(sqlite3.connect(source_db, timeout=5)) as lock:
                lock.execute('BEGIN IMMEDIATE')
                with closing(sqlite3.connect(source_db)) as source, closing(sqlite3.connect(snapshot)) as copied:
                    source.backup(copied)
                database_check(snapshot, data / 'blobs')
                files = {'data/transcom.db': snapshot}
                blob_checksums = dict(lock.execute("SELECT blob_key,sha256 FROM document_versions v JOIN documents d ON d.id=v.document_id WHERE d.lifecycle!='purged'"))
                for blob in blob_checksums:
                    if Path(blob).name != blob:
                        raise ValueError('Недопустимый ключ оригинала')
                    path = data / 'blobs' / blob
                    if path.exists():
                        files['data/blobs/' + blob] = regular(path, (data / 'blobs').resolve())
                    else:
                        raise ValueError('Не найден оригинал действующего документа')
                for folder in ('source_archive', 'inbox'):
                    parent = data / folder
                    if parent.exists():
                        for path in parent.rglob('*'):
                            if path.is_file() or path.is_symlink() or getattr(path.lstat(), 'st_file_attributes', 0) & 0x400:
                                files['data/' + path.relative_to(data).as_posix()] = regular(path, data)
                for relative in ('local/accounts.csv', 'local/source-secrets/key'):
                    path = data / relative
                    if path.exists():
                        files['data/' + relative] = regular(path, data)
                secrets_path = data / 'local/source-secrets'
                if secrets_path.exists():
                    for path in secrets_path.glob('*.bin'):
                        files['data/' + path.relative_to(data).as_posix()] = regular(path, data)
                env = project / '.env'
                if env.exists():
                    files['.env'] = regular(env, project)
                if sum(path.stat().st_size for path in files.values()) > MAX_TOTAL or len(files) > MAX_FILES:
                    raise ValueError('Резервная копия превышает установленный предел')
                manifest = {'format': 1, 'created_at': datetime.now(UTC).isoformat(), 'original_data': str(data), 'files': {}}
                with zipfile.ZipFile(partial, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
                    for name, path in sorted(files.items()):
                        archive_name(name)
                        before = path.stat()
                        checksum = hashlib.sha256()
                        with path.open('rb') as source, archive.open(name, 'w', force_zip64=True) as copied:
                            while block := source.read(1024 * 1024):
                                checksum.update(block)
                                copied.write(block)
                        after = path.stat()
                        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
                            raise ValueError('Исходный файл изменился во время копирования')
                        if name.startswith('data/blobs/') and checksum.hexdigest() != blob_checksums[name.removeprefix('data/blobs/')]:
                            raise ValueError('Оригинал изменился относительно снимка базы')
                        manifest['files'][name] = {'size': before.st_size, 'sha256': checksum.hexdigest()}
                    manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode('utf-8')
                    if len(manifest_bytes) > MAX_MANIFEST_BYTES:
                        raise ValueError('Манифест слишком большой')
                    if len(manifest_bytes) + sum(item['size'] for item in manifest['files'].values()) > MAX_TOTAL:
                        raise ValueError('Резервная копия превышает установленный предел')
                    archive.writestr('manifest.json', manifest_bytes)
                lock.rollback()
        partial.replace(target)
        target.chmod(0o600)
        checksum = digest_file(target)
        target.with_suffix('.sha256').write_text(checksum + '\n', encoding='ascii')
        return {'path': str(target), 'sha256': checksum, 'files': len(manifest['files']), 'bytes': target.stat().st_size}
    except Exception:
        partial.unlink(missing_ok=True)
        raise


def archive_name(name):
    path = PurePosixPath(name)
    if (not name or '\\' in name or path.is_absolute() or any(p in {'..', '.'} or ':' in p for p in path.parts)
            or path.as_posix() != name):
        raise ValueError('Недопустимый путь в архиве')
    allowed = (name in {'.env', 'data/transcom.db', 'data/local/accounts.csv', 'data/local/source-secrets/key'}
               or name.startswith(('data/blobs/', 'data/source_archive/', 'data/inbox/'))
               or (name.startswith('data/local/source-secrets/') and name.endswith('.bin')))
    if not allowed:
        raise ValueError('Неизвестная область резервной копии')
    return path


def restore_backup(archive_path, destination, expected_sha256):
    archive_path, destination = on_d(archive_path), on_d(destination)
    if destination.exists():
        raise ValueError('Восстановление разрешено только в новую, еще не существующую папку')
    if not re_checksum(expected_sha256) or digest_file(archive_path) != expected_sha256:
        raise ValueError('Контрольная сумма архива не совпадает')
    with zipfile.ZipFile(archive_path) as archive:
        entries = archive.infolist()
        if len(entries) > MAX_FILES + 1 or sum(item.file_size for item in entries) > MAX_TOTAL:
            raise ValueError('Превышен объем резервной копии')
        if len({item.filename for item in entries}) != len(entries):
            raise ValueError('Повторяющиеся имена в архиве')
        manifest_entry = archive.getinfo('manifest.json')
        if manifest_entry.file_size > MAX_MANIFEST_BYTES:
            raise ValueError('Манифест слишком большой')
        manifest = json.loads(archive.read(manifest_entry))
        if manifest.get('format') != 1 or set(manifest.get('files', {})) != {e.filename for e in entries if e.filename != 'manifest.json'}:
            raise ValueError('Состав архива не совпадает с манифестом')
        for item in entries:
            if item.filename == 'manifest.json':
                continue
            archive_name(item.filename)
            if stat.S_ISLNK(item.external_attr >> 16) or item.file_size != manifest['files'][item.filename]['size']:
                raise ValueError('Недопустимая запись резервной копии')
        destination.mkdir(parents=True)
        private(destination)
        for name, expected in manifest['files'].items():
            path = destination.joinpath(*archive_name(name).parts)
            path.parent.mkdir(parents=True, exist_ok=True)
            checksum = hashlib.sha256()
            with archive.open(name) as source, path.open('xb') as target:
                while block := source.read(1024 * 1024):
                    checksum.update(block)
                    target.write(block)
            if checksum.hexdigest() != expected['sha256']:
                raise ValueError('Контрольная сумма файла не совпадает')
    database = destination / 'data/transcom.db'
    count = database_check(database)
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('DELETE FROM auth_sessions')
        connection.execute('DELETE FROM login_attempts')
        connection.execute('DELETE FROM bot_tickets')
        connection.execute("UPDATE integrations SET enabled=0,version=version+1")
        for key, config in connection.execute('SELECT id,config FROM sources').fetchall():
            config = json.loads(config)
            if config.get('path'):
                original = Path(manifest['original_data']) / 'inbox'
                relative = Path(config['path']).relative_to(original)
                config['path'] = str(destination / 'data/inbox' / relative)
            connection.execute('UPDATE sources SET enabled=0,version=version+1,config=? WHERE id=?', (json.dumps(config), key))
        connection.execute("UPDATE jobs SET status='queued',updated_at=? WHERE status='running'", (datetime.now(UTC).isoformat(),))
    env = destination / '.env'
    lines = env.read_text(encoding='utf-8-sig').splitlines() if env.exists() else []
    replace = {'DATABASE_URL': 'sqlite:///' + str(database).replace('\\', '/'), 'WORKER_ENABLED': 'false',
               'GIGACHAT_BULK_INDEXING_ENABLED': 'false'}
    lines = [line for line in lines if line.split('=', 1)[0].strip().upper() not in replace]
    lines.extend(key + '=' + json.dumps(value) for key, value in replace.items())
    env.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    database_check(database)
    return {'destination': str(destination), 'documents': count, 'connectors_disabled': True, 'worker_enabled': False}


def re_checksum(value):
    return len(value) == 64 and all(c in '0123456789abcdef' for c in value)


def main():
    parser = argparse.ArgumentParser(description='Проверенная копия и восстановление только на D')
    sub = parser.add_subparsers(dest='operation', required=True)
    create = sub.add_parser('create')
    create.add_argument('--output', type=Path)
    restore = sub.add_parser('restore')
    restore.add_argument('archive', type=Path)
    restore.add_argument('--destination', type=Path, required=True)
    restore.add_argument('--sha256', required=True)
    args = parser.parse_args()
    result = create_backup(output=args.output) if args.operation == 'create' else restore_backup(args.archive, args.destination, args.sha256)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
