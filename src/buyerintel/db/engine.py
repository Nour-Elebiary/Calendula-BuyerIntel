"""Database engine, sessions and schema management (spec sections 4, 5)."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from buyerintel.db.models import Base

_engine: Engine | None = None
_session_factory: sessionmaker | None = None


def _sqlite_pragmas(dbapi_connection, _record) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


def make_engine(db_url: str) -> Engine:
    """Create an engine. SQLite gets WAL mode and FK enforcement."""
    kwargs: dict = {}
    if db_url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        # Ensure the parent directory of a file-based sqlite db exists.
        if ":///" in db_url:
            raw = db_url.split(":///", 1)[1]
            if raw and raw != ":memory:":
                Path(raw).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(db_url, **kwargs)
    if db_url.startswith("sqlite"):
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def get_engine(db_url: str | None = None) -> Engine:
    global _engine, _session_factory
    if _engine is None or db_url is not None:
        from buyerintel.config import load_settings

        _engine = make_engine(db_url or load_settings().db_url)
        _session_factory = sessionmaker(bind=_engine, expire_on_commit=False)
    return _engine


def get_session(db_url: str | None = None) -> Session:
    get_engine(db_url)
    assert _session_factory is not None
    return _session_factory()


def init_db(engine: Engine) -> None:
    """Create the schema if missing and stamp the schema version (M0 baseline)."""
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(
            text(
                "CREATE TABLE IF NOT EXISTS schema_version ("
                "version INTEGER NOT NULL, applied_at TEXT NOT NULL "
                "DEFAULT (datetime('now')))"
            )
        )
        row = conn.execute(text("SELECT MAX(version) FROM schema_version")).scalar()
        if row is None:
            conn.execute(text("INSERT INTO schema_version (version) VALUES (1)"))


def reset_engine() -> None:
    """Forget cached engine/session (used by tests)."""
    global _engine, _session_factory
    _engine = None
    _session_factory = None
