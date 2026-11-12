import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import UTC, datetime, timedelta
from logging.handlers import RotatingFileHandler
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import ForeignKey, String, UniqueConstraint, select, update
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Mapped, mapped_column

from . import db
from .documents import Document, DocumentVersion, Job, blob_path, ingest
from .settings import DATA, ROOT

logger = logging.getLogger('transcom.worker')
if not logger.handlers:
    (DATA / 'local').mkdir(exist_ok=True)
    handler = RotatingFileHandler(DATA / 'local' / 'worker.log', maxBytes=1024 * 1024, backupCount=2,
                                  encoding='utf-8')
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    logger.propagate = False

class AttachmentLink(db.Base):
    __tablename__ = 'attachment_links'
    __table_args__ = (UniqueConstraint('parent_version_id', 'child_version_id'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=db.uid)
    parent_version_id: Mapped[str] = mapped_column(ForeignKey('document_versions.id'), index=True)
    child_version_id: Mapped[str] = mapped_column(ForeignKey('document_versions.id'), index=True)


def claim():
    with db.SessionLocal() as session:
        stale = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
        session.execute(update(Job).where(Job.status == 'running', Job.updated_at < stale,
                                         Job.attempts < 3).values(status='queued'))
        session.execute(update(Job).where(Job.status == 'running', Job.updated_at < stale,
                                         Job.attempts >= 3).values(status='failed', error='Превышено число попыток'))
        job = session.scalar(select(Job).where(Job.status == 'queued', Job.updated_at <= db.utcnow(),
                                              Job.kind.in_(['parse', 'analyze', 'purge']))
                             .order_by(Job.kind != 'purge', Job.updated_at).limit(1))
        if job is None:
            session.commit()
            return None
        changed = session.execute(update(Job).where(Job.id == job.id, Job.status == 'queued')
                                  .values(status='running', attempts=Job.attempts + 1, updated_at=db.utcnow()))
        session.commit()
        return job.id if changed.rowcount else None


def process(job_id):
    with db.SessionLocal() as session:
        job = session.get(Job, job_id)
        dispatch_stamp = job.updated_at
        if job.kind == 'purge':
            from .lifecycle import cleanup_blob
            try:
                cleanup_blob(job_id)
            except OSError:
                db.write_lock(session)
                session.refresh(job)
                if job.updated_at != dispatch_stamp:
                    return
                job.status = 'queued' if job.attempts < 6 else 'failed'
                job.error = 'Не удалось физически удалить файл. Закройте открытый оригинал и повторите очистку'
                job.updated_at = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
                session.commit()
            return
        if job.kind == 'analyze':
            from .analyzer import analyze_job
            from .gigachat import ProviderError
            try:
                analyze_job(job_id)
            except Exception as error:
                logger.exception('Analysis job %s failed', job_id)
                db.write_lock(session)
                session.refresh(job)
                if job.status not in {'queued', 'running'} or job.updated_at != dispatch_stamp:
                    return
                job.status = 'failed'
                job.error = str(error) if isinstance(error, ProviderError) else 'Не удалось проанализировать документ'
                job.updated_at = db.utcnow()
                session.commit()
            return
    temporary = Path(tempfile.mkdtemp(prefix='parse-', dir=DATA / 'tmp'))
    job_stamp = None
    if not temporary.resolve().is_relative_to((DATA / 'tmp').resolve()):
        raise RuntimeError('Unsafe parser workspace')
    try:
        with db.SessionLocal() as session:
            db.write_lock(session)
            job = session.get(Job, job_id)
            if job.status not in {'queued', 'running'}:
                return
            job_stamp = job.updated_at
            version = session.get(DocumentVersion, job.version_id)
            document = session.get(Document, version.document_id)
            if document.lifecycle != 'active':
                job.status = 'cancelled'
                session.commit()
                return
            version.status = 'parsing'
            project_id, creator, version_id = version.project_id, version.created_by, version.id
            source = blob_path(version.blob_key)
            session.commit()
        environment = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', TEMP=str(DATA / 'tmp'), TMP=str(DATA / 'tmp'))
        with (temporary / 'parser.log').open('wb') as log:
            outcome = subprocess.run([sys.executable, '-B', '-m', 'apps.api.parse_file', str(source), str(temporary)],
                                     cwd=ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=log,
                                     timeout=90, check=False)
        if outcome.returncode != 0:
            raise ValueError('Parser rejected file')
        result_path = temporary / 'result.json'
        if result_path.stat().st_size > 16 * 1024 * 1024:
            raise ValueError('Parser result limit')
        result = json.loads(result_path.read_text(encoding='utf-8'))
        with db.SessionLocal() as session:
            db.write_lock(session)
            job = session.get(Job, job_id)
            if job.status not in {'queued', 'running'} or job.updated_at != job_stamp:
                return
            version = session.get(DocumentVersion, version_id)
            if session.get(Document, version.document_id).lifecycle != 'active':
                session.get(Job, job_id).status = 'cancelled'
                session.commit()
                return
            version.segments = result['segments']
            version.text = '\n\n'.join(s['text'] for s in result['segments'])
            version.metadata_json = result['metadata']
            version.warning = result['warning']
            version.status = result['status']
            if version.number > 1 and version.text:
                analysis = session.scalar(select(Job).where(Job.version_id == version.id, Job.kind == 'analyze'))
                if analysis is None:
                    session.add(Job(project_id=version.project_id, version_id=version.id, kind='analyze'))
            session.commit()
        for index, item in enumerate(result['attachments']):
            raw_path = Path(item['path'])
            path = raw_path.resolve()
            if not path.is_relative_to(temporary.resolve()) or raw_path.is_symlink():
                raise ValueError('Unsafe attachment path')
            try:
                with db.SessionLocal() as session:
                    db.write_lock(session)
                    parent_job = session.get(Job, job_id)
                    parent_doc = session.get(Document, session.get(DocumentVersion, version_id).document_id)
                    if (parent_doc.lifecycle != 'active' or parent_job.status not in {'queued', 'running'}
                            or parent_job.updated_at != job_stamp):
                        return
                    child = ingest(session, project_id, creator, path, item['name'], channel='attachment',
                                   external_id=f'{version_id}:{index}', source_name=item['name'], parent_version_id=version_id)
                    if child.get('suppressed'):
                        continue
            except (HTTPException, OSError, ValueError, SQLAlchemyError):
                logger.exception('Attachment rejected for version %s', version_id)
                with db.SessionLocal() as session:
                    db.write_lock(session)
                    current_job = session.get(Job, job_id)
                    version = session.get(DocumentVersion, version_id)
                    if (session.get(Document, version.document_id).lifecycle != 'active'
                            or current_job.status not in {'queued', 'running'} or current_job.updated_at != job_stamp):
                        return
                    version.warning = (version.warning + '; Вложение не принято: неподдержанный формат или конфликт')[:4000]
                    version.status = 'needs_preparation'
                    session.commit()
        with db.SessionLocal() as session:
            db.write_lock(session)
            job = session.get(Job, job_id)
            if job.status not in {'queued', 'running'} or job.updated_at != job_stamp:
                return
            job.status, job.updated_at = 'done', db.utcnow()
            session.commit()
    except Exception:
        logger.exception('Parser job %s failed', job_id)
        parser_log = temporary / 'parser.log'
        if parser_log.exists():
            logger.warning('Parser diagnostics: %s', parser_log.read_text(encoding='utf-8', errors='replace')[-8000:])
        with db.SessionLocal() as session:
            db.write_lock(session)
            job = session.get(Job, job_id)
            if job.status not in {'queued', 'running'} or job_stamp is None or job.updated_at != job_stamp:
                return
            version = session.get(DocumentVersion, job.version_id)
            if session.get(Document, version.document_id).lifecycle != 'active':
                return
            job.status, job.error = 'failed', 'Не удалось безопасно обработать файл; доступен оригинал'
            job.updated_at = db.utcnow()
            version.status, version.warning = 'needs_preparation', job.error
            session.commit()
    finally:
        if temporary.resolve().is_relative_to((DATA / 'tmp').resolve()):
            shutil.rmtree(temporary)


def drain():
    count = 0
    while job_id := claim():
        process(job_id)
        count += 1
        if count % 25 == 0:
            print(f'Processed jobs: {count}', flush=True)
    print(f'Queue complete: {count}', flush=True)


def start():
    stop = threading.Event()

    def loop():
        while not stop.is_set():
            try:
                job_id = claim()
                if job_id:
                    process(job_id)
                else:
                    stop.wait(1)
            except Exception:
                logger.exception('Worker iteration failed')
                stop.wait(3)

    thread = threading.Thread(target=loop, name='transcom-worker', daemon=True)
    thread.start()
    return stop, thread


if __name__ == '__main__':
    drain()
