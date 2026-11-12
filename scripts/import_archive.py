import hashlib
import tempfile
from pathlib import Path

from sqlalchemy import select

from apps.api.db import Project, SessionLocal, User
from apps.api.documents import ingest
from apps.api.settings import DATA


def main():
    source = (DATA / 'source_archive').resolve()
    count = 0
    for path in sorted(source.rglob('*')):
        if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(source):
            continue
        with path.open('rb') as stream:
            checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
        with tempfile.NamedTemporaryFile(dir=DATA / 'tmp', suffix=path.suffix, delete=False) as out:
            temporary = Path(out.name)
            with path.open('rb') as stream:
                while block := stream.read(1024 * 1024):
                    out.write(block)
        try:
            with SessionLocal() as session:
                project = session.scalar(select(Project).order_by(Project.created_at).limit(1))
                user = session.scalar(select(User).where(User.username == 'admin01'))
                relative = path.relative_to(source).as_posix()
                ingest(session, project.id, user.id, temporary, path.name, channel='folder',
                       external_id=checksum + ':' + relative, source_name=relative)
            count += 1
        finally:
            temporary.unlink(missing_ok=True)
    print(f'Imported source files: {count}')


if __name__ == '__main__':
    main()
