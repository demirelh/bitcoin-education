"""Tests for Sprint 1 database migrations (002-004)."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from btcedu.migrations import (
    AddV2PipelineColumnsMigration,
    CreatePromptVersionsTableMigration,
    CreateReviewTablesMigration,
    get_pending_migrations,
    run_migrations,
)


@pytest.fixture
def post_001_db_engine():
    """Database with migration 001 already applied (has channels table + channel_id)."""
    engine = create_engine("sqlite:///:memory:")

    with engine.connect() as conn:
        # Base episodes table with channel_id (post-001 state)
        conn.execute(
            text("""
            CREATE TABLE episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                episode_id VARCHAR(64) UNIQUE NOT NULL,
                channel_id VARCHAR(64),
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

        conn.execute(
            text("""
            CREATE TABLE channels (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                channel_id VARCHAR(64) UNIQUE NOT NULL,
                name VARCHAR(200) NOT NULL,
                youtube_channel_id VARCHAR(64),
                rss_url VARCHAR(500),
                is_active BOOLEAN NOT NULL DEFAULT 1,
                created_at TIMESTAMP NOT NULL,
                updated_at TIMESTAMP NOT NULL
            )
        """)
        )

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

        conn.execute(
            text("""
            CREATE TABLE schema_migrations (
                version VARCHAR(64) PRIMARY KEY,
                applied_at TIMESTAMP NOT NULL
            )
        """)
        )

        # Mark migration 001 as already applied
        conn.execute(
            text(
                "INSERT INTO schema_migrations (version, applied_at) "
                "VALUES ('001_add_channels_support', :now)"
            ),
            {"now": datetime.now(UTC)},
        )

        conn.commit()

    yield engine
    engine.dispose()


@pytest.fixture
def post_001_session(post_001_db_engine):
    factory = sessionmaker(bind=post_001_db_engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture
def seeded_post_001(post_001_session):
    """Post-001 database with some episodes."""
    post_001_session.execute(
        text("""
        INSERT INTO episodes (episode_id, channel_id, title, url, status, detected_at)
        VALUES
            ('ep001', 'default', 'Episode 1', 'https://youtube.com/watch?v=ep001', 'new', :now),
            (
                'ep002', 'default', 'Episode 2',
                'https://youtube.com/watch?v=ep002', 'completed', :now
            )
    """),
        {"now": datetime.now(UTC)},
    )
    post_001_session.commit()
    return post_001_session


# --- Migration 002 tests ---


def test_migration_002_adds_columns(seeded_post_001):
    session = seeded_post_001
    migration = AddV2PipelineColumnsMigration()
    migration.up(session)

    result = session.execute(text("PRAGMA table_info(episodes)"))
    columns = [row[1] for row in result.fetchall()]

    assert "pipeline_version" in columns
    assert "review_status" in columns
    assert "youtube_video_id" in columns
    assert "published_at_youtube" in columns

    # Verify existing episodes get pipeline_version=1
    result = session.execute(
        text("SELECT pipeline_version FROM episodes WHERE episode_id = 'ep001'")
    )
    assert result.scalar() == 1


def test_migration_002_idempotent(seeded_post_001):
    session = seeded_post_001
    migration = AddV2PipelineColumnsMigration()
    migration.up(session)
    migration.up(session)  # Second run should not error

    result = session.execute(text("PRAGMA table_info(episodes)"))
    columns = [row[1] for row in result.fetchall()]
    assert "pipeline_version" in columns


# --- Migration 003 tests ---


def test_migration_003_creates_prompt_versions(post_001_session):
    session = post_001_session
    migration = CreatePromptVersionsTableMigration()
    migration.up(session)

    # Table exists
    result = session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='prompt_versions'")
    )
    assert result.fetchone() is not None

    # Check columns
    result = session.execute(text("PRAGMA table_info(prompt_versions)"))
    columns = {row[1] for row in result.fetchall()}
    expected = {
        "id",
        "name",
        "version",
        "content_hash",
        "template_path",
        "model",
        "temperature",
        "max_tokens",
        "is_default",
        "created_at",
        "notes",
    }
    assert expected.issubset(columns)

    # Check indexes
    result = session.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='prompt_versions'")
    )
    index_names = {row[0] for row in result.fetchall()}
    assert "idx_prompt_versions_name" in index_names
    assert "idx_prompt_versions_default" in index_names


