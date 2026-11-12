"""Local encrypted source credentials, excluded from Git and API responses."""
import json
import os

from cryptography.fernet import Fernet

from . import db
from .settings import DATA

VAULT = DATA / 'local' / 'source-secrets'


def cipher():
    VAULT.mkdir(parents=True, exist_ok=True)
    if os.name != 'nt':
        VAULT.chmod(0o700)
    key = VAULT / 'key'
    try:
        with key.open('xb') as stream:
            stream.write(Fernet.generate_key())
            stream.flush()
            os.fsync(stream.fileno())
        key.chmod(0o600)
    except FileExistsError:
        pass
    return Fernet(key.read_bytes())


def secret_path(reference):
    if len(reference) != 32 or any(c not in '0123456789abcdef' for c in reference):
        raise ValueError('Invalid secret reference')
    return VAULT / (reference + '.bin')


def put(value):
    reference = db.uid()
    path = secret_path(reference)
    data = cipher().encrypt(json.dumps(value).encode())
    with path.open('xb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    path.chmod(0o600)
    return reference


def get(reference):
    if not reference:
        return {}
    return json.loads(cipher().decrypt(secret_path(reference).read_bytes()))


def discard(reference):
    if reference:
        secret_path(reference).unlink(missing_ok=True)
