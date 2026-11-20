"""Database engine/session handling. Run `python -m app.db` to create the tables."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from pathlib import Path

from sqlalchemy import Engine, create_engine, make_url
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


SessionLocal = sessionmaker(expire_on_commit=False)
_engine: Engine | None = None


def configure_engine(url: str) -> Engine:
    """(Re)create the engine. Called lazily with settings.database_url; tests call it with their own DB."""
    global _engine
    if _engine is not None:
        _engine.dispose()

    options: dict = {"pool_pre_ping": True}
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        options["connect_args"] = {"check_same_thread": False}
        if parsed.database and parsed.database != ":memory:":
            Path(parsed.database).parent.mkdir(parents=True, exist_ok=True)

    _engine = create_engine(url, **options)
    SessionLocal.configure(bind=_engine)
    return _engine


def get_engine() -> Engine:
    return _engine if _engine is not None else configure_engine(settings.database_url)


def new_session() -> Session:
    get_engine()
    return SessionLocal()


def get_session() -> Iterator[Session]:
    """FastAPI dependency: one session per request."""
    with new_session() as session:
        yield session


def init_db() -> None:
    from . import models  # noqa: F401  (register tables on Base.metadata)

    Base.metadata.create_all(get_engine())


if __name__ == "__main__":
    # Under `python -m app.db` this file runs as `__main__`, a different module object from `app.db`
    # (which the models register on), so call init_db through the real module.
    from app.db import init_db as _init_db

    logging.basicConfig(level=settings.log_level)
    _init_db()
    logger.info("database tables created")
