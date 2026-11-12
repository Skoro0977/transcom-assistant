import hashlib
import mimetypes
import re
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from sqlalchemy import (
    JSON,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    select,
    update,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from .auth import Identity, identity, project_access
from .db import Base, session_db, uid, utcnow
from .settings import DATA

router = APIRouter(tags=['documents'])
BLOBS = DATA / 'blobs'
BLOBS.mkdir(exist_ok=True)
MAX_BYTES = 100 * 1024 * 1024
EXTENSIONS = {'.pdf', '.docx', '.doc', '.xlsx', '.xls', '.msg', '.eml', '.txt', '.csv',
              '.rtf', '.png', '.jpg', '.jpeg', '.dxf', '.kml', '.log'}


class Document(Base):
    __tablename__ = 'documents'
    __table_args__ = (UniqueConstraint('id', 'project_id'),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(ForeignKey('projects.id'), index=True)
    title: Mapped[str] = mapped_column(String(240))
    category: Mapped[str] = mapped_column(String(200), default='Не определено')
    lifecycle: Mapped[str] = mapped_column(String(16), default='active')
    current_number: Mapped[int] = mapped_column(default=1)
    version: Mapped[int] = mapped_column(default=1)
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class DocumentVersion(Base):
    __tablename__ = 'document_versions'
    __table_args__ = (UniqueConstraint('document_id', 'number'), UniqueConstraint('id', 'project_id'),
                     ForeignKeyConstraint(['document_id', 'project_id'], ['documents.id', 'documents.project_id']))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    document_id: Mapped[str] = mapped_column(String(32), index=True)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    number: Mapped[int] = mapped_column()
    filename: Mapped[str] = mapped_column(String(240))
    blob_key: Mapped[str] = mapped_column(String(100))
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    size: Mapped[int] = mapped_column()
    status: Mapped[str] = mapped_column(String(32), default='queued')
    text: Mapped[str] = mapped_column(Text, default='')
    segments: Mapped[list] = mapped_column(JSON, default=list)
    metadata_json: Mapped[dict] = mapped_column(JSON, default=dict)
    warning: Mapped[str] = mapped_column(Text, default='')
    created_at: Mapped[str] = mapped_column(String(40), default=utcnow)
    created_by: Mapped[str] = mapped_column(ForeignKey('users.id'))


class Occurrence(Base):
    __tablename__ = 'document_occurrences'
    __table_args__ = (UniqueConstraint('project_id', 'channel', 'external_id'),
                     ForeignKeyConstraint(['version_id', 'project_id'],
                                          ['document_versions.id', 'document_versions.project_id']))
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    version_id: Mapped[str] = mapped_column(String(32), index=True)
    channel: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(500))
    source_name: Mapped[str] = mapped_column(String(500))
    received_at: Mapped[str] = mapped_column(String(40), default=utcnow)


class Job(Base):
    __tablename__ = 'jobs'
    __table_args__ = (ForeignKeyConstraint(['version_id', 'project_id'],
                                          ['document_versions.id', 'document_versions.project_id']),)
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=uid)
    project_id: Mapped[str] = mapped_column(String(32), index=True)
    version_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(32), default='parse')
    status: Mapped[str] = mapped_column(String(24), default='queued', index=True)
    attempts: Mapped[int] = mapped_column(default=0)
    error: Mapped[str] = mapped_column(Text, default='')
    updated_at: Mapped[str] = mapped_column(String(40), default=utcnow)


def filename_only(value: str):
    name = value.replace('\\', '/').rsplit('/', 1)[-1]
    name = re.sub(r'[\x00-\x1f\x7f]', '', name).strip(' .')[:240]
    if not name or Path(name).suffix.lower() not in EXTENSIONS:
        raise HTTPException(422, 'Формат файла не поддерживается для загрузки')
    return name


def blob_path(key: str):
    path = (BLOBS / key).resolve()
    if not path.is_relative_to(BLOBS.resolve()):
        raise HTTPException(404, 'Файл недоступен')
    return path


