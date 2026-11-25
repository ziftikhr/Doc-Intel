"""Engine / session factory and schema creation."""

from __future__ import annotations

from contextlib import contextmanager
from functools import lru_cache
from typing import Iterator

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from .models import EXTENSIONS_SQL, UPGRADE_SQL, Base


@lru_cache(maxsize=8)
def get_engine(url: str) -> Engine:
    return create_engine(url, pool_pre_ping=True, pool_size=10, max_overflow=10, future=True)


def session_factory(url: str) -> sessionmaker[Session]:
    return sessionmaker(bind=get_engine(url), expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Commit on success, roll back on error."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db(engine: Engine) -> None:
    """Create extensions, tables and indexes (idempotent).

    A production deployment would manage schema changes with Alembic migrations;
    for this project ``create_all`` on start-up keeps setup to one command.
    """
    with engine.begin() as conn:
        # Serialise concurrent start-ups (API + worker starting together).
        conn.execute(text("SELECT pg_advisory_xact_lock(727001)"))
        for stmt in EXTENSIONS_SQL:
            conn.execute(stmt)
        Base.metadata.create_all(conn)
        for stmt in UPGRADE_SQL:
            conn.execute(stmt)


def drop_all(engine: Engine) -> None:
    with engine.begin() as conn:
        Base.metadata.drop_all(conn)


def check_connection(engine: Engine) -> tuple[bool, str]:
    try:
        with engine.connect() as conn:
            v = conn.execute(text("SELECT extversion FROM pg_extension WHERE extname = 'vector'")).scalar()
        return True, f"pgvector {v}" if v else "connected (pgvector missing)"
    except Exception as e:  # noqa: BLE001
        return False, f"{type(e).__name__}: {str(e).splitlines()[0][:200]}"
