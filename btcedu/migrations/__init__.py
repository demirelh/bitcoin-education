"""Database migration system for btcedu."""

import logging
from abc import ABC, abstractmethod
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

from btcedu.models.migration import SchemaMigration

logger = logging.getLogger(__name__)


class Migration(ABC):
    """Base class for database migrations."""

    @property
    @abstractmethod
    def version(self) -> str:
        """Unique version identifier for this migration."""
        pass

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description of this migration."""
        pass

    @abstractmethod
    def up(self, session: Session) -> None:
        """Apply the migration."""
        pass

    def down(self, session: Session) -> None:
        """Rollback the migration (optional, not always safe)."""
        raise NotImplementedError(f"Migration {self.version} does not support rollback")

    def is_applied(self, session: Session) -> bool:
        """Check if this migration has been applied."""
        result = (
            session.query(SchemaMigration).filter(SchemaMigration.version == self.version).first()
        )
        return result is not None

    def mark_applied(self, session: Session) -> None:
        """Mark this migration as applied."""
        # Check if already marked
        if self.is_applied(session):
            logger.info(f"Migration {self.version} already marked as applied")
            return

        migration = SchemaMigration(version=self.version, applied_at=datetime.now(UTC))
        session.add(migration)
        session.commit()
        logger.info(f"Marked migration {self.version} as applied")


class AddChannelsSupportMigration(Migration):
    """Migration v1: Add multi-channel support to the database."""

    @property
    def version(self) -> str:
        return "001_add_channels_support"

    @property
    def description(self) -> str:
        return "Add channels table and channel_id to episodes"

    def up(self, session: Session) -> None:
        """Apply the migration."""
        logger.info(f"Running migration: {self.version}")
        logger.info(f"Description: {self.description}")

        # Step 1: Create channels table if not exists
        logger.info("Step 1/6: Creating channels table...")
        result = session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='channels'")
        )
        if not result.fetchone():
            session.execute(
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
            session.commit()
            logger.info("✓ Created channels table")
        else:
            logger.info("✓ Channels table already exists (skipped)")

        # Step 2: Add channel_id column to episodes if missing
        logger.info("Step 2/6: Adding channel_id column to episodes...")
        result = session.execute(text("PRAGMA table_info(episodes)"))
        columns = [row[1] for row in result.fetchall()]

        if "channel_id" not in columns:
            session.execute(text("ALTER TABLE episodes ADD COLUMN channel_id VARCHAR(64)"))
            session.commit()
            logger.info("✓ Added channel_id column to episodes")
        else:
            logger.info("✓ Channel_id column already exists (skipped)")

        # Step 3: Create default channel from existing config
        logger.info("Step 3/6: Creating default channel...")
        from btcedu.config import get_settings

        settings = get_settings()

        result = session.execute(text("SELECT COUNT(*) FROM channels WHERE channel_id = 'default'"))
        count = result.scalar()

        if count == 0:
            # Determine feed URL from settings
            feed_url = None
            if settings.podcast_rss_url:
                feed_url = settings.podcast_rss_url
            elif settings.podcast_youtube_channel_id:
                feed_url = f"https://www.youtube.com/feeds/videos.xml?channel_id={settings.podcast_youtube_channel_id}"

            session.execute(
                text("""
                    INSERT INTO channels
                    (channel_id, name, youtube_channel_id, rss_url,
                     is_active, created_at, updated_at)
                    VALUES (:channel_id, :name, :youtube_channel_id,
                            :rss_url, 1, :now, :now)
                """),
                {
                    "channel_id": "default",
                    "name": "Default Channel",
                    "youtube_channel_id": settings.podcast_youtube_channel_id or None,
                    "rss_url": feed_url,
                    "now": datetime.now(UTC),
                },
            )
            session.commit()
            logger.info("✓ Created default channel")
        else:
            logger.info("✓ Default channel already exists (skipped)")

        # Step 4: Backfill existing episodes
        logger.info("Step 4/6: Backfilling existing episodes with default channel_id...")
        result = session.execute(text("SELECT COUNT(*) FROM episodes WHERE channel_id IS NULL"))
        null_count = result.scalar()

        if null_count > 0:
            session.execute(
                text("UPDATE episodes SET channel_id = 'default' WHERE channel_id IS NULL")
            )
            session.commit()
            logger.info(f"✓ Backfilled {null_count} episodes with default channel_id")
        else:
            logger.info("✓ No episodes to backfill (skipped)")

        # Step 5: Add index on channel_id
        logger.info("Step 5/6: Creating index on episodes.channel_id...")
        result = session.execute(
            text(
                "SELECT name FROM sqlite_master "
                "WHERE type='index' AND name='idx_episodes_channel_id'"
            )
        )
        if not result.fetchone():
            session.execute(text("CREATE INDEX idx_episodes_channel_id ON episodes(channel_id)"))
            session.commit()
            logger.info("✓ Created index on episodes.channel_id")
        else:
            logger.info("✓ Index already exists (skipped)")

        # Step 6: Mark migration as applied
        logger.info("Step 6/6: Marking migration as applied...")
        self.mark_applied(session)
        logger.info("✓ Migration completed successfully!")


class AddV2PipelineColumnsMigration(Migration):
    """Migration v2: Add v2 pipeline columns to episodes table."""

    @property
    def version(self) -> str:
        return "002_add_v2_pipeline_columns"

    @property
    def description(self) -> str:
        return (
            "Add pipeline_version, review_status, youtube_video_id, "
            "published_at_youtube columns to episodes"
        )

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(text("PRAGMA table_info(episodes)"))
        columns = [row[1] for row in result.fetchall()]

        if "pipeline_version" not in columns:
            session.execute(
                text("ALTER TABLE episodes ADD COLUMN pipeline_version INTEGER DEFAULT 1")
            )
            session.commit()
            logger.info("Added pipeline_version column")

        if "review_status" not in columns:
            session.execute(text("ALTER TABLE episodes ADD COLUMN review_status TEXT"))
            session.commit()
            logger.info("Added review_status column")

        if "youtube_video_id" not in columns:
            session.execute(text("ALTER TABLE episodes ADD COLUMN youtube_video_id TEXT"))
            session.commit()
            logger.info("Added youtube_video_id column")

        if "published_at_youtube" not in columns:
            session.execute(text("ALTER TABLE episodes ADD COLUMN published_at_youtube TIMESTAMP"))
            session.commit()
            logger.info("Added published_at_youtube column")

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class CreatePromptVersionsTableMigration(Migration):
    """Migration v3: Create prompt_versions table."""

    @property
    def version(self) -> str:
        return "003_create_prompt_versions"

    @property
    def description(self) -> str:
        return "Create prompt_versions table for prompt versioning system"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='prompt_versions'")
        )
        if not result.fetchone():
            session.execute(
                text("""
                    CREATE TABLE prompt_versions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        name TEXT NOT NULL,
                        version INTEGER NOT NULL,
                        content_hash TEXT NOT NULL,
                        template_path TEXT,
                        model TEXT,
                        temperature REAL,
                        max_tokens INTEGER,
                        is_default BOOLEAN NOT NULL DEFAULT 0,
                        created_at TIMESTAMP NOT NULL,
                        notes TEXT,
                        UNIQUE(name, version),
                        UNIQUE(name, content_hash)
                    )
                """)
            )
            session.execute(text("CREATE INDEX idx_prompt_versions_name ON prompt_versions(name)"))
            session.execute(
                text(
                    "CREATE INDEX idx_prompt_versions_default ON prompt_versions(name, is_default)"
                )
            )
            session.commit()
            logger.info("Created prompt_versions table with indexes")

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class CreateReviewTablesMigration(Migration):
    """Migration v4: Create review_tasks and review_decisions tables."""

    @property
    def version(self) -> str:
        return "004_create_review_tables"

    @property
    def description(self) -> str:
        return "Create review_tasks and review_decisions tables"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        # Create review_tasks table
        result = session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='review_tasks'")
        )
        if not result.fetchone():
            session.execute(
                text("""
                    CREATE TABLE review_tasks (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        episode_id TEXT NOT NULL,
                        stage TEXT NOT NULL,
                        status TEXT NOT NULL DEFAULT 'pending',
                        artifact_paths TEXT,
                        diff_path TEXT,
                        prompt_version_id INTEGER,
                        created_at TIMESTAMP NOT NULL,
                        reviewed_at TIMESTAMP,
                        reviewer_notes TEXT,
                        artifact_hash TEXT,
                        FOREIGN KEY (prompt_version_id) REFERENCES prompt_versions(id)
                    )
                """)
            )
            session.execute(
                text(
                    "CREATE INDEX idx_review_tasks_episode_stage ON review_tasks(episode_id, stage)"
                )
            )
            session.execute(text("CREATE INDEX idx_review_tasks_status ON review_tasks(status)"))
            session.commit()
            logger.info("Created review_tasks table with indexes")

        # Create review_decisions table
        result = session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='review_decisions'")
        )
        if not result.fetchone():
            session.execute(
                text("""
                    CREATE TABLE review_decisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        review_task_id INTEGER NOT NULL,
                        decision TEXT NOT NULL,
                        notes TEXT,
                        decided_at TIMESTAMP NOT NULL,
                        FOREIGN KEY (review_task_id) REFERENCES review_tasks(id)
                    )
                """)
            )
            session.execute(
                text("CREATE INDEX idx_review_decisions_task ON review_decisions(review_task_id)")
            )
            session.commit()
            logger.info("Created review_decisions table with index")

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class CreateMediaAssetsTableMigration(Migration):
    """Migration v5: Create media_assets table for tracking generated media files."""

    @property
    def version(self) -> str:
        return "005_create_media_assets"

    @property
    def description(self) -> str:
        return "Create media_assets table for tracking images, audio, and video files"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        # Create media_assets table
        result = session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='media_assets'")
        )
        if not result.fetchone():
            session.execute(
                text("""
                    CREATE TABLE media_assets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        episode_id TEXT NOT NULL,
                        asset_type TEXT NOT NULL,
                        chapter_id TEXT,
                        file_path TEXT NOT NULL,
                        mime_type TEXT NOT NULL,
                        size_bytes INTEGER NOT NULL,
                        duration_seconds REAL,
                        meta TEXT,
                        prompt_version_id INTEGER,
                        created_at TIMESTAMP NOT NULL,
                        FOREIGN KEY (prompt_version_id) REFERENCES prompt_versions(id)
                    )
                """)
            )
            session.execute(
                text(
                    "CREATE INDEX idx_media_assets_episode_type_chapter "
                    "ON media_assets(episode_id, asset_type, chapter_id)"
                )
            )
            session.execute(text("CREATE INDEX idx_media_assets_type ON media_assets(asset_type)"))
            session.execute(
                text("CREATE INDEX idx_media_assets_episode ON media_assets(episode_id)")
            )
            session.commit()
            logger.info("Created media_assets table with indexes")

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


# Registry of all available migrations
MIGRATIONS = [
    AddChannelsSupportMigration(),
    AddV2PipelineColumnsMigration(),
    CreatePromptVersionsTableMigration(),
    CreateReviewTablesMigration(),
    CreateMediaAssetsTableMigration(),
]


def get_pending_migrations(session: Session) -> list[Migration]:
    """Get list of migrations that haven't been applied yet."""
    # Ensure schema_migrations table exists
    _ensure_migrations_table(session)

    pending = []
    for migration in MIGRATIONS:
        if not migration.is_applied(session):
            pending.append(migration)
    return pending


def get_applied_migrations(session: Session) -> list[str]:
    """Get list of applied migration versions."""
    # Ensure schema_migrations table exists
    _ensure_migrations_table(session)

    results = session.query(SchemaMigration).order_by(SchemaMigration.applied_at).all()
    return [m.version for m in results]


def _ensure_migrations_table(session: Session) -> None:
    """Ensure the schema_migrations table exists."""
    engine = session.get_bind()
    SchemaMigration.__table__.create(engine, checkfirst=True)


def run_migrations(session: Session, dry_run: bool = False) -> None:
    """Run all pending migrations."""
    pending = get_pending_migrations(session)

    if not pending:
        logger.info("No pending migrations")
        return

    logger.info(f"Found {len(pending)} pending migration(s)")

    for migration in pending:
        if dry_run:
            logger.info(f"[DRY RUN] Would apply: {migration.version} - {migration.description}")
        else:
            try:
                migration.up(session)
            except Exception as e:
                logger.error(f"Migration {migration.version} failed: {e}")
                raise
