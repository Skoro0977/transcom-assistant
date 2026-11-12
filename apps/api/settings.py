import os
import tempfile
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[2]
if os.name == 'nt' and ROOT.drive.upper() != 'D:':
    raise RuntimeError('Project must be located on D')
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
(DATA / 'tmp').mkdir(exist_ok=True)
tempfile.tempdir = str(DATA / 'tmp')


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / '.env', extra='ignore')
    database_url: str = 'sqlite:///' + str(DATA / 'transcom.db').replace('\\', '/')
    session_hours: int = Field(default=12, ge=1, le=168)
    cookie_secure: bool = False
    allowed_origins: str = 'http://127.0.0.1:8000,http://localhost:8000,http://127.0.0.1:5173,http://localhost:5173'


settings = Settings()


def validate_database_location(value: str):
    url = make_url(value)
    if os.name == 'nt' and url.get_backend_name() == 'sqlite' and url.database != ':memory:':
        path = Path(url.database or '')
        if (not path.is_absolute() or path.resolve().drive.upper() != 'D:'
                or url.query.get('uri') or str(path).startswith('file:')):
            raise RuntimeError('SQLite data must be stored on D, file URI is not supported')
    return url


DATABASE_URL = validate_database_location(settings.database_url)
IS_SQLITE = DATABASE_URL.get_backend_name() == 'sqlite'