def ingest(db: Session, project_id: str, user_id: str, temporary: Path, filename: str,
           channel='upload', external_id=None, source_name=None, document_id=None, expected_version=None):
    filename = filename_only(filename)
    if temporary.stat().st_size > MAX_BYTES:
        raise HTTPException(413, 'Файл превышает ограничение 100 МиБ')
    with temporary.open('rb') as stream:
        checksum = hashlib.file_digest(stream, 'sha256').hexdigest()
    if db.get_bind().dialect.name == 'sqlite':
        db.connection().exec_driver_sql('BEGIN IMMEDIATE')
    document = db.get(Document, document_id) if document_id else None
    if document_id and (not document or document.project_id != project_id or document.lifecycle != 'active'):
        raise HTTPException(404, 'Активный документ не найден')
    if document and document.version != expected_version:
        raise HTTPException(409, 'Документ уже изменился, обновите его')
    duplicate_query = select(DocumentVersion).join(Document, Document.id == DocumentVersion.document_id).where(
        DocumentVersion.project_id == project_id, DocumentVersion.sha256 == checksum, Document.lifecycle != 'purged')
    if document_id:
        duplicate_query = duplicate_query.where(DocumentVersion.document_id == document_id,
                                                 DocumentVersion.number == document.current_number)
    existing = db.scalar(duplicate_query.limit(1))
    created_path = None
    try:
        if existing:
            version = existing
            document = db.get(Document, version.document_id)
            if document.lifecycle == 'purged':
                raise HTTPException(409, 'Файл относится к удаленному документу, загрузите как новое основание')
        else:
            if document is None:
                document = Document(project_id=project_id, title=filename,
                                    category=(source_name or '').split('/')[0] if channel == 'folder' else 'Не определено')
                db.add(document)
                db.flush()
                number = 1
            else:
                number = document.current_number + 1
                result = db.execute(update(Document).where(Document.id == document.id,
                                    Document.version == expected_version).values(current_number=number,
                                                                               version=expected_version + 1))
                if result.rowcount != 1:
                    raise HTTPException(409, 'Документ уже изменился')
            version_id = uid()
            key = version_id + Path(filename).suffix.lower()
            created_path = blob_path(key)
            temporary.replace(created_path)
            version = DocumentVersion(id=version_id, document_id=document.id, project_id=project_id,
                                      number=number, filename=filename, blob_key=key, sha256=checksum,
                                      size=created_path.stat().st_size, created_by=user_id)
            db.add(version)
            db.flush()
            db.add(Job(project_id=project_id, version_id=version.id))
        origin_id = external_id or uid()
        occurrence = db.scalar(select(Occurrence).where(Occurrence.project_id == project_id,
                              Occurrence.channel == channel, Occurrence.external_id == origin_id))
        if occurrence is None:
            db.add(Occurrence(project_id=project_id, version_id=version.id, channel=channel,
                              external_id=origin_id, source_name=source_name or filename))
        elif occurrence.version_id != version.id:
            raise HTTPException(409, 'Идентификатор источника уже связан с другим содержимым')
        db.commit()
        return {'id': document.id, 'version_id': version.id, 'number': version.number,
                'duplicate': existing is not None, 'status': version.status, 'lifecycle': document.lifecycle}
    except Exception:
        db.rollback()
        if created_path and created_path.exists():
            created_path.unlink()
        raise


def accessible_document(document_id: str, db: Session, who: Identity):
    document = db.get(Document, document_id)
    if document is None or document.lifecycle == 'purged':
        raise HTTPException(404, 'Документ не найден')
    project_access(document.project_id, db, who)
    return document


