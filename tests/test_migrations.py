"""Tests for database migrations."""

from datetime import UTC

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from btcedu.migrations import (
    AddChannelsSupportMigration,
    get_applied_migrations,
    get_pending_migrations,
    run_migrations,
)
from btcedu.models.migration import SchemaMigration


@pytest.fixture
def old_db_engine():
    """Create an old database without channels support."""
    engine = create_engine("sqlite:///:memory:")

    # Create only the base tables without channels
    with engine.connect() as conn:
        # Create episodes table WITHOUT channel_id column (old schema)
        conn.execute(
            text("""
            CREATE TABLE episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id VARCHAR(64) UNIQUE NOT NULL,
                source VARCHAR(32) NOT NULL DEFAULT 'youtube_rss',
                title VARCHAR(500) NOT NULL,
                published_at TIMESTAMP,
                duration_seconds INTEGER,
                url VARCHAR(500) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'new',
                audio_path VARCHAR(500),
                transcript_path VARCHAR(500),
                output_dir VARCHAR(500),
                detected_at TIMESTAMP NOT NULL,
                completed_at TIMESTAMP,
                error_message TEXT,
                retry_count INTEGER NOT NULL DEFAULT 0
            )
        """)
        )

        # Create pipeline_runs table
        conn.execute(
            text("""
            CREATE TABLE pipeline_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id INTEGER NOT NULL,
                stage VARCHAR(20) NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'running',
                started_at TIMESTAMP NOT NULL,
                completed_at TIMESTAMP,
                input_tokens INTEGER NOT NULL DEFAULT 0,
                output_tokens INTEGER NOT NULL DEFAULT 0,
                estimated_cost_usd REAL NOT NULL DEFAULT 0.0,
                error_message TEXT,
                FOREIGN KEY (episode_id) REFERENCES episodes(id)
            )
        """)
        )

        # Create chunks table
        conn.execute(
            text("""
            CREATE TABLE chunks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chunk_id VARCHAR(64) UNIQUE NOT NULL,
                episode_id VARCHAR(64) NOT NULL,
                ordinal INTEGER NOT NULL,
                text TEXT NOT NULL,
                token_estimate INTEGER NOT NULL,
                start_char INTEGER NOT NULL,
                end_char INTEGER NOT NULL
            )
        """)
        )

        # Create index
        conn.execute(text("CREATE INDEX idx_chunks_episode_id ON chunks(episode_id)"))

        # Create FTS5 table
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
                "USING fts5(chunk_id UNINDEXED, episode_id UNINDEXED, text)"
            )
        )

        # Create schema_migrations table (for tracking)
        conn.execute(
            text("""
            CREATE TABLE schema_migrations (
                version VARCHAR(64) PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL
            )
        """)
        )

        conn.commit()

    yield engine
    engine.dispose()


@pytest.fixture
def old_db_session(old_db_engine):
    """Session for old database."""
    factory = sessionmaker(bind=old_db_engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture
def seeded_old_db(old_db_session):
    """Old database with some episodes (no channel_id)."""
    # Insert episodes directly with raw SQL since ORM would fail
    from datetime import datetime

    old_db_session.execute(
        text("""
        INSERT INTO episodes (episode_id, title, url, status, detected_at)
        VALUES
            ('ep001', 'Episode 1', 'https://youtube.com/watch?v=ep001', 'new', :now),
            ('ep002', 'Episode 2', 'https://youtube.com/watch?v=ep002', 'downloaded', :now),
            ('ep003', 'Episode 3', 'https://youtube.com/watch?v=ep003', 'completed', :now)
    """),
        {"now": datetime.now(UTC)},
    )
    old_db_session.commit()

    return old_db_session


def test_migration_on_old_database(seeded_old_db):
    """Test migration adds channels table and channel_id column."""
    session = seeded_old_db

    # Verify old schema (no channel_id column)
    result = session.execute(text("PRAGMA table_info(episodes)"))
    columns = [row[1] for row in result.fetchall()]
    assert "channel_id" not in columns

    # Verify no channels table
    result = session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='channels'")
    )
    assert result.fetchone() is None

    # Apply migration
    migration = AddChannelsSupportMigration()
    migration.up(session)

    # Verify channel_id column was added
    result = session.execute(text("PRAGMA table_info(episodes)"))
    columns = [row[1] for row in result.fetchall()]
    assert "channel_id" in columns

    # Verify channels table was created
    result = session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='channels'")
    )
    assert result.fetchone() is not None

    # Verify default channel was created
    result = session.execute(text("SELECT COUNT(*) FROM channels WHERE channel_id = 'default'"))
    assert result.scalar() == 1

    # Verify existing episodes were backfilled
    result = session.execute(text("SELECT COUNT(*) FROM episodes WHERE channel_id = 'default'"))
    assert result.scalar() == 3

    # Verify index was created
    result = session.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' AND name='idx_episodes_channel_id'")
    )
    assert result.fetchone() is not None

    # Verify migration was marked as applied
    result = (
        session.query(SchemaMigration)
        .filter(SchemaMigration.version == "001_add_channels_support")
        .first()
    )
    assert result is not None


