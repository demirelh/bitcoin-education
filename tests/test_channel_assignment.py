"""Which channel an episode is filed under.

The dashboard's channel filter is a plain `channel_id` comparison, so an
episode is visible under a channel only if it carries that channel's id. The
local recorder resolved the channel from the *global* podcast settings, which
name exactly one channel: every tagesschau broadcast taken off disk was filed
under the Bitcoin podcast, and selecting "ARD tagesschau" in the dashboard
showed one of ten -- the single episode that had come in through the feed,
where the per-channel path assigns the id explicitly.

Nothing here contacts a provider or a feed; recordings are files in tmp_path.
"""

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from btcedu.core.detector import _resolve_channel_id, detect_local_recordings
from btcedu.db import Base
from btcedu.migrations import RepairEpisodeChannelByProfileMigration
from btcedu.models.channel import Channel
from btcedu.models.episode import Episode
from tests.test_local_recorder import local_settings, make_recording

PROFILE = "local_test"


@pytest.fixture
def recordings_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "recordings"
    directory.mkdir()
    return directory


def _channel(session, channel_id: str, profile: str, **kwargs) -> Channel:
    channel = Channel(
        channel_id=channel_id,
        name=kwargs.pop("name", channel_id),
        content_profile=profile,
        **kwargs,
    )
    session.add(channel)
    session.commit()
    return channel


# ---------------------------------------------------------------------------
# Resolving the channel for a newly detected episode
# ---------------------------------------------------------------------------


class TestResolveChannelId:
    def test_an_explicit_channel_always_wins(self, db_session, tmp_path, recordings_dir):
        _channel(db_session, "by-profile", PROFILE)
        settings = local_settings(tmp_path, recordings_dir)

        resolved = _resolve_channel_id(
            db_session, settings, "explicit", profile_name=PROFILE
        )

        assert resolved == "explicit"

    def test_the_profiles_channel_is_preferred_over_the_global_settings(
        self, db_session, tmp_path, recordings_dir
    ):
        """The regression: the global settings named the wrong channel."""
        _channel(
            db_session,
            "default",
            "bitcoin_podcast",
            youtube_channel_id="UC_PODCAST",
        )
        _channel(db_session, "tagesschau", PROFILE)
        settings = local_settings(
            tmp_path, recordings_dir, podcast_youtube_channel_id="UC_PODCAST"
        )

        resolved = _resolve_channel_id(db_session, settings, None, profile_name=PROFILE)

        assert resolved == "tagesschau"

    def test_the_global_settings_still_apply_when_no_profile_matches(
        self, db_session, tmp_path, recordings_dir
    ):
        """Every existing deployment keeps resolving exactly as before."""
        _channel(
            db_session,
            "default",
            "bitcoin_podcast",
            youtube_channel_id="UC_PODCAST",
        )
        settings = local_settings(
            tmp_path, recordings_dir, podcast_youtube_channel_id="UC_PODCAST"
        )

        resolved = _resolve_channel_id(
            db_session, settings, None, profile_name="a_profile_with_no_channel"
        )

        assert resolved == "default"

    def test_without_a_profile_the_behaviour_is_unchanged(
        self, db_session, tmp_path, recordings_dir
    ):
        _channel(
            db_session,
            "default",
            "bitcoin_podcast",
            youtube_channel_id="UC_PODCAST",
        )
        settings = local_settings(
            tmp_path, recordings_dir, podcast_youtube_channel_id="UC_PODCAST"
        )

        assert _resolve_channel_id(db_session, settings, None) == "default"

    def test_a_duplicated_profile_resolves_to_the_first_channel(
        self, db_session, tmp_path, recordings_dir
    ):
        """Ambiguity must be deterministic rather than arbitrary."""
        _channel(db_session, "first", PROFILE)
        _channel(db_session, "second", PROFILE)
        settings = local_settings(tmp_path, recordings_dir)

        assert (
            _resolve_channel_id(db_session, settings, None, profile_name=PROFILE)
            == "first"
        )

    def test_no_channel_at_all_resolves_to_none(self, db_session, tmp_path, recordings_dir):
        settings = local_settings(tmp_path, recordings_dir)

        assert _resolve_channel_id(db_session, settings, None, profile_name=PROFILE) is None


# ---------------------------------------------------------------------------
# Ingest
# ---------------------------------------------------------------------------


class TestLocalRecordingsAreFiledUnderTheirChannel:
    def test_a_recording_lands_on_the_channel_for_its_profile(
        self, db_session, tmp_path, recordings_dir
    ):
        _channel(
            db_session,
            "default",
            "bitcoin_podcast",
            youtube_channel_id="UC_PODCAST",
        )
        _channel(db_session, "tagesschau", PROFILE)
        make_recording(recordings_dir, date(2026, 8, 6))
        settings = local_settings(
            tmp_path, recordings_dir, podcast_youtube_channel_id="UC_PODCAST"
        )

        detect_local_recordings(db_session, settings)

        episode = db_session.query(Episode).one()
        assert episode.content_profile == PROFILE
        assert episode.channel_id == "tagesschau"

    def test_every_recording_of_a_profile_lands_on_the_same_channel(
        self, db_session, tmp_path, recordings_dir
    ):
        """Ten broadcasts must not become one visible and nine hidden."""
        _channel(db_session, "tagesschau", PROFILE)
        for day in (4, 5, 6):
            make_recording(recordings_dir, date(2026, 8, day))
        settings = local_settings(tmp_path, recordings_dir)

        detect_local_recordings(db_session, settings)

        channels = {ep.channel_id for ep in db_session.query(Episode).all()}
        assert db_session.query(Episode).count() == 3
        assert channels == {"tagesschau"}

    def test_an_explicit_channel_is_still_honoured(
        self, db_session, tmp_path, recordings_dir
    ):
        _channel(db_session, "tagesschau", PROFILE)
        make_recording(recordings_dir, date(2026, 8, 6))
        settings = local_settings(tmp_path, recordings_dir)

        detect_local_recordings(db_session, settings, channel_id="somewhere-else")

        assert db_session.query(Episode).one().channel_id == "somewhere-else"


