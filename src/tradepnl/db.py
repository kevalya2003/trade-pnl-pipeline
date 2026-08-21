"""Engine construction and schema helpers."""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from tradepnl.models import Base

DEFAULT_DATABASE_URL = "postgresql+psycopg://trades:trades@localhost:5432/trades"


def database_url() -> str:
    """Resolve the connection string from the environment."""
    return os.environ.get("TRADEPNL_DATABASE_URL", DEFAULT_DATABASE_URL)


def make_engine(url: str | None = None, *, echo: bool = False) -> Engine:
    """Build an engine with a pre-ping, so a recycled connection does not fail a run."""
    return create_engine(url or database_url(), echo=echo, pool_pre_ping=True, future=True)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a configured session factory."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on any exception."""
    session = session_factory(engine)()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def read_sql(name: str) -> str:
    """Load a packaged .sql file by filename."""
    return resources.files("tradepnl.sql").joinpath(name).read_text(encoding="utf-8")


def create_schema(engine: Engine) -> None:
    """Create tables and views. Safe to run repeatedly."""
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(read_sql("views.sql")))


VIEWS = ("v_top_instruments_by_month", "v_running_pnl", "v_valid_trade")


def drop_schema(engine: Engine) -> None:
    """Drop views and tables. Used by tests and by ``tradepnl reset``.

    Views must go first and the list must be complete, otherwise ``drop_all`` fails on
    the table a surviving view depends on.
    """
    with engine.begin() as conn:
        for view in VIEWS:
            conn.execute(text(f"DROP VIEW IF EXISTS {view} CASCADE"))
    Base.metadata.drop_all(engine)
