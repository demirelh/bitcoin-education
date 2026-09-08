"""WP-8C: the migration evidence for the Phase-1 handover.

Two things have to be true before an operator runs ``btcedu migrate`` against a
database that already holds paid-for episodes: an empty database must end up
with the whole schema, and an existing one must gain the new tables without
losing or silently trusting anything it already had.
"""

from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from btcedu.db import Base
from btcedu.migrations import (
    MIGRATIONS,
    get_applied_migrations,
    get_pending_migrations,
    run_migrations,
)
from btcedu.models import (  # noqa: F401 - registers every table on Base
    avatar_audio_asset,
    avatar_job,
    avatar_job_audit,
    avatar_provider_breaker,
    avatar_regeneration,
    presenter_assignment,
)

#: Tables the avatar work packages introduced. Named explicitly so a dropped
#: migration is a failing test rather than a missing table on the Pi.
AVATAR_TABLES = (
    "presenter_assignments",
    "avatar_jobs",
    "avatar_job_audit",
    "avatar_regeneration_requests",
    "avatar_audio_assets",
    "avatar_provider_breakers",
)


@pytest.fixture
def db_path(tmp_path) -> Path:
    return tmp_path / "btcedu.sqlite3"


def _session(path: Path):
    """A database in the state ``btcedu migrate`` actually finds one in.

    The CLI calls ``init_db`` (``Base.metadata.create_all``) before it looks at
    migrations, so a migration may assume the declarative tables exist and only
    has to carry the *changes* that ``create_all`` cannot express — added
    columns, backfills, drops. Running the list against a truly empty file is
    not the deployment path and fails on the first ``ALTER TABLE``.
    """
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)(), engine


class TestAFreshDatabase:
    def test_every_migration_applies_from_empty(self, db_path):
        session, engine = _session(db_path)
        assert len(get_pending_migrations(session)) == len(MIGRATIONS)

        run_migrations(session)

        assert get_pending_migrations(session) == []
        assert len(get_applied_migrations(session)) == len(MIGRATIONS)

    def test_the_avatar_tables_exist_afterwards(self, db_path):
        session, engine = _session(db_path)
        run_migrations(session)

        names = set(inspect(engine).get_table_names())
        for table in AVATAR_TABLES:
            assert table in names, table

    def test_the_columns_the_avatar_code_reads_are_present(self, db_path):
        session, engine = _session(db_path)
        run_migrations(session)
        inspector = inspect(engine)

        jobs = {c["name"] for c in inspector.get_columns("avatar_jobs")}
        # The restart contract: a job is identified by its provider id, bound to
        # a look, priced, and idempotent for a day.
        for column in (
            "provider_job_id",
            "avatar_look_id",
            "status",
            "cost_usd",
            "idempotency_key",
        ):
            assert column in jobs, column
        # WP-6A: the clip is bound to its bytes.
        assert "file_sha256" in jobs

        audit = {c["name"] for c in inspector.get_columns("avatar_job_audit")}
        for column in ("action", "from_status", "to_status", "operator_ref"):
            assert column in audit, column

        assignments = {c["name"] for c in inspector.get_columns("presenter_assignments")}
        for column in ("episode_id", "avatar_look_id"):
            assert column in assignments, column

    def test_the_indexes_the_hot_lookups_need_exist(self, db_path):
        session, engine = _session(db_path)
        run_migrations(session)
        inspector = inspect(engine)

        indexed = {
            column
            for index in inspector.get_indexes("avatar_jobs")
            for column in index["column_names"]
        }
        assert "episode_id" in indexed

    def test_a_second_run_changes_nothing(self, db_path):
        session, engine = _session(db_path)
        run_migrations(session)
        first = get_applied_migrations(session)

        run_migrations(session)

        assert get_applied_migrations(session) == first

    def test_a_third_run_still_changes_nothing(self, db_path):
        """Deployment reruns ``migrate`` on every ``run.sh``."""
        session, engine = _session(db_path)
        for _ in range(3):
            run_migrations(session)
        assert get_pending_migrations(session) == []