# ---------------------------------------------------------------------------
# Migration 021: the episodes already stored on the wrong channel
# ---------------------------------------------------------------------------


@pytest.fixture
def migration_session(tmp_path: Path):
    engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def _episode(session, episode_id: str, channel_id: str | None, profile: str | None):
    session.add(
        Episode(
            episode_id=episode_id,
            channel_id=channel_id,
            source="local_recorder",
            title=episode_id,
            url=f"/tmp/{episode_id}.mp4",
            content_profile=profile,
        )
    )
    session.commit()


def _channel_of(session, episode_id: str) -> str | None:
    return session.execute(
        text("SELECT channel_id FROM episodes WHERE episode_id = :e"), {"e": episode_id}
    ).scalar()


class TestRepairEpisodeChannelMigration:
    def test_a_misfiled_episode_is_moved(self, migration_session):
        _channel(migration_session, "default", "bitcoin_podcast")
        _channel(migration_session, "tagesschau", "tagesschau_tr")
        _episode(migration_session, "ep_wrong", "default", "tagesschau_tr")

        RepairEpisodeChannelByProfileMigration().up(migration_session)

        assert _channel_of(migration_session, "ep_wrong") == "tagesschau"

    def test_a_correctly_filed_episode_is_left_alone(self, migration_session):
        _channel(migration_session, "default", "bitcoin_podcast")
        _channel(migration_session, "tagesschau", "tagesschau_tr")
        _episode(migration_session, "ep_ok", "default", "bitcoin_podcast")

        RepairEpisodeChannelByProfileMigration().up(migration_session)

        assert _channel_of(migration_session, "ep_ok") == "default"

    def test_a_channel_that_already_declares_the_profile_is_never_reshuffled(
        self, migration_session
    ):
        """Two channels may serve one profile; an episode must not migrate sideways.

        Only a contradiction is repaired -- an episode whose channel declares a
        *different* profile. Where the channel already agrees, the assignment is
        deliberate and is left exactly as an operator made it.
        """
        _channel(migration_session, "first", "tagesschau_tr")
        _channel(migration_session, "second", "tagesschau_tr")
        _episode(migration_session, "ep_second", "second", "tagesschau_tr")

        RepairEpisodeChannelByProfileMigration().up(migration_session)

        assert _channel_of(migration_session, "ep_second") == "second"

    def test_an_episode_whose_profile_has_no_channel_is_left_alone(self, migration_session):
        _channel(migration_session, "default", "bitcoin_podcast")
        _episode(migration_session, "ep_orphan", "default", "some_other_profile")

        RepairEpisodeChannelByProfileMigration().up(migration_session)

        assert _channel_of(migration_session, "ep_orphan") == "default"

    def test_an_episode_without_a_profile_is_left_alone(self, migration_session):
        _channel(migration_session, "default", "bitcoin_podcast")
        _episode(migration_session, "ep_noprofile", "default", None)

        RepairEpisodeChannelByProfileMigration().up(migration_session)

        assert _channel_of(migration_session, "ep_noprofile") == "default"

    def test_the_whole_backlog_is_repaired_at_once(self, migration_session):
        _channel(migration_session, "default", "bitcoin_podcast")
        _channel(migration_session, "tagesschau", "tagesschau_tr")
        for i in range(9):
            _episode(migration_session, f"ep_local_{i}", "default", "tagesschau_tr")
        _episode(migration_session, "ep_feed", "tagesschau", "tagesschau_tr")
        for i in range(3):
            _episode(migration_session, f"ep_pod_{i}", "default", "bitcoin_podcast")

        RepairEpisodeChannelByProfileMigration().up(migration_session)

        rows = migration_session.execute(
            text("SELECT channel_id, content_profile, COUNT(*) FROM episodes GROUP BY 1, 2")
        ).fetchall()
        assert set(rows) == {
            ("default", "bitcoin_podcast", 3),
            ("tagesschau", "tagesschau_tr", 10),
        }

    def test_running_it_twice_changes_nothing(self, migration_session):
        _channel(migration_session, "default", "bitcoin_podcast")
        _channel(migration_session, "tagesschau", "tagesschau_tr")
        _episode(migration_session, "ep_wrong", "default", "tagesschau_tr")

        RepairEpisodeChannelByProfileMigration().up(migration_session)
        migration_session.execute(
            text(
                "DELETE FROM schema_migrations "
                "WHERE version = '021_repair_episode_channel_by_profile'"
            )
        )
        migration_session.commit()
        RepairEpisodeChannelByProfileMigration().up(migration_session)

        assert _channel_of(migration_session, "ep_wrong") == "tagesschau"

    def test_it_survives_a_database_without_the_tables(self, tmp_path):
        engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}")
        from btcedu.models.migration import SchemaMigration

        SchemaMigration.__table__.create(engine, checkfirst=True)
        session = sessionmaker(bind=engine)()

        RepairEpisodeChannelByProfileMigration().up(session)

        applied = session.execute(
            text(
                "SELECT COUNT(*) FROM schema_migrations "
                "WHERE version = '021_repair_episode_channel_by_profile'"
            )
        ).scalar()
        assert applied == 1
        session.close()
