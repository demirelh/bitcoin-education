from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from btcedu.db import Base
from btcedu.migrations import (
    MIGRATIONS,
    CreateNewsroomArticleTablesMigration,
    CreateNewsroomCoreTablesMigration,
    CreateNewsroomEvidenceTablesMigration,
    CreateNewsroomMediaRightsTablesMigration,
    CreateNewsroomPublicationTablesMigration,
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
    "news_research_queries",
    "news_source_observations",
    "news_evidence_links",
    "news_claim_assessments",
    "news_media_assets",
    "news_media_source_offers",
    "news_license_evidence",
    "news_media_use_decisions",
    "news_revision_media",
    "news_article_revisions",
    "news_article_paragraphs",
    "news_article_paragraph_claims",
    "news_editorial_decisions",
    "news_publications",
    "news_publication_versions",
    "news_correction_notices",
    "news_site_releases",
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
    targets = {
        CreateNewsroomCoreTablesMigration().version,
        CreateNewsroomEvidenceTablesMigration().version,
        CreateNewsroomMediaRightsTablesMigration().version,
        CreateNewsroomArticleTablesMigration().version,
        CreateNewsroomPublicationTablesMigration().version,
    }
    for migration in MIGRATIONS:
        if migration.version not in targets:
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


def test_n1_database_can_upgrade_only_the_evidence_tables(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'n1-upgrade.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    evidence_tables = {
        "news_research_queries",
        "news_source_observations",
        "news_evidence_links",
        "news_claim_assessments",
    }
    for table in evidence_tables:
        session.execute(text(f"DROP TABLE IF EXISTS {table}"))
    target = CreateNewsroomEvidenceTablesMigration().version
    for migration in MIGRATIONS:
        if migration.version != target:
            session.add(SchemaMigration(version=migration.version))
    session.commit()

    run_migrations(session)

    assert evidence_tables <= set(inspect(engine).get_table_names())
    assert get_pending_migrations(session) == []


def test_media_rights_tables_do_not_adopt_existing_pipeline_assets(tmp_path):
    """The rights ledger starts empty even where the pipeline already has images.

    Existing Pexels or frame assets were never rights-checked, so migrating
    them in would present unverified pictures as cleared for publication.
    """
    from btcedu.models.media_asset import Base as MediaBase

    engine = create_engine(f"sqlite:///{tmp_path / 'media-upgrade.db'}")
    Base.metadata.create_all(engine)
    MediaBase.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.execute(
        text(
            "INSERT INTO media_assets "
            "(episode_id, asset_type, file_path, mime_type, size_bytes, created_at) "
            "VALUES ('episode-1', 'IMAGE', 'outputs/e1/images/c1.png', "
            "'image/png', 10, '2026-09-09 00:00:00')"
        )
    )
    media_tables = {
        "news_media_assets",
        "news_media_source_offers",
        "news_license_evidence",
        "news_media_use_decisions",
        "news_revision_media",
    }
    for table in media_tables:
        session.execute(text(f"DROP TABLE IF EXISTS {table}"))
    target = CreateNewsroomMediaRightsTablesMigration().version
    for migration in MIGRATIONS:
        if migration.version != target:
            session.add(SchemaMigration(version=migration.version))
    session.commit()

    run_migrations(session)

    assert media_tables <= set(inspect(engine).get_table_names())
    assert session.execute(text("SELECT COUNT(*) FROM news_media_assets")).scalar() == 0
    assert session.execute(text("SELECT COUNT(*) FROM media_assets")).scalar() == 1
    assert get_pending_migrations(session) == []


ARTICLE_TABLES = {
    "news_article_revisions",
    "news_article_paragraphs",
    "news_article_paragraph_claims",
    "news_editorial_decisions",
}


def test_n3_database_can_upgrade_only_the_article_tables(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'n3-upgrade.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    for table in ARTICLE_TABLES:
        session.execute(text(f"DROP TABLE IF EXISTS {table}"))
    target = CreateNewsroomArticleTablesMigration().version
    for migration in MIGRATIONS:
        if migration.version != target:
            session.add(SchemaMigration(version=migration.version))
    session.commit()

    run_migrations(session)

    assert ARTICLE_TABLES <= set(inspect(engine).get_table_names())
    assert get_pending_migrations(session) == []


def test_article_revisions_separate_versions_by_every_reviewed_hash(tmp_path):
    """Same words over different evidence or pictures are different versions.

    Approval is bound to the content, evidence and media hash together, so the
    uniqueness of a stored revision has to span all three. Keying on the text
    alone would make a redraft after a withdrawn picture collide with the
    version an operator already reviewed.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'article-hashes.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run_migrations(session)

    columns = {
        row[2]
        for row in session.execute(
            text("PRAGMA index_info('uq_news_article_revision_content')")
        )
    }
    if not columns:
        indexes = session.execute(
            text("PRAGMA index_list('news_article_revisions')")
        ).fetchall()
        unique = [row[1] for row in indexes if row[2]]
        columns = {
            row[2]
            for name in unique
            for row in session.execute(text(f"PRAGMA index_info('{name}')"))
        }

    assert {"content_hash", "evidence_hash", "media_hash"} <= columns


PUBLICATION_TABLES = {
    "news_publications",
    "news_publication_versions",
    "news_correction_notices",
    "news_site_releases",
}


def test_n4_database_can_upgrade_only_the_publication_tables(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'n4-upgrade.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    for table in PUBLICATION_TABLES:
        session.execute(text(f"DROP TABLE IF EXISTS {table}"))
    target = CreateNewsroomPublicationTablesMigration().version
    for migration in MIGRATIONS:
        if migration.version != target:
            session.add(SchemaMigration(version=migration.version))
    session.commit()

    run_migrations(session)

    assert PUBLICATION_TABLES <= set(inspect(engine).get_table_names())
    assert get_pending_migrations(session) == []


def test_one_topic_can_only_hold_one_public_address(tmp_path):
    """A second slug for the same topic would split a correction into two articles."""
    engine = create_engine(f"sqlite:///{tmp_path / 'publication-identity.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    run_migrations(session)

    indexes = session.execute(text("PRAGMA index_list('news_publications')")).fetchall()
    unique_columns = {
        row[2]
        for name, is_unique in ((row[1], row[2]) for row in indexes)
        if is_unique
        for row in session.execute(text(f"PRAGMA index_info('{name}')"))
    }

    assert {"topic_id", "slug"} <= unique_columns