def test_migration_003_idempotent(post_001_session):
    session = post_001_session
    migration = CreatePromptVersionsTableMigration()
    migration.up(session)
    migration.up(session)  # Should not error


# --- Migration 004 tests ---


def test_migration_004_creates_review_tables(post_001_session):
    session = post_001_session

    # Need 003 first for FK
    CreatePromptVersionsTableMigration().up(session)

    migration = CreateReviewTablesMigration()
    migration.up(session)

    # review_tasks exists
    result = session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='review_tasks'")
    )
    assert result.fetchone() is not None

    # review_decisions exists
    result = session.execute(
        text("SELECT name FROM sqlite_master WHERE type='table' AND name='review_decisions'")
    )
    assert result.fetchone() is not None

    # Check review_tasks columns
    result = session.execute(text("PRAGMA table_info(review_tasks)"))
    columns = {row[1] for row in result.fetchall()}
    expected = {
        "id",
        "episode_id",
        "stage",
        "status",
        "artifact_paths",
        "diff_path",
        "prompt_version_id",
        "created_at",
        "reviewed_at",
        "reviewer_notes",
        "artifact_hash",
    }
    assert expected.issubset(columns)

    # Check review_decisions columns
    result = session.execute(text("PRAGMA table_info(review_decisions)"))
    columns = {row[1] for row in result.fetchall()}
    expected = {"id", "review_task_id", "decision", "notes", "decided_at"}
    assert expected.issubset(columns)

    # Check indexes
    result = session.execute(
        text("SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='review_tasks'")
    )
    index_names = {row[0] for row in result.fetchall()}
    assert "idx_review_tasks_episode_stage" in index_names
    assert "idx_review_tasks_status" in index_names


def test_migration_004_idempotent(post_001_session):
    session = post_001_session
    CreatePromptVersionsTableMigration().up(session)
    migration = CreateReviewTablesMigration()
    migration.up(session)
    migration.up(session)  # Should not error


# --- Integration tests ---


def test_all_migrations_run_sequentially(post_001_session):
    """Run all pending migrations via run_migrations()."""
    session = post_001_session

    pending = get_pending_migrations(session)
    # 001 is already applied, so we should see 002, 003, 004
    assert len(pending) == 3

    run_migrations(session, dry_run=False)

    pending = get_pending_migrations(session)
    assert len(pending) == 0


def test_existing_pipeline_works_after_migrations(seeded_post_001):
    """After all migrations, existing v1 episodes can be queried."""
    session = seeded_post_001

    AddV2PipelineColumnsMigration().up(session)
    CreatePromptVersionsTableMigration().up(session)
    CreateReviewTablesMigration().up(session)

    # Query existing episodes
    result = session.execute(
        text("SELECT episode_id, status, pipeline_version FROM episodes ORDER BY episode_id")
    )
    rows = result.fetchall()
    assert len(rows) == 2
    assert rows[0][0] == "ep001"
    assert rows[0][1] == "new"
    assert rows[0][2] == 1
    assert rows[1][0] == "ep002"
    assert rows[1][1] == "completed"
    assert rows[1][2] == 1

    # Can update an episode status
    session.execute(text("UPDATE episodes SET status = 'downloaded' WHERE episode_id = 'ep001'"))
    session.commit()

    result = session.execute(text("SELECT status FROM episodes WHERE episode_id = 'ep001'"))
    assert result.scalar() == "downloaded"
