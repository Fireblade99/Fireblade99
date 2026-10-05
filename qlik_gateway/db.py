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
    _add_missing_columns(_engine)
    return _engine


def _add_missing_columns(engine) -> None:
    """Minimal forward migration: add columns introduced by a newer version to existing tables.

    Only columns that are nullable or have a server default are added (enough for our additive
    changes); nothing is ever dropped or altered.
    """
    from sqlalchemy import inspect, text

    insp = inspect(engine)
    for table in Base.metadata.sorted_tables:
        if not insp.has_table(table.name):
            continue
        existing = {c["name"] for c in insp.get_columns(table.name)}
        for col in table.columns:
            if col.name in existing or not (col.nullable or col.server_default is not None):
                continue
            # the API and the worker start together and may both try: IF NOT EXISTS on PostgreSQL,
            # and a column added by the other process in the meantime is not an error
            if_not = "IF NOT EXISTS " if engine.dialect.name == "postgresql" else ""
            ddl = f"ALTER TABLE {table.name} ADD COLUMN {if_not}{col.name} {col.type.compile(engine.dialect)}"
            if col.server_default is not None:
                ddl += f" DEFAULT '{col.server_default.arg}'"
            try:
                with engine.begin() as conn:
                    conn.execute(text(ddl))
            except Exception:  # noqa: BLE001
                if col.name not in {c["name"] for c in inspect(engine).get_columns(table.name)}:
                    raise


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
