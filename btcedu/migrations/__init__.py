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

            # On a fresh installation the CLI runs create_all before the
            # migrations, so `channels` already carries every column the model
            # declares -- including content_profile, which migration 011 adds
            # to an older database and which is NOT NULL. Naming only the
            # original columns here made `btcedu migrate` fail on the very
            # first migration of a brand-new install.
            result = session.execute(text("PRAGMA table_info(channels)"))
            channel_columns = [row[1] for row in result.fetchall()]
            profile_column = ", content_profile" if "content_profile" in channel_columns else ""
            profile_value = ", :content_profile" if "content_profile" in channel_columns else ""
            params = {
                "channel_id": "default",
                "name": "Bitcoin Podcast",
                "youtube_channel_id": settings.podcast_youtube_channel_id or None,
                "rss_url": feed_url,
                "now": datetime.now(UTC),
            }
            if profile_column:
                params["content_profile"] = "bitcoin_podcast"
            session.execute(
                text(f"""
                    INSERT INTO channels
                    (channel_id, name, youtube_channel_id, rss_url,
                     is_active, created_at, updated_at{profile_column})
                    VALUES (:channel_id, :name, :youtube_channel_id,
                            :rss_url, 1, :now, :now{profile_value})
                """),
                params,
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


class CreatePublishJobsTableMigration(Migration):
    """Migration 006: Create publish_jobs table for YouTube publishing (Sprint 11)."""

    @property
    def version(self) -> str:
        return "006_create_publish_jobs"

    @property
    def description(self) -> str:
        return "Create publish_jobs table for tracking YouTube upload jobs"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='publish_jobs'")
        )
        if not result.fetchone():
            session.execute(
                text("""
                    CREATE TABLE publish_jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        episode_id VARCHAR(128) NOT NULL,
                        status VARCHAR(32) NOT NULL DEFAULT 'pending',
                        youtube_video_id VARCHAR(64),
                        youtube_url VARCHAR(512),
                        metadata_snapshot TEXT,
                        published_at TIMESTAMP,
                        error_message TEXT,
                        created_at TIMESTAMP NOT NULL
                    )
                """)
            )
            session.execute(
                text("CREATE INDEX idx_publish_jobs_episode ON publish_jobs(episode_id)")
            )
            session.execute(text("CREATE INDEX idx_publish_jobs_status ON publish_jobs(status)"))
            session.commit()
            logger.info("Created publish_jobs table with indexes")
        else:
            logger.info("publish_jobs table already exists (skipped)")

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class AddContentProfileMigration(Migration):
    """Migration 008: Add content_profile column to episodes table."""

    @property
    def version(self) -> str:
        return "008_add_content_profile"

    @property
    def description(self) -> str:
        return "Add content_profile column to episodes table"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(text("PRAGMA table_info(episodes)"))
        columns = [row[1] for row in result.fetchall()]

        if "content_profile" not in columns:
            session.execute(
                text(
                    "ALTER TABLE episodes "
                    "ADD COLUMN content_profile VARCHAR(64) DEFAULT 'bitcoin_podcast' NOT NULL"
                )
            )
            session.commit()
            logger.info("Added content_profile column to episodes")
        else:
            logger.info("content_profile column already exists (skipped)")

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class AddReviewItemDecisionsMigration(Migration):
    """Migration 007: Create review_item_decisions table for granular diff review (Phase 5)."""

    @property
    def version(self) -> str:
        return "007_add_review_item_decisions"

    @property
    def description(self) -> str:
        return "Create review_item_decisions table for per-item diff review actions"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(
            text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='review_item_decisions'"
            )
        )
        if not result.fetchone():
            session.execute(
                text("""
                    CREATE TABLE review_item_decisions (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        review_task_id INTEGER NOT NULL,
                        item_id VARCHAR(64) NOT NULL,
                        operation_type VARCHAR(32) NOT NULL,
                        original_text TEXT,
                        proposed_text TEXT,
                        action VARCHAR(32) NOT NULL DEFAULT 'pending',
                        edited_text TEXT,
                        decided_at TIMESTAMP,
                        FOREIGN KEY (review_task_id) REFERENCES review_tasks(id)
                    )
                """)
            )
            session.execute(
                text(
                    "CREATE INDEX idx_review_item_decisions_task "
                    "ON review_item_decisions(review_task_id)"
                )
            )
            session.execute(
                text(
                    "CREATE INDEX idx_review_item_decisions_task_item "
                    "ON review_item_decisions(review_task_id, item_id)"
                )
            )
            session.commit()
            logger.info("Created review_item_decisions table with indexes")
        else:
            logger.info("review_item_decisions table already exists (skipped)")

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class CreateDeadLetterQueueMigration(Migration):
    """Migration 009: Create dead_letter_queue table for permanent pipeline failures."""

    @property
    def version(self) -> str:
        return "009_create_dead_letter_queue"

    @property
    def description(self) -> str:
        return "Create dead_letter_queue table for permanent pipeline failure tracking"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' AND name='dead_letter_queue'")
        )
        if not result.fetchone():
            session.execute(
                text("""
                    CREATE TABLE dead_letter_queue (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        episode_id VARCHAR(64) NOT NULL,
                        stage VARCHAR(32) NOT NULL,
                        error_category VARCHAR(32) NOT NULL,
                        error_message TEXT NOT NULL,
                        suggestion TEXT NOT NULL DEFAULT '',
                        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        resolved_at TIMESTAMP,
                        resolved_by VARCHAR(32),
                        retry_count INTEGER NOT NULL DEFAULT 0
                    )
                """)
            )
            session.execute(
                text("CREATE INDEX idx_dlq_episode_id ON dead_letter_queue(episode_id)")
            )
            session.execute(text("CREATE INDEX idx_dlq_resolved ON dead_letter_queue(resolved_at)"))
            session.commit()
            logger.info("Created dead_letter_queue table with indexes")
        else:
            logger.info("dead_letter_queue table already exists (skipped)")

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class AddChannelContentProfileMigration(Migration):
    """Migration 011: Add content_profile column to channels table."""

    @property
    def version(self) -> str:
        return "011_add_channel_content_profile"

    @property
    def description(self) -> str:
        return "Add content_profile column to channels and rename default channel"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(text("PRAGMA table_info(channels)"))
        columns = [row[1] for row in result.fetchall()]

        if "content_profile" not in columns:
            session.execute(
                text(
                    "ALTER TABLE channels "
                    "ADD COLUMN content_profile VARCHAR(64) "
                    "DEFAULT 'bitcoin_podcast' NOT NULL"
                )
            )
            session.commit()
            logger.info("Added content_profile column to channels")
        else:
            logger.info("content_profile column already exists (skipped)")

        # Backfill profile for existing channels based on name heuristic
        session.execute(
            text(
                "UPDATE channels SET content_profile = 'tagesschau_tr' "
                "WHERE LOWER(name) LIKE '%tagesschau%' "
                "AND content_profile != 'tagesschau_tr'"
            )
        )
        session.commit()

        # Rename the legacy "Default Channel" to "Bitcoin Podcast"
        session.execute(
            text(
                "UPDATE channels SET name = 'Bitcoin Podcast' "
                "WHERE channel_id = 'default' AND name = 'Default Channel'"
            )
        )
        session.commit()
        logger.info("Renamed default channel to 'Bitcoin Podcast' (if present)")

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class AddQualityRatingMigration(Migration):
    """Migration 010: Add quality_rating column to review_decisions."""

    @property
    def version(self) -> str:
        return "010_add_quality_rating"

    @property
    def description(self) -> str:
        return "Add quality_rating column to review_decisions for user feedback"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        # Check if column already exists
        result = session.execute(text("PRAGMA table_info(review_decisions)"))
        columns = [row[1] for row in result.fetchall()]

        if "quality_rating" not in columns:
            session.execute(text("ALTER TABLE review_decisions ADD COLUMN quality_rating INTEGER"))
            session.commit()
            logger.info("Added quality_rating column to review_decisions")
        else:
            logger.info("quality_rating column already exists (skipped)")

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class AddPipelineRunGitCommitMigration(Migration):
    """Migration 012: Add git_commit column to pipeline_runs for traceability."""

    @property
    def version(self) -> str:
        return "012_add_pipeline_run_git_commit"

    @property
    def description(self) -> str:
        return "Add git_commit column to pipeline_runs so each stage run is traceable to a commit"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(text("PRAGMA table_info(pipeline_runs)"))
        columns = [row[1] for row in result.fetchall()]

        if "git_commit" not in columns:
            session.execute(text("ALTER TABLE pipeline_runs ADD COLUMN git_commit VARCHAR(40)"))
            session.commit()
            logger.info("Added git_commit column to pipeline_runs")
        else:
            logger.info("git_commit column already exists (skipped)")

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class DropV1ChunksTableMigration(Migration):
    """Migration 013: Drop legacy v1 chunks + chunks_fts tables.

    The v1 pipeline (chunk/generate/refine) has been fully removed. The
    ``chunks`` table and its ``chunks_fts`` FTS5 mirror are no longer used by
    any code path, so drop them to keep the schema clean.
    """

    @property
    def version(self) -> str:
        return "013_drop_v1_chunks_table"

    @property
    def description(self) -> str:
        return "Drop legacy v1 chunks and chunks_fts tables (v1 pipeline removed)"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(
            text("SELECT name FROM sqlite_master WHERE type IN ('table','view')")
        )
        existing = {row[0] for row in result.fetchall()}

        # Drop FTS mirror first (it references the chunks content table)
        for tbl in ("chunks_fts", "chunks"):
            if tbl in existing:
                session.execute(text(f"DROP TABLE IF EXISTS {tbl}"))
                logger.info("Dropped table %s", tbl)
            else:
                logger.info("Table %s does not exist (skipped)", tbl)
        session.commit()

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class CreateAppSettingsTableMigration(Migration):
    """Migration 014: Create app_settings table for operator-editable runtime values.

    ``Settings`` is loaded from ``.env`` at process start, so the dashboard
    cannot change it while the pipeline runs. This table stores the few values
    that must be switchable at runtime (currently the render execution mode).
    """

    @property
    def version(self) -> str:
        return "014_create_app_settings_table"

    @property
    def description(self) -> str:
        return "Create app_settings table for runtime-editable settings (e.g. render mode)"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        existing = {row[0] for row in result.fetchall()}

        if "app_settings" not in existing:
            session.execute(
                text(
                    """
                    CREATE TABLE app_settings (
                        key VARCHAR(64) PRIMARY KEY,
                        value TEXT NOT NULL DEFAULT '',
                        updated_at DATETIME NOT NULL
                    )
                    """
                )
            )
            session.commit()
            logger.info("Created app_settings table")
        else:
            logger.info("app_settings table already exists (skipped)")

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class CreatePresenterAssignmentTableMigration(Migration):
    """Migration 015: Create presenter_assignments table for ALMANYA24 outfits.

    One row per episode, enforced by a unique index. That constraint is the
    mechanism that keeps a retry or a reboot from re-rolling the presenter's
    outfit mid-episode, so it is created with the table rather than left to
    application code. The index on (avatar_look_id, assigned_at) serves the
    rotation query, which looks for the look that has gone unused longest.
    """

    @property
    def version(self) -> str:
        return "015_create_presenter_assignments_table"

    @property
    def description(self) -> str:
        return "Create presenter_assignments table for per-episode avatar look assignment"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        existing = {row[0] for row in result.fetchall()}

        if "presenter_assignments" not in existing:
            session.execute(
                text(
                    """
                    CREATE TABLE presenter_assignments (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        episode_id VARCHAR(64) NOT NULL,
                        provider VARCHAR(32) NOT NULL,
                        engine VARCHAR(32) NOT NULL,
                        avatar_type VARCHAR(32) NOT NULL,
                        avatar_look_id VARCHAR(128) NOT NULL,
                        look_name VARCHAR(64) NOT NULL,
                        strategy VARCHAR(32) NOT NULL,
                        config_version INTEGER NOT NULL DEFAULT 1,
                        status VARCHAR(32) NOT NULL DEFAULT 'assigned',
                        content_hash VARCHAR(64) NOT NULL DEFAULT '',
                        provenance_path VARCHAR(500),
                        assigned_at DATETIME NOT NULL,
                        superseded_at DATETIME,
                        cost_per_second_usd FLOAT NOT NULL DEFAULT 0.0
                    )
                    """
                )
            )
            session.commit()
            logger.info("Created presenter_assignments table")
        else:
            logger.info("presenter_assignments table already exists (skipped)")

        result = session.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))
        indexes = {row[0] for row in result.fetchall()}

        if "uq_presenter_assignment_episode" not in indexes:
            session.execute(
                text(
                    "CREATE UNIQUE INDEX uq_presenter_assignment_episode "
                    "ON presenter_assignments (episode_id)"
                )
            )
            session.commit()
            logger.info("Created unique index on presenter_assignments.episode_id")

        if "ix_presenter_assignments_rotation" not in indexes:
            session.execute(
                text(
                    "CREATE INDEX ix_presenter_assignments_rotation "
                    "ON presenter_assignments (avatar_look_id, assigned_at)"
                )
            )
            session.commit()
            logger.info("Created rotation index on presenter_assignments")

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class CreateAvatarJobsTableMigration(Migration):
    """Migration 016: Create avatar_jobs table for restart-safe HeyGen billing.

    The unique index on (episode_id, scene_id, content_hash) is what makes a
    second purchase of the same clip impossible rather than merely unlikely: a
    reboot mid-stage finds the existing row instead of inserting a new one. The
    status index serves the "is anything awaiting reconciliation?" query the
    anchor stage runs before it declares an episode finished.
    """

    @property
    def version(self) -> str:
        return "016_create_avatar_jobs_table"

    @property
    def description(self) -> str:
        return "Create avatar_jobs table for restart-safe avatar generation"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        existing = {row[0] for row in result.fetchall()}

        if "avatar_jobs" not in existing:
            session.execute(
                text(
                    """
                    CREATE TABLE avatar_jobs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        episode_id VARCHAR(64) NOT NULL,
                        scene_id VARCHAR(128) NOT NULL,
                        chapter_id VARCHAR(128) NOT NULL DEFAULT '',
                        content_hash VARCHAR(64) NOT NULL,
                        provider VARCHAR(32) NOT NULL,
                        engine VARCHAR(32) NOT NULL DEFAULT '',
                        avatar_look_id VARCHAR(128) NOT NULL DEFAULT '',
                        output_format VARCHAR(16) NOT NULL DEFAULT 'mp4',
                        status VARCHAR(32) NOT NULL DEFAULT 'reserved',
                        provider_job_id VARCHAR(128),
                        output_path VARCHAR(500),
                        duration_seconds FLOAT NOT NULL DEFAULT 0.0,
                        cost_usd FLOAT NOT NULL DEFAULT 0.0,
                        attempt_count INTEGER NOT NULL DEFAULT 0,
                        reserved_at DATETIME NOT NULL,
                        submitted_at DATETIME,
                        completed_at DATETIME,
                        error_message TEXT,
                        resolution_note TEXT
                    )
                    """
                )
            )
            session.commit()
            logger.info("Created avatar_jobs table")
        else:
            logger.info("avatar_jobs table already exists (skipped)")

        result = session.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))
        indexes = {row[0] for row in result.fetchall()}

        if "uq_avatar_job_scene_content" not in indexes:
            session.execute(
                text(
                    "CREATE UNIQUE INDEX uq_avatar_job_scene_content "
                    "ON avatar_jobs (episode_id, scene_id, content_hash)"
                )
            )
            session.commit()
            logger.info("Created unique index on avatar_jobs identity")

        if "ix_avatar_jobs_episode_id" not in indexes:
            session.execute(
                text("CREATE INDEX ix_avatar_jobs_episode_id ON avatar_jobs (episode_id)")
            )
            session.commit()

        if "ix_avatar_jobs_status" not in indexes:
            session.execute(text("CREATE INDEX ix_avatar_jobs_status ON avatar_jobs (status)"))
            session.commit()

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class CreateAvatarJobAuditTableMigration(Migration):
    """Migration 017: Create avatar_job_audit table for operator decisions.

    Reconciliation is where a human overrules the ledger, so the reason has to
    outlive the shell session it was typed in. Nothing here is on the hot path;
    the table exists to answer "why does this row say completed" months later.
    """

    @property
    def version(self) -> str:
        return "017_create_avatar_job_audit_table"

    @property
    def description(self) -> str:
        return "Create avatar_job_audit table for avatar reconciliation decisions"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        existing = {row[0] for row in result.fetchall()}

        if "avatar_job_audit" not in existing:
            session.execute(
                text(
                    """
                    CREATE TABLE avatar_job_audit (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        episode_id VARCHAR(64) NOT NULL,
                        job_id INTEGER,
                        scene_id VARCHAR(128) NOT NULL DEFAULT '',
                        action VARCHAR(32) NOT NULL,
                        from_status VARCHAR(32) NOT NULL DEFAULT '',
                        to_status VARCHAR(32) NOT NULL DEFAULT '',
                        provider_job_id VARCHAR(128),
                        operator_ref VARCHAR(64) NOT NULL DEFAULT '',
                        note TEXT NOT NULL DEFAULT '',
                        cost_before_usd FLOAT NOT NULL DEFAULT 0.0,
                        cost_after_usd FLOAT NOT NULL DEFAULT 0.0,
                        created_at DATETIME NOT NULL
                    )
                    """
                )
            )
            session.commit()
            logger.info("Created avatar_job_audit table")
        else:
            logger.info("avatar_job_audit table already exists (skipped)")

        result = session.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))
        indexes = {row[0] for row in result.fetchall()}

        if "ix_avatar_job_audit_episode_id" not in indexes:
            session.execute(
                text(
                    "CREATE INDEX ix_avatar_job_audit_episode_id "
                    "ON avatar_job_audit (episode_id)"
                )
            )
            session.commit()

        if "ix_avatar_job_audit_job_id" not in indexes:
            session.execute(
                text("CREATE INDEX ix_avatar_job_audit_job_id ON avatar_job_audit (job_id)")
            )
            session.commit()

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class CreateAvatarRegenerationTableMigration(Migration):
    """Migration 018: Create avatar_regeneration_requests table.

    A deliberate re-purchase of a presenter clip needs a record that exists
    before the work does, and a unique constraint that turns a double click
    into one row rather than two invoices.
    """

    @property
    def version(self) -> str:
        return "018_create_avatar_regeneration_requests"

    @property
    def description(self) -> str:
        return "Create avatar_regeneration_requests table for deliberate clip re-purchases"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        existing = {row[0] for row in result.fetchall()}

        if "avatar_regeneration_requests" not in existing:
            session.execute(
                text(
                    """
                    CREATE TABLE avatar_regeneration_requests (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        episode_id VARCHAR(64) NOT NULL,
                        scene_id VARCHAR(128) NOT NULL,
                        revision INTEGER NOT NULL DEFAULT 1,
                        status VARCHAR(32) NOT NULL DEFAULT 'requested',
                        reason TEXT NOT NULL DEFAULT '',
                        requested_by_ref VARCHAR(64) NOT NULL DEFAULT '',
                        confirmed_by_ref VARCHAR(64) NOT NULL DEFAULT '',
                        previous_job_id INTEGER,
                        previous_cost_usd FLOAT NOT NULL DEFAULT 0.0,
                        estimated_cost_usd FLOAT NOT NULL DEFAULT 0.0,
                        idempotency_key VARCHAR(128) NOT NULL,
                        created_at DATETIME NOT NULL,
                        confirmed_at DATETIME,
                        consumed_at DATETIME,
                        CONSTRAINT uq_avatar_regeneration_scene_revision
                            UNIQUE (episode_id, scene_id, revision),
                        CONSTRAINT uq_avatar_regeneration_idempotency
                            UNIQUE (idempotency_key)
                    )
                    """
                )
            )
            session.commit()
            logger.info("Created avatar_regeneration_requests table")
        else:
            logger.info("avatar_regeneration_requests table already exists (skipped)")

        result = session.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))
        indexes = {row[0] for row in result.fetchall()}

        if "ix_avatar_regeneration_episode_id" not in indexes:
            session.execute(
                text(
                    "CREATE INDEX ix_avatar_regeneration_episode_id "
                    "ON avatar_regeneration_requests (episode_id)"
                )
            )
            session.commit()

        if "ix_avatar_regeneration_status" not in indexes:
            session.execute(
                text(
                    "CREATE INDEX ix_avatar_regeneration_status "
                    "ON avatar_regeneration_requests (status)"
                )
            )
            session.commit()

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class AddAvatarConcurrencyStateMigration(Migration):
    """Migration 019: Persist idempotency, retry telemetry, assets and breaker.

    Bounded parallelism only helps if the state that makes it safe survives the
    process. Three things move from memory into the database here: the
    idempotency key a create call used (with its expiry), what the last attempt
    ran into, and whether the downloaded file was ever validated. Two small
    tables join them — one so a re-run reuses an uploaded narration instead of
    pushing it again, one so a provider that keeps failing stops being asked.
    """

    @property
    def version(self) -> str:
        return "019_avatar_concurrency_state"

    @property
    def description(self) -> str:
        return "Add avatar idempotency/retry columns plus audio asset and breaker tables"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        existing = {row[0] for row in result.fetchall()}

        if "avatar_jobs" in existing:
            result = session.execute(text("PRAGMA table_info(avatar_jobs)"))
            columns = {row[1] for row in result.fetchall()}
            new_columns = [
                ("idempotency_key", "VARCHAR(128)"),
                ("idempotency_expires_at", "DATETIME"),
                ("audio_asset_id", "VARCHAR(128)"),
                ("audio_hash", "VARCHAR(64)"),
                ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
                ("last_error_type", "VARCHAR(32)"),
                ("last_status_code", "INTEGER"),
                ("retry_after_seconds", "FLOAT"),
                ("next_poll_at", "DATETIME"),
                ("validation_status", "VARCHAR(32) NOT NULL DEFAULT 'pending'"),
                ("validation_error", "TEXT"),
            ]
            for name, ddl in new_columns:
                if name not in columns:
                    session.execute(text(f"ALTER TABLE avatar_jobs ADD COLUMN {name} {ddl}"))
                    session.commit()
                    logger.info(f"Added avatar_jobs.{name}")

            # Existing completed rows were downloaded before validation existed.
            # Calling them 'valid' would be a claim nobody checked; they are
            # marked 'legacy' so the dashboard can say so honestly.
            session.execute(
                text(
                    "UPDATE avatar_jobs SET validation_status = 'legacy' "
                    "WHERE status = 'completed' AND validation_status = 'pending'"
                )
            )
            session.commit()

        if "avatar_audio_assets" not in existing:
            session.execute(
                text(
                    """
                    CREATE TABLE avatar_audio_assets (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        provider VARCHAR(32) NOT NULL,
                        audio_hash VARCHAR(64) NOT NULL,
                        asset_id VARCHAR(128) NOT NULL,
                        episode_id VARCHAR(64) NOT NULL DEFAULT '',
                        scene_id VARCHAR(128) NOT NULL DEFAULT '',
                        size_bytes INTEGER NOT NULL DEFAULT 0,
                        status VARCHAR(32) NOT NULL DEFAULT 'active',
                        expired_reason TEXT,
                        created_at DATETIME NOT NULL,
                        last_used_at DATETIME,
                        CONSTRAINT uq_avatar_audio_asset_identity
                            UNIQUE (provider, audio_hash)
                    )
                    """
                )
            )
            session.commit()
            logger.info("Created avatar_audio_assets table")

        if "avatar_provider_breakers" not in existing:
            session.execute(
                text(
                    """
                    CREATE TABLE avatar_provider_breakers (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        provider VARCHAR(32) NOT NULL UNIQUE,
                        state VARCHAR(16) NOT NULL DEFAULT 'closed',
                        consecutive_failures INTEGER NOT NULL DEFAULT 0,
                        last_failure_class VARCHAR(32),
                        last_status_code INTEGER,
                        reason TEXT,
                        opened_at DATETIME,
                        cooldown_until DATETIME,
                        last_failure_at DATETIME,
                        last_success_at DATETIME,
                        reset_by_ref VARCHAR(64),
                        reset_note TEXT,
                        reset_at DATETIME,
                        updated_at DATETIME NOT NULL
                    )
                    """
                )
            )
            session.commit()
            logger.info("Created avatar_provider_breakers table")

        result = session.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))
        indexes = {row[0] for row in result.fetchall()}
        wanted = {
            "ix_avatar_audio_assets_audio_hash": (
                "CREATE INDEX ix_avatar_audio_assets_audio_hash "
                "ON avatar_audio_assets (audio_hash)"
            ),
            "ix_avatar_audio_assets_episode_id": (
                "CREATE INDEX ix_avatar_audio_assets_episode_id "
                "ON avatar_audio_assets (episode_id)"
            ),
            "ix_avatar_audio_assets_status": (
                "CREATE INDEX ix_avatar_audio_assets_status ON avatar_audio_assets (status)"
            ),
            "ix_avatar_provider_breakers_provider": (
                "CREATE INDEX ix_avatar_provider_breakers_provider "
                "ON avatar_provider_breakers (provider)"
            ),
        }
        for name, ddl in wanted.items():
            if name not in indexes:
                session.execute(text(ddl))
                session.commit()

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


