from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from btcedu.config import get_settings


class Base(DeclarativeBase):
    pass


_engine_cache: dict[str, object] = {}


def get_engine(database_url: str | None = None):
    url = database_url or get_settings().database_url
    if url in _engine_cache:
        return _engine_cache[url]
    kwargs: dict = {"echo": False}
    if url and url.startswith("sqlite"):
        # 5-minute busy timeout so long-running renders don't hit "database is
        # locked" during their final ContentArtifact/MediaAsset commit.
        kwargs["connect_args"] = {"timeout": 300, "check_same_thread": False}
    engine = create_engine(url, **kwargs)
    if url and url.startswith("sqlite") and ":memory:" not in url:
        # Enable WAL + long busy timeout on every new connection (not just on
        # engine creation). Without this, connections created after the initial
        # setup revert to the default 5s busy_timeout.
        @event.listens_for(engine, "connect")
        def _sqlite_pragma(dbapi_connection, connection_record):  # type: ignore[unused-argument]
            cur = dbapi_connection.cursor()
            try:
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA busy_timeout=300000")  # 5 minutes
                cur.execute("PRAGMA synchronous=NORMAL")
            finally:
                cur.close()

        with engine.connect() as conn:
            conn.execute(text("PRAGMA journal_mode=WAL"))
            conn.commit()
    _engine_cache[url] = engine
    return engine


def get_session_factory(database_url: str | None = None) -> sessionmaker[Session]:
    engine = get_engine(database_url)
    return sessionmaker(bind=engine)


def init_db(database_url: str | None = None) -> None:
    """Create all tables."""
    engine = get_engine(database_url)
    Base.metadata.create_all(engine)
