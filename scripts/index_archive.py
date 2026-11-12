import argparse

from sqlalchemy import func, select

from apps.api import db
from apps.api.documents import Document, DocumentVersion
from apps.api.knowledge import Revision
from apps.api.search import Chunk, embed_next, index_revision, index_version


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--embeddings', action='store_true')
    arguments = parser.parse_args()
    with db.SessionLocal() as session:
        ids = list(session.scalars(select(DocumentVersion.id).join(Document).where(Document.lifecycle != 'purged')))
    for key in ids:
        with db.SessionLocal() as session:
            db.write_lock(session)
            version = session.get(DocumentVersion, key)
            if session.get(Document, version.document_id).lifecycle != 'purged':
                index_version(session, version)
                session.commit()
    with db.SessionLocal() as session:
        db.write_lock(session)
        for revision in session.scalars(select(Revision)):
            index_revision(session, revision)
        session.commit()
        print('Stored chunks:', session.scalar(select(func.count()).select_from(Chunk)), flush=True)
    count = 0
    while arguments.embeddings and embed_next():
        count += 1
        if count % 20 == 0:
            print('Embedding batches:', count, flush=True)
    with db.SessionLocal() as session:
        print('Index states:', session.execute(select(Chunk.state, func.count()).group_by(Chunk.state)).all())


if __name__ == '__main__':
    main()
