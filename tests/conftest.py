from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from btcedu.db import Base

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_TRANSCRIPT = (FIXTURES / "sample_transcript_de.txt").read_text()


@pytest.fixture
def db_engine():
    """In-memory SQLite engine for tests with FTS5."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    # Create FTS5 virtual table
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
                "USING fts5(chunk_id UNINDEXED, episode_id UNINDEXED, text)"
            )
        )
        conn.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """Database session for tests."""
    factory = sessionmaker(bind=db_engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture
def chunked_episode(db_session):
    """Episode at CHUNKED status with chunks in DB + FTS5."""
    from btcedu.core.chunker import chunk_text, persist_chunks
    from btcedu.models.episode import Episode, EpisodeStatus

    episode = Episode(
        episode_id="ep001",
        source="youtube_rss",
        title="Bitcoin und die Zukunft des Geldes",
        url="https://youtube.com/watch?v=ep001",
        status=EpisodeStatus.CHUNKED,
        transcript_path="/tmp/transcript.txt",
    )
    db_session.add(episode)
    db_session.commit()

    chunks = chunk_text(SAMPLE_TRANSCRIPT, "ep001", chunk_size=500)
    persist_chunks(db_session, chunks, "ep001")

    return episode