# Registry of all available migrations
class AddAvatarClipHashMigration(Migration):
    """Migration 020: Record the bytes of every presenter clip.

    Until now an approval was bound to the manifest, and the manifest to a
    path. Anything that rewrote the file underneath kept the approval alive.
    The column added here holds the digest measured during download, so the
    review, the render and the remote runner can each ask whether the clip in
    front of them is the clip a person approved.

    Rows that predate the column stay NULL rather than being back-filled: a
    hash computed today would only prove that the file has not changed since
    today, which is precisely the claim that must not be made. They surface as
    `unrecorded` and need one regeneration of the digest and a fresh approval.
    """

    @property
    def version(self) -> str:
        return "020_avatar_clip_hash"

    @property
    def description(self) -> str:
        return "Add avatar_jobs.file_sha256 for byte-level clip provenance"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        if "avatar_jobs" not in {row[0] for row in result.fetchall()}:
            logger.info("avatar_jobs table absent, nothing to migrate")
            self.mark_applied(session)
            return

        result = session.execute(text("PRAGMA table_info(avatar_jobs)"))
        columns = {row[1] for row in result.fetchall()}
        if "file_sha256" not in columns:
            session.execute(text("ALTER TABLE avatar_jobs ADD COLUMN file_sha256 VARCHAR(64)"))
            session.commit()
            logger.info("Added avatar_jobs.file_sha256")

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class RepairEpisodeChannelByProfileMigration(Migration):
    """Migration 021: File each episode under the channel its profile names.

    Episodes ingested from the local recorder had their channel resolved from
    the *global* podcast settings, which name exactly one channel. Every
    tagesschau broadcast taken off disk was therefore filed under the Bitcoin
    podcast, and the dashboard's channel filter -- which is correct, and
    filters on `channel_id` -- showed one of ten.

    The repair is deliberately narrow. A row is only moved when its
    `content_profile` names a channel *and* the channel it currently sits on
    declares a different profile. An episode whose channel already declares the
    same profile is left exactly where it is -- two channels may serve one
    profile, and that assignment carries information this migration does not
    have. Nothing is deleted and no other column is touched.
    """

    @property
    def version(self) -> str:
        return "021_repair_episode_channel_by_profile"

    @property
    def description(self) -> str:
        return "Move episodes onto the channel matching their content profile"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")

        result = session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        tables = {row[0] for row in result.fetchall()}
        if not {"episodes", "channels"} <= tables:
            logger.info("episodes/channels table absent, nothing to migrate")
            self.mark_applied(session)
            return

        episode_columns = {
            row[1] for row in session.execute(text("PRAGMA table_info(episodes)")).fetchall()
        }
        channel_columns = {
            row[1] for row in session.execute(text("PRAGMA table_info(channels)")).fetchall()
        }
        if "content_profile" not in episode_columns or "content_profile" not in channel_columns:
            logger.info("content_profile column absent, nothing to migrate")
            self.mark_applied(session)
            return

        # Only the lowest channel id per profile, so a duplicate profile cannot
        # make the target ambiguous.
        result = session.execute(
            text(
                """
                UPDATE episodes
                SET channel_id = (
                    SELECT c.channel_id FROM channels c
                    WHERE c.content_profile = episodes.content_profile
                    ORDER BY c.id LIMIT 1
                )
                WHERE episodes.content_profile IS NOT NULL
                  AND EXISTS (
                      SELECT 1 FROM channels c
                      WHERE c.content_profile = episodes.content_profile
                  )
                  AND EXISTS (
                      SELECT 1 FROM channels cur
                      WHERE cur.channel_id = episodes.channel_id
                        AND cur.content_profile IS NOT NULL
                        AND cur.content_profile != episodes.content_profile
                  )
                """
            )
        )
        session.commit()
        moved = result.rowcount if result.rowcount is not None else 0
        if moved:
            logger.info("Moved %d episode(s) onto the channel matching their profile", moved)

        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class CreateNewsroomCoreTablesMigration(Migration):
    """Migration 022: Create the immutable editorial core and provider ledger."""

    @property
    def version(self) -> str:
        return "022_create_newsroom_core_tables"

    @property
    def description(self) -> str:
        return "Create newsroom source, claim, topic and research ledger tables"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")
        from btcedu.models.editorial import (
            Claim,
            ClaimOrigin,
            ClaimRevision,
            EditorialRevision,
            ProviderOperation,
            ResearchRun,
            RevisionClaim,
            SourceItem,
            SourceRevision,
            SourceSpan,
            Topic,
            TopicSource,
        )

        tables = (
            SourceItem,
            SourceRevision,
            SourceSpan,
            Topic,
            TopicSource,
            Claim,
            ClaimRevision,
            ClaimOrigin,
            ResearchRun,
            ProviderOperation,
            EditorialRevision,
            RevisionClaim,
        )
        bind = session.get_bind()
        for model in tables:
            model.__table__.create(bind, checkfirst=True)
        session.commit()
        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class CreateNewsroomEvidenceTablesMigration(Migration):
    """Migration 023: Create durable search, observation and evidence tables."""

    @property
    def version(self) -> str:
        return "023_create_newsroom_evidence_tables"

    @property
    def description(self) -> str:
        return "Create newsroom query, source observation and claim evidence tables"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")
        from btcedu.models.editorial import (
            ClaimAssessment,
            EvidenceLink,
            ResearchQuery,
            SourceObservation,
        )

        bind = session.get_bind()
        for model in (ResearchQuery, SourceObservation, EvidenceLink, ClaimAssessment):
            model.__table__.create(bind, checkfirst=True)
        session.commit()
        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class CreateNewsroomMediaRightsTablesMigration(Migration):
    """Migration 024: Create durable media asset, licence and use decision tables."""

    @property
    def version(self) -> str:
        return "024_create_newsroom_media_rights_tables"

    @property
    def description(self) -> str:
        return "Create newsroom media asset, source offer, licence and use decision tables"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")
        from btcedu.models.media_rights import (
            LicenseEvidence,
            MediaSourceOffer,
            MediaUseDecision,
            NewsroomMediaAsset,
            RevisionMedia,
        )

        bind = session.get_bind()
        for model in (
            NewsroomMediaAsset,
            MediaSourceOffer,
            LicenseEvidence,
            MediaUseDecision,
            RevisionMedia,
        ):
            model.__table__.create(bind, checkfirst=True)
        session.commit()
        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class CreateNewsroomArticleTablesMigration(Migration):
    """Migration 025: Create article revisions, paragraphs and operator decisions."""

    @property
    def version(self) -> str:
        return "025_create_newsroom_article_tables"

    @property
    def description(self) -> str:
        return "Create newsroom article revision, paragraph and editorial decision tables"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")
        from btcedu.models.article import (
            ArticleParagraph,
            ArticleParagraphClaim,
            ArticleRevision,
            EditorialDecision,
        )

        bind = session.get_bind()
        for model in (
            ArticleRevision,
            ArticleParagraph,
            ArticleParagraphClaim,
            EditorialDecision,
        ):
            model.__table__.create(bind, checkfirst=True)
        session.commit()
        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class CreateNewsroomPublicationTablesMigration(Migration):
    """Migration 026: Create public identity, release and correction tables."""

    @property
    def version(self) -> str:
        return "026_create_newsroom_publication_tables"

    @property
    def description(self) -> str:
        return "Create newsroom publication, version, correction notice and release tables"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")
        from btcedu.models.publication import (
            CorrectionNotice,
            Publication,
            PublicationVersion,
            SiteRelease,
        )

        bind = session.get_bind()
        for model in (Publication, PublicationVersion, CorrectionNotice, SiteRelease):
            model.__table__.create(bind, checkfirst=True)
        session.commit()
        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class CreateNewsroomTopicGraphTablesMigration(Migration):
    """Migration 027: Create topic aliases, merges, dependencies and recheck jobs."""

    @property
    def version(self) -> str:
        return "027_create_newsroom_topic_graph_tables"

    @property
    def description(self) -> str:
        return "Create newsroom topic alias, merge, proposal, dependency, recheck and issue tables"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")
        from btcedu.models.topic_graph import (
            PublicationDependency,
            RecheckJob,
            SourceIssue,
            TopicAlias,
            TopicMerge,
            UpdateProposal,
        )

        bind = session.get_bind()
        for model in (
            TopicAlias,
            TopicMerge,
            UpdateProposal,
            PublicationDependency,
            RecheckJob,
            SourceIssue,
        ):
            model.__table__.create(bind, checkfirst=True)
        session.commit()
        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


