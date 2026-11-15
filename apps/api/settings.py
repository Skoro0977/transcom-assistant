import os
import shutil
import tempfile
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

from .paths import PORTABLE_MODE, ROOT, is_storage_path

DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
(DATA / 'tmp').mkdir(exist_ok=True)
tempfile.tempdir = str(DATA / 'tmp')


def default_ocr_node():
    local = ROOT / 'data/local/runtime/node-v26.13.1-win-x64/node.exe'
    if local.is_file():
        return str(local)
    if os.name == 'nt' and not PORTABLE_MODE:
        return 'D:/CodexData/Runtimes/node/node-v26.13.1-win-x64/node.exe'
    return shutil.which('node') or 'node'


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    database_url: str = 'sqlite:///' + str(DATA / 'transcom.db').replace('\\', '/')
    session_hours: int = Field(default=12, ge=1, le=168)
    cookie_secure: bool = False
    worker_enabled: bool = True
    ocr_node: str = Field(default_factory=lambda: default_ocr_node())
    gigachat_auth_key: SecretStr = SecretStr('')
    gigachat_scope: Literal['GIGACHAT_API_PERS', 'GIGACHAT_API_B2B', 'GIGACHAT_API_CORP'] = 'GIGACHAT_API_PERS'
    gigachat_model: str = 'GigaChat-3-Lightning'
    gigachat_embedding_model: str = 'EmbeddingsGigaR'
    gigachat_ca_file: str = ''
    gigachat_bulk_indexing_enabled: bool = False
    allowed_origins: str = 'http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:5173,http://localhost:5173'


settings = Settings()
if os.environ.get('TRANSCOM_LOCAL_PORT'):
    port = int(os.environ['TRANSCOM_LOCAL_PORT'])
    if not 1024 <= port <= 65535:
        raise RuntimeError('Invalid local server port')
    origin = f'http://127.0.0.1:{port}'
    origins = settings.allowed_origins.split(',')
    if origin not in origins:
        settings.allowed_origins = ','.join([*origins, origin])


def validate_database_location(value: str):
    url = make_url(value)
    if os.name == 'nt' and url.get_backend_name() == 'sqlite' and url.database != ':memory:':
        path = Path(url.database or '')
        if (not path.is_absolute() or not is_storage_path(path.resolve())
                or 'uri' in url.query or str(url.database).lower().startswith('file:')):
            raise RuntimeError('SQLite data must stay on the configured local drive; file URI is not supported')
    return url


DATABASE_URL = validate_database_location(settings.database_url)
IS_SQLITE = DATABASE_URL.get_backend_name() == 'sqlite'