@router.post('/api/projects/{project_id}/documents', status_code=201)
async def upload(project_id: str, file: UploadFile, document_id: str | None = None,
                 expected_version: int | None = None, who: Identity = Depends(identity),
                 db: Session = Depends(session_db)):
    project_access(project_id, db, who, 'edit')
    name = filename_only(file.filename or '')
    temporary = DATA / 'tmp' / (uid() + '.upload')
    try:
        total = 0
        with temporary.open('xb') as stream:
            while block := await file.read(1024 * 1024):
                total += len(block)
                if total > MAX_BYTES:
                    raise HTTPException(413, 'Файл превышает ограничение 100 МиБ')
                stream.write(block)
        return ingest(db, project_id, who.user.id, temporary, name,
                      document_id=document_id, expected_version=expected_version)
    finally:
        await file.close()
        temporary.unlink(missing_ok=True)


@router.get('/api/projects/{project_id}/documents')
def documents(project_id: str, lifecycle: str = 'active', limit: int = Query(default=100, ge=1, le=200),
              offset: int = Query(default=0, ge=0), who: Identity = Depends(identity),
              db: Session = Depends(session_db)):
    project_access(project_id, db, who)
    query = select(Document).where(Document.project_id == project_id, Document.lifecycle == lifecycle)
    total = db.scalar(select(func.count()).select_from(query.subquery()))
    items = []
    for document in db.scalars(query.order_by(Document.created_at.desc()).limit(limit).offset(offset)):
        version = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == document.id,
                                                          DocumentVersion.number == document.current_number))
        items.append({'id': document.id, 'title': document.title, 'category': document.category,
                      'lifecycle': document.lifecycle, 'version': document.version,
                      'number': document.current_number, 'status': version.status, 'warning': version.warning,
                      'size': version.size, 'created_at': document.created_at})
    return {'items': items, 'total': total}


@router.get('/api/documents/{document_id}')
def detail(document_id: str, who: Identity = Depends(identity), db: Session = Depends(session_db)):
    document = accessible_document(document_id, db, who)
    versions = db.scalars(select(DocumentVersion).where(DocumentVersion.document_id == document_id)
                         .order_by(DocumentVersion.number.desc())).all()
    from .worker import AttachmentLink
    links = db.execute(select(AttachmentLink.parent_version_id, AttachmentLink.child_version_id)
                       .join(DocumentVersion, DocumentVersion.id == AttachmentLink.parent_version_id)
                       .where(DocumentVersion.project_id == document.project_id,
                              AttachmentLink.parent_version_id.in_([v.id for v in versions]))).all()
    attached = []
    for parent, child_id in links:
        child = db.get(DocumentVersion, child_id)
        child_doc = db.get(Document, child.document_id)
        if child_doc.lifecycle != 'purged' and child.project_id == document.project_id:
            attached.append({'parent_version_id': parent, 'document_id': child.document_id,
                             'version_id': child.id, 'filename': child.filename})
    return {'id': document.id, 'title': document.title, 'version': document.version, 'attachments': attached,
            'origins': [{'channel': o.channel, 'source_name': o.source_name, 'received_at': o.received_at,
                         'version_id': o.version_id} for o in db.scalars(select(Occurrence).where(
                             Occurrence.version_id.in_([v.id for v in versions])))],
            'versions': [{'id': v.id, 'number': v.number, 'filename': v.filename, 'status': v.status,
                          'warning': v.warning, 'text': v.text[:200000], 'metadata': v.metadata_json,
                          'created_at': v.created_at} for v in versions]}


@router.get('/api/documents/{document_id}/content')
def content(document_id: str, number: int | None = None, who: Identity = Depends(identity),
            db: Session = Depends(session_db)):
    document = accessible_document(document_id, db, who)
    version = db.scalar(select(DocumentVersion).where(DocumentVersion.document_id == document_id,
                        DocumentVersion.number == (number or document.current_number)))
    if not version or not blob_path(version.blob_key).is_file():
        raise HTTPException(404, 'Файл не найден')
    return FileResponse(blob_path(version.blob_key), filename=version.filename,
                        media_type=mimetypes.guess_type(version.filename)[0] or 'application/octet-stream')
