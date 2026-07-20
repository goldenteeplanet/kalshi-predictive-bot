import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker

from kalshi_predictor.config import get_settings
from kalshi_predictor.data.backend import (
    database_url_from_settings,
    redact_database_url,
    warn_if_sqlite_on_onedrive,
)
from kalshi_predictor.data.schema import Base

logger = logging.getLogger(__name__)


def make_engine(db_url: str | None = None) -> Engine:
    resolved_db_url = db_url or database_url_from_settings(get_settings())
    _ensure_sqlite_parent(resolved_db_url)
    engine_kwargs: dict[str, Any] = {"future": True}
    if _is_sqlite_url(resolved_db_url):
        engine_kwargs["connect_args"] = {"timeout": 30, "check_same_thread": False}
        warning = warn_if_sqlite_on_onedrive(db_url=resolved_db_url)
        if warning:
            logger.warning("%s Path: %s", warning, describe_db_location(resolved_db_url))
    elif _is_postgres_url(resolved_db_url):
        engine_kwargs.update(
            {
                "pool_pre_ping": True,
                "pool_size": 10,
                "max_overflow": 20,
                "pool_timeout": 30,
                "isolation_level": "READ COMMITTED",
            }
        )
    engine = create_engine(resolved_db_url, **engine_kwargs)
    if _is_sqlite_url(resolved_db_url):
        _configure_sqlite_pragmas(engine, resolved_db_url)
    return engine


def get_session_factory(engine: Engine | None = None) -> sessionmaker[Session]:
    return sessionmaker(bind=engine or make_engine(), expire_on_commit=False, autoflush=False)


def init_db(db_url: str | None = None) -> Engine:
    resolved_db_url = db_url or database_url_from_settings(get_settings())
    engine = make_engine(resolved_db_url)
    Base.metadata.create_all(engine)
    if _is_sqlite_url(resolved_db_url):
        with engine.begin() as connection:
            connection.execute(text("PRAGMA busy_timeout=30000"))
            connection.execute(text("PRAGMA synchronous=NORMAL"))
            if _is_file_sqlite_url(resolved_db_url):
                connection.execute(text("PRAGMA journal_mode=WAL"))
    return engine


@contextmanager
def session_scope(db_url: str | None = None) -> Iterator[Session]:
    engine = init_db(db_url)
    session_factory = get_session_factory(engine)
    session = session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def describe_db_location(db_url: str | None = None) -> str:
    resolved_db_url = db_url or database_url_from_settings(get_settings())
    url = make_url(resolved_db_url)
    if url.drivername.startswith("sqlite") and url.database:
        return str(Path(url.database).resolve()) if url.database != ":memory:" else ":memory:"
    return redact_database_url(resolved_db_url)


def _ensure_sqlite_parent(db_url: str) -> None:
    url = make_url(db_url)
    if not url.drivername.startswith("sqlite"):
        return
    if not url.database or url.database == ":memory:":
        return
    Path(url.database).expanduser().parent.mkdir(parents=True, exist_ok=True)


def _is_sqlite_url(db_url: str) -> bool:
    return make_url(db_url).drivername.startswith("sqlite")


def _is_postgres_url(db_url: str) -> bool:
    driver = make_url(db_url).drivername
    return driver.startswith("postgresql") or driver.startswith("postgres")


def _is_file_sqlite_url(db_url: str) -> bool:
    url = make_url(db_url)
    return url.drivername.startswith("sqlite") and bool(url.database and url.database != ":memory:")


def _configure_sqlite_pragmas(engine: Engine, db_url: str) -> None:
    file_backed = _is_file_sqlite_url(db_url)

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            if file_backed:
                cursor.execute("PRAGMA journal_mode=WAL")
        finally:
            cursor.close()