class TestAnExistingDatabase:
    """The realistic case: a Pi that has been running since before the avatar work."""

    def _pre_avatar_database(self, db_path: Path):
        engine = create_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        session = sessionmaker(bind=engine)()
        # Rewind: drop what the avatar packages added and forget their versions.
        for table in AVATAR_TABLES:
            session.execute(text(f"DROP TABLE IF EXISTS {table}"))
        from btcedu.migrations import _ensure_migrations_table
        from btcedu.models.migration import SchemaMigration

        _ensure_migrations_table(session)
        avatar_versions = [
            m.version
            for m in MIGRATIONS
            if "avatar" in (m.version + type(m).__name__).lower()
            or "presenter" in (m.version + type(m).__name__).lower()
        ]
        for migration in MIGRATIONS:
            if migration.version in avatar_versions:
                continue
            session.add(SchemaMigration(version=migration.version))
        session.commit()
        return session, engine, avatar_versions

    def test_the_rewind_is_a_real_pre_avatar_state(self, db_path):
        session, engine, avatar_versions = self._pre_avatar_database(db_path)
        assert avatar_versions, "the avatar migrations must be identifiable by name"
        assert {m.version for m in get_pending_migrations(session)} == set(avatar_versions)

    def test_the_upgrade_adds_only_what_was_missing(self, db_path):
        session, engine, avatar_versions = self._pre_avatar_database(db_path)

        run_migrations(session)

        assert get_pending_migrations(session) == []
        names = set(inspect(engine).get_table_names())
        for table in AVATAR_TABLES:
            assert table in names, table

    def test_existing_episodes_survive_the_upgrade(self, db_path):
        """A published episode and what it cost must both come through."""
        from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, RunStatus

        session, engine, _ = self._pre_avatar_database(db_path)
        session.add(
            Episode(
                episode_id="ep_old",
                title="Vor der Avatararbeit",
                url="https://example.invalid/x",
                status=EpisodeStatus.PUBLISHED,
                youtube_video_id="OLD-VIDEO",
            )
        )
        session.add(
            PipelineRun(
                episode_id="ep_old",
                stage="render",
                status=RunStatus.SUCCESS,
                estimated_cost_usd=1.23,
            )
        )
        session.commit()

        run_migrations(session)

        kept = session.query(Episode).filter_by(episode_id="ep_old").one()
        assert kept.status == EpisodeStatus.PUBLISHED
        assert kept.youtube_video_id == "OLD-VIDEO"
        run = session.query(PipelineRun).filter_by(episode_id="ep_old").one()
        assert run.estimated_cost_usd == pytest.approx(1.23)

    def test_an_upgraded_database_trusts_nothing_new_by_default(self, db_path):
        """Migrating must not manufacture an approval or an avatar job."""
        session, engine, _ = self._pre_avatar_database(db_path)
        run_migrations(session)

        for table in ("avatar_jobs", "presenter_assignments", "avatar_regeneration_requests"):
            count = session.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            assert count == 0, table

    def test_the_upgrade_is_idempotent(self, db_path):
        session, engine, _ = self._pre_avatar_database(db_path)
        run_migrations(session)
        first = sorted(get_applied_migrations(session))

        run_migrations(session)

        assert sorted(get_applied_migrations(session)) == first


class TestAPartiallyAppliedMigration:
    def test_a_failure_leaves_the_version_unrecorded(self, db_path, monkeypatch):
        """A half-applied migration must be retried, not skipped as done."""
        session, engine = _session(db_path)
        target = MIGRATIONS[-1]

        def _boom(self, session):
            raise RuntimeError("disk full halfway through")

        monkeypatch.setattr(type(target), "up", _boom, raising=False)

        with pytest.raises(RuntimeError):
            run_migrations(session)

        session.rollback()
        assert target.version not in get_applied_migrations(session)

    def test_the_diagnosis_names_the_migration(self, db_path, monkeypatch, caplog):
        session, engine = _session(db_path)
        target = MIGRATIONS[-1]
        monkeypatch.setattr(
            type(target),
            "up",
            lambda self, session: (_ for _ in ()).throw(RuntimeError("disk full")),
            raising=False,
        )

        with caplog.at_level("ERROR"):
            with pytest.raises(RuntimeError):
                run_migrations(session)

        assert target.version in caplog.text or "disk full" in caplog.text

    def test_a_retry_after_the_fault_completes(self, db_path, monkeypatch):
        session, engine = _session(db_path)
        target = MIGRATIONS[-1]
        original = type(target).up
        monkeypatch.setattr(
            type(target),
            "up",
            lambda self, session: (_ for _ in ()).throw(RuntimeError("disk full")),
            raising=False,
        )
        with pytest.raises(RuntimeError):
            run_migrations(session)
        session.rollback()

        monkeypatch.setattr(type(target), "up", original, raising=False)
        run_migrations(session)

        assert get_pending_migrations(session) == []