def test_migration_idempotency(seeded_old_db):
    """Test that running migration twice doesn't cause errors."""
    session = seeded_old_db

    # Apply migration first time
    migration = AddChannelsSupportMigration()
    migration.up(session)

    # Apply migration second time (should be idempotent)
    migration.up(session)

    # Verify still only one default channel
    result = session.execute(text("SELECT COUNT(*) FROM channels WHERE channel_id = 'default'"))
    assert result.scalar() == 1

    # Verify all episodes still have channel_id
    result = session.execute(text("SELECT COUNT(*) FROM episodes WHERE channel_id IS NULL"))
    assert result.scalar() == 0


def test_get_pending_migrations(old_db_session):
    """Test getting pending migrations."""
    pending = get_pending_migrations(old_db_session)

    # Should have at least the channels migration
    assert len(pending) >= 1
    assert any(m.version == "001_add_channels_support" for m in pending)


def test_get_applied_migrations(old_db_session):
    """Test getting applied migrations."""
    # No migrations applied yet
    applied = get_applied_migrations(old_db_session)
    assert len(applied) == 0

    # Apply migration
    migration = AddChannelsSupportMigration()
    migration.up(old_db_session)

    # Check applied migrations
    applied = get_applied_migrations(old_db_session)
    assert "001_add_channels_support" in applied


def test_run_migrations(old_db_session):
    """Test run_migrations function."""
    # Verify migration is pending
    pending = get_pending_migrations(old_db_session)
    assert len(pending) >= 1

    # Run migrations
    run_migrations(old_db_session, dry_run=False)

    # Verify no more pending migrations
    pending = get_pending_migrations(old_db_session)
    assert len(pending) == 0

    # Verify channels table exists
    result = old_db_session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='channels'")
    )
    assert result.fetchone() is not None


def test_migration_dry_run(old_db_session):
    """Test dry run doesn't make changes."""
    # Run in dry-run mode
    run_migrations(old_db_session, dry_run=True)

    # Verify no changes were made
    result = old_db_session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='channels'")
    )
    assert result.fetchone() is None

    # Migrations should still be pending
    pending = get_pending_migrations(old_db_session)
    assert len(pending) >= 1


def test_api_works_after_migration(seeded_old_db):
    """Test that ORM queries work after migration."""
    session = seeded_old_db

    # Apply migration
    migration = AddChannelsSupportMigration()
    migration.up(session)

    # Now we should be able to query episodes with ORM
    # (Before migration, this would fail because Episode model expects channel_id)
    from btcedu.models.channel import Channel

    # Query episodes
    result = session.execute(
        text("SELECT episode_id, channel_id, title FROM episodes ORDER BY episode_id")
    )
    episodes = result.fetchall()
    assert len(episodes) == 3
    assert all(ep[1] == "default" for ep in episodes)

    # Query channel
    channel = session.query(Channel).filter(Channel.channel_id == "default").first()
    assert channel is not None
    assert channel.name == "Default Channel"
    assert channel.is_active is True
