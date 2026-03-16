from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from btcedu.config import get_settings


class Base(DeclarativeBase):
    pass


def get_engine(database_url: str | None = None):
    url = database_url or get_settings().database_url
    kwargs: dict = {"echo": False}
    if url and url.startswith("sqlite"):
        kwargs["connect_args"] = {"timeout": 30}
    engine = create_engine(url, **kwargs)
    # Enable WAL mode for SQLite so readers don't block writers
    if url and url.startswith("sqlite") and ":memory:" not in url:
        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.commit()
    return engine


def get_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    engine = get_engine(database_url)
    return sessionmaker(bind=engine)


def init_db(database_url: str | None = None) -> None:
    """Create all tables including FTS5 virtual table."""
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
    _init_fts(engine)


def _init_fts(engine) -> None:
    """Create FTS5 virtual table for chunk full-text search."""
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
                "USING fts5(chunk_id UNINDEXED, episode_id UNINDEXED, text)"
            )
        )
        conn.commit()
