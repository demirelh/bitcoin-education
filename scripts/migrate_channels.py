"""Database migration helper for adding Channel support.

This script adds the channel_id column to the episodes table
and creates the channels table.

Run this after updating to the new version with multi-channel support.
"""

import logging
from sqlalchemy import text

from btcedu.config import get_settings
from btcedu.db import get_session_factory

logger = logging.getLogger(__name__)


def migrate_database():
    """Add channel support to existing database."""
    settings = get_settings()
    session_factory = get_session_factory(settings.database_url)
    session = session_factory()

    try:
        # Check if channels table exists
        result = session.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='channels'"
            )
        )
        if not result.fetchone():
            logger.info("Creating channels table...")
            session.execute(
                text(
                    """
                CREATE TABLE channels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    channel_id VARCHAR(64) UNIQUE NOT NULL,
                    name VARCHAR(200) NOT NULL,
                    youtube_channel_id VARCHAR(64),
                    rss_url VARCHAR(500),
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
                )
            )
            session.commit()
            logger.info("✓ Created channels table")
        else:
            logger.info("✓ Channels table already exists")

        # Check if episodes.channel_id column exists
        result = session.execute(text("PRAGMA table_info(episodes)"))
        columns = [row[1] for row in result.fetchall()]

        if "channel_id" not in columns:
            logger.info("Adding channel_id column to episodes table...")
            session.execute(
                text("ALTER TABLE episodes ADD COLUMN channel_id VARCHAR(64)")
            )
            session.execute(text("CREATE INDEX idx_episodes_channel_id ON episodes(channel_id)"))
            session.commit()
            logger.info("✓ Added channel_id column to episodes")
        else:
            logger.info("✓ Episodes.channel_id column already exists")

        logger.info("Database migration completed successfully!")

    except Exception as e:
        session.rollback()
        logger.error(f"Migration failed: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    migrate_database()
