"""The no-silent-fallback policy.

The old ALMANYA24 presentation — a full-frame picture with a voice-over — is
still in the code, so the danger these tests guard is not a crash but a
plausible-looking success: an episode that quietly publishes in the abandoned
format because HeyGen had a bad night.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.core.anchor_fallback import (
    ACTION_STOP,
    ACTION_VOICE_OVER,
    AnchorPolicyError,
    active_voice_over_override,
    failure_action,
    record_voice_over_override,
    require_anchor_usable,
    unusable_jobs,
)
from btcedu.core.avatar_jobs import (
    hold_for_reconciliation,
    record_completion,
    record_submission,
    reserve_scene,
)
from btcedu.db import Base
from btcedu.models.avatar_job import AvatarJobStatus
from btcedu.models.avatar_job_audit import AvatarAuditAction, AvatarJobAudit


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    yield db
    db.close()


def _reserve(session, *, scene_id="ch01_s00", episode_id="ep1"):
    return reserve_scene(
        session,
        episode_id=episode_id,
        scene_id=scene_id,
        chapter_id="ch01",
        content_hash=f"hash-{episode_id}-{scene_id}",
        provider="heygen",
        engine="avatar_iii",
        avatar_look_id="look-1",
        output_format="webm",
        estimated_cost_usd=1.0,
    ).job


def _completed(session, **kwargs):
    job = _reserve(session, **kwargs)
    record_submission(session, job, "prov-1")
    record_completion(session, job, output_path="clip.webm", duration_seconds=4.0, cost_usd=1.0)
    return job


class TestTheGate:
    def test_a_clean_episode_passes(self, session):
        _completed(session)

        require_anchor_usable(session, "ep1", anchor_enabled=True)

    def test_an_episode_with_no_avatar_jobs_passes(self, session):
        require_anchor_usable(session, "ep1", anchor_enabled=True)

    def test_a_held_job_stops_the_episode(self, session):
        job = _reserve(session)
        hold_for_reconciliation(session, job, "run died before the provider answered")

        with pytest.raises(AnchorPolicyError) as exc:
            require_anchor_usable(session, "ep1", anchor_enabled=True)

        assert "ch01_s00" in str(exc.value)
        assert "avatar-reconcile" in str(exc.value)

    def test_a_reserved_job_stops_the_episode(self, session):
        _reserve(session)

        with pytest.raises(AnchorPolicyError):
            require_anchor_usable(session, "ep1", anchor_enabled=True)

    def test_an_abandoned_job_stops_the_episode(self, session):
        job = _reserve(session)
        job.status = AvatarJobStatus.ABANDONED.value
        session.commit()

        with pytest.raises(AnchorPolicyError):
            require_anchor_usable(session, "ep1", anchor_enabled=True)

    def test_another_episode_s_trouble_is_not_this_episode_s(self, session):
        job = _reserve(session, episode_id="ep2")
        hold_for_reconciliation(session, job, "unknown outcome")
        _completed(session, episode_id="ep1")

        require_anchor_usable(session, "ep1", anchor_enabled=True)

    def test_the_disabled_anchor_path_is_untouched(self, session):
        job = _reserve(session)
        hold_for_reconciliation(session, job, "unknown outcome")

        # Profiles that never use an avatar must not inherit this gate.
        require_anchor_usable(session, "ep1", anchor_enabled=False)

    def test_the_error_names_the_scenes_not_a_remedy_it_chose(self, session):
        for scene in ("ch01_s00", "ch02_s00"):
            hold_for_reconciliation(session, _reserve(session, scene_id=scene), "unknown")

        with pytest.raises(AnchorPolicyError) as exc:
            require_anchor_usable(session, "ep1", anchor_enabled=True)

        message = str(exc.value)
        assert "ch01_s00" in message and "ch02_s00" in message
        # It reports; it does not pick a presentation.
        assert "does not choose" in message


class TestNoAutomaticFallback:
    def test_the_default_action_is_to_stop(self, session):
        hold_for_reconciliation(session, _reserve(session), "unknown outcome")

        assert failure_action(session, "ep1") == ACTION_STOP

    def test_repeated_failures_never_earn_a_fallback(self, session):
        for scene in ("ch01_s00", "ch02_s00", "ch03_s00"):
            hold_for_reconciliation(session, _reserve(session, scene_id=scene), "unknown")

        # There is no threshold, no counter and no "enough is enough" path.
        assert failure_action(session, "ep1") == ACTION_STOP

    def test_an_override_for_another_episode_does_not_carry_over(self, session):
        record_voice_over_override(
            session, "ep2", operator_ref="ops-1", reason="provider outage"
        )

        assert failure_action(session, "ep1") == ACTION_STOP


class TestTheOverride:
    def test_an_override_permits_exactly_one_episode(self, session):
        override = record_voice_over_override(
            session, "ep1", operator_ref="ops-1", reason="HeyGen outage before the deadline"
        )

        assert override.episode_id == "ep1"
        assert failure_action(session, "ep1") == ACTION_VOICE_OVER
        assert failure_action(session, "ep2") == ACTION_STOP

    def test_an_override_always_demands_a_fresh_final_review(self, session):
        override = record_voice_over_override(
            session, "ep1", operator_ref="ops-1", reason="provider outage"
        )

        # The approval that exists was granted for a presented bulletin.
        assert override.requires_final_review is True
        assert override.to_dict()["requires_final_review"] is True

    def test_an_override_is_audited(self, session):
        record_voice_over_override(
            session, "ep1", operator_ref="ops-9", reason="provider outage before the deadline"
        )

        entry = session.query(AvatarJobAudit).one()
        assert entry.action == AvatarAuditAction.VOICE_OVER_OVERRIDE.value
        assert entry.episode_id == "ep1"
        assert entry.operator_ref == "ops-9"
        assert "outage" in entry.note
        assert entry.to_status == "voice_over"

    def test_an_override_needs_a_reason(self, session):
        with pytest.raises(AnchorPolicyError, match="reason"):
            record_voice_over_override(session, "ep1", operator_ref="ops-1", reason="  ")

    def test_an_override_needs_an_operator(self, session):
        with pytest.raises(AnchorPolicyError, match="operator"):
            record_voice_over_override(session, "ep1", operator_ref="", reason="outage")

    def test_an_override_needs_an_episode(self, session):
        with pytest.raises(AnchorPolicyError, match="episode"):
            record_voice_over_override(session, "  ", operator_ref="ops-1", reason="outage")

    def test_an_override_unblocks_the_gate(self, session):
        hold_for_reconciliation(session, _reserve(session), "unknown outcome")
        record_voice_over_override(
            session, "ep1", operator_ref="ops-1", reason="provider outage"
        )

        require_anchor_usable(session, "ep1", anchor_enabled=True)

    def test_the_latest_override_is_the_one_that_counts(self, session):
        record_voice_over_override(session, "ep1", operator_ref="ops-1", reason="first")
        record_voice_over_override(session, "ep1", operator_ref="ops-2", reason="second")

        assert active_voice_over_override(session, "ep1").operator_ref == "ops-2"

    def test_no_override_reads_as_none(self, session):
        assert active_voice_over_override(session, "ep1") is None


class TestUnusableJobs:
    def test_only_unusable_states_are_reported(self, session):
        _completed(session, scene_id="ch01_s00")
        hold_for_reconciliation(session, _reserve(session, scene_id="ch02_s00"), "unknown")
        submitted = _reserve(session, scene_id="ch03_s00")
        record_submission(session, submitted, "prov-3")

        scenes = [job.scene_id for job in unusable_jobs(session, "ep1")]

        # Submitted is still in flight, not unusable; completed is fine.
        assert scenes == ["ch02_s00"]

    def test_a_refused_job_counts_as_unusable(self, session):
        job = _reserve(session)
        job.status = AvatarJobStatus.FAILED.value
        session.commit()

        assert [j.scene_id for j in unusable_jobs(session, "ep1")] == ["ch01_s00"]
