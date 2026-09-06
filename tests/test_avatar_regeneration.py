"""Deliberately buying a presenter clip a second time.

The whole point of this module is that it must be hard. A retry after a
timeout is free; a regeneration is a new invoice. These tests check that the
two never blur into each other, and that no amount of double-clicking turns
one decision into two orders.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.core.avatar_jobs import compute_job_hash, reserve_scene
from btcedu.core.avatar_regeneration import (
    RegenerationError,
    active_revision,
    cancel,
    confirm,
    episode_revisions,
    idempotency_key,
    mark_consumed,
    prepare,
)
from btcedu.db import Base
from btcedu.models.avatar_job import AvatarJob, AvatarJobStatus
from btcedu.models.avatar_regeneration import AvatarRegenerationRequest, RegenerationStatus
from btcedu.models.media_asset import Base as MediaBase

EPISODE_ID = "ep_regen_001"
SCENE_ID = "sc_001"


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    MediaBase.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


def _job(session, scene_id=SCENE_ID, *, status=AvatarJobStatus.COMPLETED.value, cost=0.10):
    decision = reserve_scene(
        session,
        episode_id=EPISODE_ID,
        scene_id=scene_id,
        chapter_id="ch_01",
        content_hash=f"hash-{scene_id}",
        provider="heygen",
        engine="avatar_iii",
        avatar_look_id="look_a",
        output_format="webm",
        estimated_cost_usd=cost,
    )
    job = decision.job
    job.status = status
    job.cost_usd = cost
    job.duration_seconds = 6.0
    session.commit()
    return job


class TestPreparingIsNotBuying:
    def test_a_quote_shows_what_was_spent_and_what_it_would_cost(self, session):
        _job(session)
        quote = prepare(
            session,
            EPISODE_ID,
            SCENE_ID,
            reason="Lippen asynchron",
            requested_by_ref="op1",
            cost_per_second_usd=0.0167,
        )
        assert quote.previous_cost_usd == pytest.approx(0.10)
        assert quote.estimated_cost_usd == pytest.approx(6.0 * 0.0167, rel=1e-6)
        assert "paid" in quote.to_dict()["warning"]

    def test_a_prepared_request_authorises_nothing(self, session):
        _job(session)
        prepare(
            session,
            EPISODE_ID,
            SCENE_ID,
            reason="zu dunkel",
            requested_by_ref="op1",
            cost_per_second_usd=0.0167,
        )
        assert active_revision(session, EPISODE_ID, SCENE_ID) == 0
        assert episode_revisions(session, EPISODE_ID) == {}

    def test_a_regeneration_needs_a_reason(self, session):
        _job(session)
        with pytest.raises(RegenerationError):
            prepare(
                session,
                EPISODE_ID,
                SCENE_ID,
                reason="  ",
                requested_by_ref="op1",
                cost_per_second_usd=0.0167,
            )

    def test_a_regeneration_needs_an_operator_reference(self, session):
        _job(session)
        with pytest.raises(RegenerationError):
            prepare(
                session,
                EPISODE_ID,
                SCENE_ID,
                reason="x",
                requested_by_ref="",
                cost_per_second_usd=0.0167,
            )

    def test_a_scene_that_was_never_generated_cannot_be_regenerated(self, session):
        with pytest.raises(RegenerationError, match="nothing to regenerate"):
            prepare(
                session,
                EPISODE_ID,
                SCENE_ID,
                reason="x",
                requested_by_ref="op1",
                cost_per_second_usd=0.0167,
            )

    def test_an_unreconciled_scene_cannot_be_regenerated(self, session):
        """Buying a replacement for a job of unknown outcome pays twice."""
        _job(session, status=AvatarJobStatus.RECONCILE_REQUIRED.value)
        with pytest.raises(RegenerationError, match="avatar-reconcile"):
            prepare(
                session,
                EPISODE_ID,
                SCENE_ID,
                reason="x",
                requested_by_ref="op1",
                cost_per_second_usd=0.0167,
            )


class TestOneDecisionIsOneOrder:
    def test_a_double_click_produces_one_request(self, session):
        _job(session)
        first = prepare(
            session,
            EPISODE_ID,
            SCENE_ID,
            reason="x",
            requested_by_ref="op1",
            cost_per_second_usd=0.0167,
        )
        second = prepare(
            session,
            EPISODE_ID,
            SCENE_ID,
            reason="x",
            requested_by_ref="op1",
            cost_per_second_usd=0.0167,
        )
        assert first.request_id == second.request_id
        assert session.query(AvatarRegenerationRequest).count() == 1

    def test_confirming_twice_is_not_two_orders(self, session):
        _job(session)
        quote = prepare(
            session,
            EPISODE_ID,
            SCENE_ID,
            reason="x",
            requested_by_ref="op1",
            cost_per_second_usd=0.0167,
        )
        a = confirm(
            session, EPISODE_ID, SCENE_ID, revision=quote.revision, confirmed_by_ref="op1"
        )
        b = confirm(
            session, EPISODE_ID, SCENE_ID, revision=quote.revision, confirmed_by_ref="op1"
        )
        assert a.id == b.id
        assert session.query(AvatarRegenerationRequest).count() == 1

    def test_a_revision_nobody_was_shown_cannot_be_confirmed(self, session):
        _job(session)
        prepare(
            session,
            EPISODE_ID,
            SCENE_ID,
            reason="x",
            requested_by_ref="op1",
            cost_per_second_usd=0.0167,
        )
        with pytest.raises(RegenerationError, match="No regeneration request"):
            confirm(session, EPISODE_ID, SCENE_ID, revision=7, confirmed_by_ref="op1")

    def test_confirmation_needs_an_operator_reference(self, session):
        _job(session)
        quote = prepare(
            session,
            EPISODE_ID,
            SCENE_ID,
            reason="x",
            requested_by_ref="op1",
            cost_per_second_usd=0.0167,
        )
        with pytest.raises(RegenerationError):
            confirm(session, EPISODE_ID, SCENE_ID, revision=quote.revision, confirmed_by_ref="")

    def test_the_idempotency_key_is_stable_and_revision_specific(self, session):
        first = idempotency_key(EPISODE_ID, SCENE_ID, 1)
        assert first == idempotency_key(EPISODE_ID, SCENE_ID, 1)
        assert first != idempotency_key(EPISODE_ID, SCENE_ID, 2)
        assert first != idempotency_key(EPISODE_ID, "sc_002", 1)


class TestTheAuditTrailSurvives:
    def test_confirmation_records_who_agreed_to_pay(self, session):
        _job(session)
        quote = prepare(
            session,
            EPISODE_ID,
            SCENE_ID,
            reason="Blickrichtung",
            requested_by_ref="op1",
            cost_per_second_usd=0.0167,
        )
        record = confirm(
            session, EPISODE_ID, SCENE_ID, revision=quote.revision, confirmed_by_ref="op2"
        )
        assert record.requested_by_ref == "op1"
        assert record.confirmed_by_ref == "op2"
        assert record.confirmed_at is not None
        assert record.reason == "Blickrichtung"

    def test_the_old_job_is_never_deleted(self, session):
        job = _job(session)
        quote = prepare(
            session,
            EPISODE_ID,
            SCENE_ID,
            reason="x",
            requested_by_ref="op1",
            cost_per_second_usd=0.0167,
        )
        confirm(session, EPISODE_ID, SCENE_ID, revision=quote.revision, confirmed_by_ref="op1")
        assert session.query(AvatarJob).filter_by(id=job.id).first() is not None
        assert session.query(AvatarJob).count() == 1

    def test_other_scenes_are_untouched(self, session):
        _job(session)
        _job(session, scene_id="sc_003")
        quote = prepare(
            session,
            EPISODE_ID,
            SCENE_ID,
            reason="x",
            requested_by_ref="op1",
            cost_per_second_usd=0.0167,
        )
        confirm(session, EPISODE_ID, SCENE_ID, revision=quote.revision, confirmed_by_ref="op1")
        assert episode_revisions(session, EPISODE_ID) == {SCENE_ID: 1}
        assert active_revision(session, EPISODE_ID, "sc_003") == 0


class TestTheRevisionReachesTheLedger:
    def test_a_confirmed_revision_changes_the_content_hash(self, session):
        base = compute_job_hash(
            scene_id=SCENE_ID,
            text_hash="t",
            audio_hash="a",
            avatar_look_id="look_a",
            provider="heygen",
            engine="avatar_iii",
            output_format="webm",
            resolution="1080p",
            aspect_ratio="16:9",
        )
        revised = compute_job_hash(
            scene_id=SCENE_ID,
            text_hash="t",
            audio_hash="a",
            avatar_look_id="look_a",
            provider="heygen",
            engine="avatar_iii",
            output_format="webm",
            resolution="1080p",
            aspect_ratio="16:9",
            generation_revision=1,
        )
        assert base != revised

    def test_revision_zero_is_the_historical_hash(self, session):
        """Every hash computed before revisions existed must stay valid."""
        without = compute_job_hash(
            scene_id=SCENE_ID,
            text_hash="t",
            audio_hash="a",
            avatar_look_id="look_a",
            provider="heygen",
            engine="avatar_iii",
            output_format="webm",
            resolution="1080p",
            aspect_ratio="16:9",
        )
        with_zero = compute_job_hash(
            scene_id=SCENE_ID,
            text_hash="t",
            audio_hash="a",
            avatar_look_id="look_a",
            provider="heygen",
            engine="avatar_iii",
            output_format="webm",
            resolution="1080p",
            aspect_ratio="16:9",
            generation_revision=0,
        )
        assert without == with_zero

    def test_a_consumed_request_stays_active_for_the_hash(self, session):
        """The revision must not fall back to 0 once the clip was generated."""
        _job(session)
        quote = prepare(
            session,
            EPISODE_ID,
            SCENE_ID,
            reason="x",
            requested_by_ref="op1",
            cost_per_second_usd=0.0167,
        )
        confirm(session, EPISODE_ID, SCENE_ID, revision=quote.revision, confirmed_by_ref="op1")
        assert mark_consumed(session, EPISODE_ID, [SCENE_ID]) == 1
        assert active_revision(session, EPISODE_ID, SCENE_ID) == 1
        assert episode_revisions(session, EPISODE_ID) == {SCENE_ID: 1}

    def test_a_second_regeneration_gets_a_higher_revision(self, session):
        _job(session)
        first = prepare(
            session,
            EPISODE_ID,
            SCENE_ID,
            reason="x",
            requested_by_ref="op1",
            cost_per_second_usd=0.0167,
        )
        confirm(session, EPISODE_ID, SCENE_ID, revision=first.revision, confirmed_by_ref="op1")
        mark_consumed(session, EPISODE_ID, [SCENE_ID])
        second = prepare(
            session,
            EPISODE_ID,
            SCENE_ID,
            reason="immer noch falsch",
            requested_by_ref="op1",
            cost_per_second_usd=0.0167,
        )
        assert second.revision == first.revision + 1


class TestWithdrawing:
    def test_an_unconfirmed_request_can_be_cancelled(self, session):
        _job(session)
        quote = prepare(
            session,
            EPISODE_ID,
            SCENE_ID,
            reason="x",
            requested_by_ref="op1",
            cost_per_second_usd=0.0167,
        )
        record = cancel(session, EPISODE_ID, SCENE_ID, revision=quote.revision)
        assert record.status == RegenerationStatus.CANCELLED.value
        assert active_revision(session, EPISODE_ID, SCENE_ID) == 0

    def test_a_generated_regeneration_cannot_be_cancelled(self, session):
        _job(session)
        quote = prepare(
            session,
            EPISODE_ID,
            SCENE_ID,
            reason="x",
            requested_by_ref="op1",
            cost_per_second_usd=0.0167,
        )
        confirm(session, EPISODE_ID, SCENE_ID, revision=quote.revision, confirmed_by_ref="op1")
        mark_consumed(session, EPISODE_ID, [SCENE_ID])
        with pytest.raises(RegenerationError, match="unspend"):
            cancel(session, EPISODE_ID, SCENE_ID, revision=quote.revision)

    def test_a_cancelled_request_cannot_be_confirmed_later(self, session):
        _job(session)
        quote = prepare(
            session,
            EPISODE_ID,
            SCENE_ID,
            reason="x",
            requested_by_ref="op1",
            cost_per_second_usd=0.0167,
        )
        cancel(session, EPISODE_ID, SCENE_ID, revision=quote.revision)
        with pytest.raises(RegenerationError, match="cannot be confirmed"):
            confirm(
                session, EPISODE_ID, SCENE_ID, revision=quote.revision, confirmed_by_ref="op1"
            )
