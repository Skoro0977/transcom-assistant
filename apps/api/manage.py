import csv
import getpass
import os
import secrets
import subprocess
from pathlib import Path

from sqlalchemy import select

from . import db
from .auth import passwords
from .settings import DATA, ROOT


def protect_directory(path: Path):
    if os.name == 'nt':
        subprocess.run(['icacls', str(path), '/inheritance:r', '/grant:r',
                        f'{getpass.getuser()}:(OI)(CI)F', '*S-1-5-18:(OI)(CI)F'],
                       capture_output=True, check=True)
    else:
        path.chmod(0o700)


def seed(session, save_credentials=True):
    if session.scalar(select(db.User.id).limit(1)):
        return []
    project = db.Project(title='Газопровод Ренево', description='Пилотный архив проектирования')
    session.add(project)
    session.flush()
    accounts = []
    target = DATA / 'local' / 'accounts.csv'
    known = {}
    if save_credentials and target.exists():
        with target.open(encoding='utf-8-sig', newline='') as stream:
            known = {item['username']: item['password'] for item in csv.DictReader(stream)}
        expected = {f'admin{n:02}' for n in range(1, 11)} | {f'user{n:02}' for n in range(1, 6)}
        if set(known) != expected or not all(known.values()):
            raise RuntimeError('Existing bootstrap credentials are incomplete; restore the local credential file')
    for role, count in [('admin', 10), ('user', 5)]:
        for number in range(1, count + 1):
            name = f'{role}{number:02}'
            password = known.get(name) or secrets.token_urlsafe(18)
            user = db.User(username=name, password_hash=passwords.hash(password), role=role)
            session.add(user)
            session.flush()
            session.add(db.Membership(user_id=user.id, project_id=project.id, can_edit=True, can_review=True))
            accounts.append({'username': name, 'password': password, 'role': role})
    if save_credentials:
        private = DATA / 'local'
        private.mkdir(exist_ok=True)
        protect_directory(private)
        if not target.exists():
            pending = private / 'accounts.pending.csv'
            with pending.open('w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=['username', 'password', 'role'])
                writer.writeheader()
                writer.writerows(accounts)
                stream.flush()
                os.fsync(stream.fileno())
            pending.replace(target)
        target.chmod(0o600)
    session.commit()
    return accounts


def main():
    from alembic import command
    from alembic.config import Config
    protect_directory(DATA)
    command.upgrade(Config(str(ROOT / 'alembic.ini')), 'head')
    with db.SessionLocal() as session:
        seed(session)
    print('Database ready. Local credentials are in data/local/accounts.csv')


if __name__ == '__main__':
    main()
