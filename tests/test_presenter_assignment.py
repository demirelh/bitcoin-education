"""Per-episode outfit assignment: chosen once, rotated across episodes."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core.anchor_config import AnchorConfig, PresenterLook, StudioConfig
from btcedu.core.presenter_assignment import (
    NoActiveLookError,
    assignment_path,
    compute_look_pool_hash,
    ensure_assignment,
    get_assignment,
    reassign_look,
    select_look,
)
from btcedu.db import Base
from btcedu.models.presenter_assignment import PresenterAssignment


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    yield db
    db.close()


@pytest.fixture
def outputs_dir(tmp_path):
    return tmp_path / "outputs"


def _config(look_count: int = 3, strategy: str = "least_recently_used") -> AnchorConfig:
    looks = tuple(
        PresenterLook(name=f"look_{i:02d}", avatar_look_id=f"heygen-look-{i:02d}", active=True)
        for i in range(1, look_count + 1)
    )
    return AnchorConfig(
        provider="heygen",
        engine="avatar_iii",
        source_image="",
        source_image_url="",
        avatar_id="",
        avatar_type="digital_twin",
        expression="",
        output_format="webm",
        resolution="1080p",
        aspect_ratio="16:9",
        cost_per_second_usd=0.0167,
        max_cost_usd=7.0,
        studio_mode="composite",
        max_concurrent_jobs=4,
        rotation_strategy=strategy,
        looks=looks,
        studio=StudioConfig(asset_dir="assets/almanya24/studio"),
    )


class TestOneOutfitPerEpisode:
    def test_assignment_is_created_once_and_then_reused(self, session, outputs_dir):
        config = _config()

        first = ensure_assignment(session, "ep_001", config, outputs_dir)
        second = ensure_assignment(session, "ep_001", config, outputs_dir)

        assert first.id == second.id
        assert first.avatar_look_id == second.avatar_look_id
        assert session.query(PresenterAssignment).count() == 1

    def test_repeated_calls_never_re_roll_the_outfit(self, session, outputs_dir):
        config = _config(look_count=10)

        chosen = ensure_assignment(session, "ep_001", config, outputs_dir).avatar_look_id
        # Stands in for a retry loop and a few restarts.
        for _ in range(20):
            again = ensure_assignment(session, "ep_001", config, outputs_dir)
            assert again.avatar_look_id == chosen

    def test_database_refuses_a_second_assignment_for_one_episode(self, session, outputs_dir):
        config = _config()
        ensure_assignment(session, "ep_001", config, outputs_dir)

        # The constraint is the mechanism, not the application code: even a
        # direct insert must not be able to give one episode two outfits.
        session.add(
            PresenterAssignment(
                episode_id="ep_001",
                provider="heygen",
                engine="avatar_iii",
                avatar_type="digital_twin",
                avatar_look_id="heygen-look-99",
                look_name="look_99",
                strategy="least_recently_used",
                assigned_at=datetime.now(UTC),
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()

    def test_restart_with_a_fresh_session_keeps_the_outfit(self, session, outputs_dir):
        config = _config(look_count=10)
        chosen = ensure_assignment(session, "ep_001", config, outputs_dir).avatar_look_id

        session.expunge_all()
        reloaded = get_assignment(session, "ep_001")

        assert reloaded.avatar_look_id == chosen


class TestRotation:
    def test_new_episodes_spend_the_whole_pool_before_repeating(self, session, outputs_dir):
        config = _config(look_count=10)

        used = [
            ensure_assignment(session, f"ep_{i:03d}", config, outputs_dir).avatar_look_id
            for i in range(10)
        ]

        assert len(set(used)) == 10

    def test_eleventh_episode_reuses_the_oldest_look(self, session, outputs_dir):
        config = _config(look_count=3)

        first = ensure_assignment(session, "ep_001", config, outputs_dir).avatar_look_id
        ensure_assignment(session, "ep_002", config, outputs_dir)
        ensure_assignment(session, "ep_003", config, outputs_dir)
        fourth = ensure_assignment(session, "ep_004", config, outputs_dir).avatar_look_id

        # The pool is exhausted, so the longest-unused look comes back first.
        assert fourth == first

    def test_empty_history_takes_the_first_configured_look(self, session):
        config = _config(look_count=3)

        # Ties break on profile order, so a fresh database is deterministic
        # rather than arbitrary.
        assert select_look(session, config).name == "look_01"

    def test_rotation_prefers_the_least_recently_used_not_the_least_used(
        self, session, outputs_dir
    ):
        config = _config(look_count=2)
        now = datetime.now(UTC)

        # look_01 used twice but long ago; look_02 used once, recently.
        for offset, look in ((30, "01"), (20, "01"), (1, "02")):
            session.add(
                PresenterAssignment(
                    episode_id=f"old_{offset}_{look}",
                    provider="heygen",
                    engine="avatar_iii",
                    avatar_type="digital_twin",
                    avatar_look_id=f"heygen-look-{look}",
                    look_name=f"look_{look}",
                    strategy="least_recently_used",
                    assigned_at=now - timedelta(days=offset),
                )
            )
        session.commit()

        assert select_look(session, config).name == "look_01"

    def test_fixed_strategy_always_returns_the_first_look(self, session, outputs_dir):
        config = _config(look_count=3, strategy="fixed")

        for i in range(5):
            assignment = ensure_assignment(session, f"ep_{i:03d}", config, outputs_dir)
            assert assignment.look_name == "look_01"

    def test_inactive_looks_are_never_chosen(self, session):
        config = _config(look_count=3)
        config = replace(
            config,
            looks=(
                PresenterLook("look_01", "heygen-look-01", active=False),
                PresenterLook("look_02", "heygen-look-02", active=True),
            ),
        )

        assert select_look(session, config).name == "look_02"

    def test_no_active_look_is_a_clear_error(self, session):
        config = replace(
            _config(),
            looks=(PresenterLook("look_01", "heygen-look-01", active=False),),
        )

        with pytest.raises(NoActiveLookError, match="No active anchor look"):
            select_look(session, config)


class TestArtifactMirror:
    def test_artifact_is_written_for_the_renderer(self, session, outputs_dir):
        config = _config()
        assignment = ensure_assignment(session, "ep_001", config, outputs_dir)

        path = assignment_path(outputs_dir, "ep_001")
        assert path.exists()
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["episode_id"] == "ep_001"
        assert data["engine"] == "avatar_iii"
        assert data["avatar_look_id"] == assignment.avatar_look_id
        assert data["look_name"] == assignment.look_name
        assert data["schema_version"] == 1

    def test_missing_artifact_is_repaired_without_changing_the_outfit(
        self, session, outputs_dir
    ):
        config = _config(look_count=10)
        chosen = ensure_assignment(session, "ep_001", config, outputs_dir).avatar_look_id
        assignment_path(outputs_dir, "ep_001").unlink()

        again = ensure_assignment(session, "ep_001", config, outputs_dir)

        assert again.avatar_look_id == chosen
        assert assignment_path(outputs_dir, "ep_001").exists()

    def test_artifact_carries_no_api_key(self, session, outputs_dir):
        config = _config()
        ensure_assignment(session, "ep_001", config, outputs_dir)

        text = assignment_path(outputs_dir, "ep_001").read_text(encoding="utf-8")
        assert "api_key" not in text.lower()


class TestDeliberateOutfitChange:
    def test_unconfirmed_change_is_refused(self, session, outputs_dir):
        config = _config()
        ensure_assignment(session, "ep_001", config, outputs_dir)

        with pytest.raises(ValueError, match="confirmed=True"):
            reassign_look(session, "ep_001", config, outputs_dir)

    def test_confirmed_change_moves_to_a_different_look(self, session, outputs_dir):
        config = _config(look_count=3)
        before = ensure_assignment(session, "ep_001", config, outputs_dir).avatar_look_id

        after = reassign_look(session, "ep_001", config, outputs_dir, confirmed=True)

        assert after.avatar_look_id != before
        # Still exactly one outfit for this episode.
        assert session.query(PresenterAssignment).filter_by(episode_id="ep_001").count() == 1

    def test_named_change_picks_the_requested_look(self, session, outputs_dir):
        config = _config(look_count=3)
        ensure_assignment(session, "ep_001", config, outputs_dir)

        after = reassign_look(
            session, "ep_001", config, outputs_dir, look_name="look_03", confirmed=True
        )

        assert after.look_name == "look_03"

    def test_unknown_look_name_is_refused(self, session, outputs_dir):
        config = _config(look_count=3)
        ensure_assignment(session, "ep_001", config, outputs_dir)

        with pytest.raises(NoActiveLookError, match="look_99"):
            reassign_look(
                session, "ep_001", config, outputs_dir, look_name="look_99", confirmed=True
            )

    def test_change_updates_the_mirrored_artifact(self, session, outputs_dir):
        config = _config(look_count=3)
        ensure_assignment(session, "ep_001", config, outputs_dir)

        after = reassign_look(session, "ep_001", config, outputs_dir, confirmed=True)

        data = json.loads(assignment_path(outputs_dir, "ep_001").read_text(encoding="utf-8"))
        assert data["avatar_look_id"] == after.avatar_look_id

    def test_single_look_pool_cannot_be_rotated_away_from(self, session, outputs_dir):
        config = replace(
            _config(),
            looks=(PresenterLook("look_01", "heygen-look-01", active=True),),
        )
        ensure_assignment(session, "ep_001", config, outputs_dir)

        with pytest.raises(NoActiveLookError, match="no alternative outfit"):
            reassign_look(session, "ep_001", config, outputs_dir, confirmed=True)


class TestLookPoolHash:
    def test_hash_tracks_the_active_pool(self):
        base = _config(look_count=3)
        smaller = replace(base, looks=base.looks[:2])

        assert compute_look_pool_hash(base) != compute_look_pool_hash(smaller)

    def test_hash_ignores_a_price_correction(self):
        base = _config()
        repriced = replace(base, cost_per_second_usd=0.02)

        # Correcting a rate must not read like the presenter changed.
        assert compute_look_pool_hash(base) == compute_look_pool_hash(repriced)

    def test_hash_ignores_a_studio_swap(self):
        base = _config()
        restyled = replace(base, studio=StudioConfig(asset_dir="assets/other"))

        assert compute_look_pool_hash(base) == compute_look_pool_hash(restyled)

    def test_hash_tracks_the_engine(self):
        base = _config()
        other_engine = replace(base, engine="avatar_iv")

        assert compute_look_pool_hash(base) != compute_look_pool_hash(other_engine)


class TestProfileIntegration:
    def test_almanya24_profile_has_no_usable_look_before_phase_one(self, session, tmp_path):
        from btcedu.core.anchor_config import resolve_anchor_config

        settings = Settings(outputs_dir=str(tmp_path / "outputs"))
        config = resolve_anchor_config("tagesschau_tr", settings)

        # Placeholders ship inactive, so activation cannot silently proceed.
        with pytest.raises(NoActiveLookError):
            select_look(session, config)
