from sqlalchemy import create_engine, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from btcedu.config import get_settings


class Base(DeclarativeBase):
    pass


def get_engine(database_url: str | None = None):
    url = database_url or get_settings().database_url
    return create_engine(url, echo=False)


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
