from alembic import context

from apps.api import (
    documents,  # noqa: F401
    knowledge,  # noqa: F401
    lifecycle,  # noqa: F401
    risks,  # noqa: F401
    search,  # noqa: F401
    worker,  # noqa: F401
)
from apps.api.db import Base, engine

with engine.connect() as connection:
    context.configure(connection=connection, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
