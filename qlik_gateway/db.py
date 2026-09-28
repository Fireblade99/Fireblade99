from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


_engine = None
_SessionLocal: sessionmaker | None = None


def init_engine(url: str | None = None):
    global _engine, _SessionLocal
    url = url or get_settings().database_url
    kwargs = {"pool_pre_ping": True}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    _engine = create_engine(url, **kwargs)
    if url.startswith("sqlite"):

        @event.listens_for(_engine, "connect")
        def _sqlite_pragmas(conn, _):
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA foreign_keys=ON")
            cur.close()

    _SessionLocal = sessionmaker(bind=_engine, expire_on_commit=False)
    from . import models  # noqa: F401  (register tables)

    Base.metadata.create_all(_engine)
    return _engine


def get_engine():
    if _engine is None:
        init_engine()
    return _engine


@contextmanager
def session_scope() -> Iterator[Session]:
    if _SessionLocal is None:
        init_engine()
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_db() -> Iterator[Session]:
    """FastAPI dependency."""
    with session_scope() as s:
        yield s
