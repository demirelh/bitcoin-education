from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from btcedu.db import Base
from btcedu.migrations import (
    MIGRATIONS,
    CreateNewsroomCoreTablesMigration,
    get_pending_migrations,
    run_migrations,
)
from btcedu.models import editorial  # noqa: F401
from btcedu.models.media_asset import Base as MediaBase
from btcedu.models.migration import SchemaMigration

NEWSROOM_TABLES = {
    "news_source_items",
    "news_source_revisions",
    "news_source_spans",
    "news_topics",
    "news_topic_sources",
    "news_claims",
    "news_claim_revisions",
    "news_claim_origins",
    "news_research_runs",
    "news_provider_operations",
    "news_editorial_revisions",
    "news_revision_claims",
}


def test_fresh_database_contains_newsroom_tables(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()

    run_migrations(session)

    assert NEWSROOM_TABLES <= set(inspect(engine).get_table_names())
    assert get_pending_migrations(session) == []


def test_existing_database_adds_newsroom_tables_without_touching_episode(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'upgrade.db'}")
    Base.metadata.create_all(engine)
    MediaBase.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    for table in NEWSROOM_TABLES:
        session.execute(text(f"DROP TABLE IF EXISTS {table}"))
    target = CreateNewsroomCoreTablesMigration().version
    for migration in MIGRATIONS:
        if migration.version != target:
            session.add(SchemaMigration(version=migration.version))
    session.execute(
        text(
            """
            INSERT INTO episodes
                (episode_id, source, title, url, status, detected_at, retry_count,
                 pipeline_version, content_profile)
            VALUES
                ('kept', 'youtube_rss', 'Kept', 'https://example.invalid/kept',
                 'new', CURRENT_TIMESTAMP, 0, 2, 'tagesschau_tr')
            """
        )
    )
    session.commit()

    run_migrations(session)
    run_migrations(session)

    assert NEWSROOM_TABLES <= set(inspect(engine).get_table_names())
    assert "media_assets" in inspect(engine).get_table_names()
    kept = session.execute(
        text("SELECT COUNT(*) FROM episodes WHERE episode_id='kept'")
    ).scalar()
    assert kept == 1
    assert get_pending_migrations(session) == []
