"""Tests for the dead-letter queue model and pipeline integration."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from btcedu.db import Base
from btcedu.models.dead_letter import DeadLetterEntry


@pytest.fixture
def db_session():
    """In-memory SQLite session with dead_letter_queue table."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    # Create FTS table required by some models
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
                "USING fts5(chunk_id UNINDEXED, episode_id UNINDEXED, text)"
            )
        )
        conn.commit()
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()
    engine.dispose()


class TestDeadLetterEntry:
    """Test DeadLetterEntry model CRUD."""

    def test_create_entry(self, db_session):
        entry = DeadLetterEntry(
            episode_id="ep001",
            stage="tts",
            error_category="auth_error",
            error_message="401 Unauthorized",
            suggestion="Check .env for correct API keys.",
        )
        db_session.add(entry)
        db_session.commit()

        loaded = db_session.query(DeadLetterEntry).first()
        assert loaded.episode_id == "ep001"
        assert loaded.stage == "tts"
        assert loaded.error_category == "auth_error"
        assert loaded.resolved_at is None
        assert loaded.retry_count == 0

    def test_resolve_entry(self, db_session):
        entry = DeadLetterEntry(
            episode_id="ep002",
            stage="imagegen",
            error_category="content_error",
            error_message="Content policy violation",
            suggestion="Review prompt.",
        )
        db_session.add(entry)
        db_session.commit()

        # Resolve it
        entry.resolved_at = datetime.now(UTC)
        entry.resolved_by = "manual"
        db_session.commit()

        loaded = db_session.query(DeadLetterEntry).first()
        assert loaded.resolved_at is not None
        assert loaded.resolved_by == "manual"

    def test_query_pending(self, db_session):
        for i in range(3):
            entry = DeadLetterEntry(
                episode_id=f"ep{i:03d}",
                stage="download",
                error_category="network",
                error_message=f"Error {i}",
                suggestion="Check network.",
            )
            db_session.add(entry)
        db_session.commit()

        # Resolve one
        first = db_session.query(DeadLetterEntry).first()
        first.resolved_at = datetime.now(UTC)
        first.resolved_by = "auto_retry"
        db_session.commit()

        pending = (
            db_session.query(DeadLetterEntry)
            .filter(DeadLetterEntry.resolved_at.is_(None))
            .all()
        )
        assert len(pending) == 2

    def test_repr(self, db_session):
        entry = DeadLetterEntry(
            episode_id="ep001",
            stage="tts",
            error_category="auth_error",
            error_message="test",
            suggestion="test",
        )
        db_session.add(entry)
        db_session.commit()

        r = repr(entry)
        assert "ep001" in r
        assert "pending" in r

    def test_multiple_entries_per_episode(self, db_session):
        """Same episode can have multiple DLQ entries for different stages."""
        for stage in ["download", "tts", "render"]:
            entry = DeadLetterEntry(
                episode_id="ep001",
                stage=stage,
                error_category="network",
                error_message=f"{stage} failed",
                suggestion="Fix it.",
            )
            db_session.add(entry)
        db_session.commit()

        entries = (
            db_session.query(DeadLetterEntry)
            .filter(DeadLetterEntry.episode_id == "ep001")
            .all()
        )
        assert len(entries) == 3