class CreateNewsroomVideoEditionTablesMigration(Migration):
    """Migration 028: Create video edition, segment, media and decision tables."""

    @property
    def version(self) -> str:
        return "028_create_newsroom_video_edition_tables"

    @property
    def description(self) -> str:
        return "Create newsroom video edition, segment, claim, media and decision tables"

    def up(self, session: Session) -> None:
        logger.info(f"Running migration: {self.version}")
        from btcedu.models.video_edition import (
            EditionDecision,
            EditionMedia,
            EditionSegment,
            EditionSegmentClaim,
            VideoEdition,
        )

        bind = session.get_bind()
        for model in (
            VideoEdition,
            EditionSegment,
            EditionSegmentClaim,
            EditionMedia,
            EditionDecision,
        ):
            model.__table__.create(bind, checkfirst=True)
        session.commit()
        self.mark_applied(session)
        logger.info(f"Migration {self.version} completed successfully")


MIGRATIONS = [
    AddChannelsSupportMigration(),
    AddV2PipelineColumnsMigration(),
    CreatePromptVersionsTableMigration(),
    CreateReviewTablesMigration(),
    CreateMediaAssetsTableMigration(),
    CreatePublishJobsTableMigration(),
    AddReviewItemDecisionsMigration(),
    AddContentProfileMigration(),
    CreateDeadLetterQueueMigration(),
    AddQualityRatingMigration(),
    AddChannelContentProfileMigration(),
    AddPipelineRunGitCommitMigration(),
    DropV1ChunksTableMigration(),
    CreateAppSettingsTableMigration(),
    CreatePresenterAssignmentTableMigration(),
    CreateAvatarJobsTableMigration(),
    CreateAvatarJobAuditTableMigration(),
    CreateAvatarRegenerationTableMigration(),
    AddAvatarConcurrencyStateMigration(),
    AddAvatarClipHashMigration(),
    RepairEpisodeChannelByProfileMigration(),
    CreateNewsroomCoreTablesMigration(),
    CreateNewsroomEvidenceTablesMigration(),
    CreateNewsroomMediaRightsTablesMigration(),
    CreateNewsroomArticleTablesMigration(),
    CreateNewsroomPublicationTablesMigration(),
    CreateNewsroomTopicGraphTablesMigration(),
    CreateNewsroomVideoEditionTablesMigration(),
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
