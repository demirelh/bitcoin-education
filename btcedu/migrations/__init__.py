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


# Registry of all available migrations
MIGRATIONS = [
    AddChannelsSupportMigration(),
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
