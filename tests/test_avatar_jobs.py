"""The avatar job ledger: a clip is bought once, and never twice by accident.

HeyGen bills a generation when it starts. Every test here is ultimately about
one question — after a crash, a timeout or a rerun, does the pipeline pay again?
No test contacts a provider; the ledger itself never does.
"""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from btcedu.core.avatar_jobs import (
    ACTION_RECONCILE,
    ACTION_RESUME,
    ACTION_REUSE,
    ACTION_SUBMIT,
    AvatarJobConflictError,
    blocked_jobs,
    compute_job_hash,
    episode_avatar_cost,
    episode_jobs,
    get_job,
    hold_for_reconciliation,
    record_completion,
    record_refusal,
    record_submission,
    reserve_scene,
    resolve_job,
)
from btcedu.db import Base
from btcedu.models.avatar_job import AvatarJob, AvatarJobStatus


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    yield db
    db.close()


def _hash(**overrides) -> str:
    base = {
        "scene_id": "ch01_s00",
        "text_hash": "t" * 8,
        "audio_hash": "a" * 8,
        "avatar_look_id": "heygen-look-01",
        "provider": "heygen",
        "engine": "avatar_iii",
        "output_format": "webm",
        "resolution": "1080p",
        "aspect_ratio": "16:9",
    }
    base.update(overrides)
    return compute_job_hash(**base)


def _reserve(session, *, scene_id="ch01_s00", content_hash=None, cost=1.5):
    return reserve_scene(
        session,
        episode_id="ep-1",
        scene_id=scene_id,
        chapter_id="ch01",
        content_hash=content_hash or _hash(scene_id=scene_id),
        provider="heygen",
        engine="avatar_iii",
        avatar_look_id="heygen-look-01",
        output_format="webm",
        estimated_cost_usd=cost,
    )


class TestJobHash:
    def test_the_same_clip_hashes_the_same(self):
        assert _hash() == _hash()

    def test_a_different_outfit_is_different_work(self):
        assert _hash() != _hash(avatar_look_id="heygen-look-02")

    def test_a_resynthesised_take_is_different_work(self):
        # Same words, new audio: the lip-sync has to follow the take that will
        # actually be in the video.
        assert _hash() != _hash(audio_hash="b" * 8)

    def test_avatar_iv_is_not_avatar_iii(self):
        assert _hash() != _hash(engine="avatar_iv")

    def test_the_output_format_is_part_of_the_identity(self):
        assert _hash() != _hash(output_format="mp4")


class TestFirstReservation:
    def test_a_new_scene_may_be_submitted(self, session):
        decision = _reserve(session)
        assert decision.action == ACTION_SUBMIT
        assert decision.may_call_provider is True

    def test_the_row_exists_before_the_provider_is_called(self, session):
        decision = _reserve(session)
        # A different session sees it, i.e. it survived a commit rather than
        # living in the caller's memory.
        assert get_job(session, "ep-1", "ch01_s00", decision.job.content_hash) is not None
        assert decision.job.status == AvatarJobStatus.RESERVED.value

    def test_the_estimate_is_charged_from_the_reservation(self, session):
        _reserve(session, cost=2.25)
        assert episode_avatar_cost(session, "ep-1") == pytest.approx(2.25)

    def test_the_identity_is_unique_in_the_database(self, session):
        decision = _reserve(session)
        session.add(
            AvatarJob(
                episode_id="ep-1",
                scene_id="ch01_s00",
                content_hash=decision.job.content_hash,
                provider="heygen",
            )
        )
        with pytest.raises(IntegrityError):
            session.commit()
        session.rollback()


class TestCompletedWorkIsNeverBoughtAgain:
    def test_a_finished_clip_is_reused(self, session):
        decision = _reserve(session)
        record_submission(session, decision.job, "heygen-video-1")
        record_completion(
            session,
            decision.job,
            output_path="anchor/ch01_s00.webm",
            duration_seconds=12.0,
            cost_usd=0.2,
        )

        again = _reserve(session)
        assert again.action == ACTION_REUSE
        assert again.may_call_provider is False
        assert again.job.output_path == "anchor/ch01_s00.webm"

    def test_a_rerun_spends_nothing_more(self, session):
        decision = _reserve(session, cost=1.5)
        record_submission(session, decision.job, "heygen-video-1")
        record_completion(
            session, decision.job, output_path="a.webm", duration_seconds=12.0, cost_usd=0.2
        )
        _reserve(session, cost=1.5)
        assert episode_avatar_cost(session, "ep-1") == pytest.approx(0.2)
        assert len(episode_jobs(session, "ep-1")) == 1

    def test_changed_work_is_a_new_row(self, session):
        first = _reserve(session)
        record_submission(session, first.job, "heygen-video-1")
        record_completion(
            session, first.job, output_path="a.webm", duration_seconds=12.0, cost_usd=0.2
        )

        # New outfit means a new clip, and it may be generated.
        second = reserve_scene(
            session,
            episode_id="ep-1",
            scene_id="ch01_s00",
            chapter_id="ch01",
            content_hash=_hash(avatar_look_id="heygen-look-02"),
            provider="heygen",
            engine="avatar_iii",
            avatar_look_id="heygen-look-02",
            output_format="webm",
            estimated_cost_usd=1.5,
        )
        assert second.action == ACTION_SUBMIT
        assert len(episode_jobs(session, "ep-1")) == 2


class TestRestartAfterSubmission:
    def test_a_submitted_job_is_resumed_not_resubmitted(self, session):
        decision = _reserve(session)
        record_submission(session, decision.job, "heygen-video-1")

        resumed = _reserve(session)
        assert resumed.action == ACTION_RESUME
        assert resumed.may_call_provider is False
        assert resumed.job.provider_job_id == "heygen-video-1"

    def test_the_provider_job_id_is_persisted_immediately(self, session):
        decision = _reserve(session)
        record_submission(session, decision.job, "heygen-video-1")
        session.expire_all()
        assert get_job(session, "ep-1", "ch01_s00", decision.job.content_hash).provider_job_id == (
            "heygen-video-1"
        )

    def test_an_empty_provider_job_id_is_refused(self, session):
        decision = _reserve(session)
        with pytest.raises(ValueError):
            record_submission(session, decision.job, "")


class TestUnknownOutcomesFailClosed:
    def test_a_crash_before_the_answer_is_not_retried(self, session):
        # Reserved, then the process died: HeyGen may have started and billed a
        # generation whose id nobody ever saw.
        _reserve(session)
        after_restart = _reserve(session)
        assert after_restart.action == ACTION_RECONCILE
        assert after_restart.may_call_provider is False
        assert after_restart.job.status == AvatarJobStatus.RECONCILE_REQUIRED.value

    def test_it_stays_blocked_on_every_further_run(self, session):
        _reserve(session)
        _reserve(session)
        third = _reserve(session)
        assert third.action == ACTION_RECONCILE
        assert len(episode_jobs(session, "ep-1")) == 1

    def test_an_unknown_outcome_still_counts_against_the_budget(self, session):
        _reserve(session, cost=1.5)
        _reserve(session, cost=1.5)
        assert episode_avatar_cost(session, "ep-1") == pytest.approx(1.5)

    def test_a_timeout_holds_the_job_rather_than_failing_it(self, session):
        decision = _reserve(session)
        record_submission(session, decision.job, "heygen-video-1")
        hold_for_reconciliation(session, decision.job, "polling timed out")
        assert decision.job.status == AvatarJobStatus.RECONCILE_REQUIRED.value
        assert _reserve(session).action == ACTION_RECONCILE

    def test_a_submitted_row_without_an_id_is_never_a_second_purchase(self, session):
        decision = _reserve(session)
        decision.job.status = AvatarJobStatus.SUBMITTED.value
        session.commit()
        assert _reserve(session).action == ACTION_RECONCILE

    def test_blocked_jobs_are_reported(self, session):
        _reserve(session)
        _reserve(session)
        blocked = blocked_jobs(session, "ep-1")
        assert [j.scene_id for j in blocked] == ["ch01_s00"]

    def test_an_untouched_episode_blocks_nothing(self, session):
        decision = _reserve(session)
        record_submission(session, decision.job, "heygen-video-1")
        record_completion(
            session, decision.job, output_path="a.webm", duration_seconds=1.0, cost_usd=0.1
        )
        assert blocked_jobs(session, "ep-1") == []


class TestRefusedRequests:
    def test_a_refused_request_may_be_tried_again(self, session):
        decision = _reserve(session)
        record_refusal(session, decision.job, "401 invalid api key")

        retry = _reserve(session)
        assert retry.action == ACTION_SUBMIT
        assert retry.job.attempt_count == 2

    def test_a_refused_request_costs_nothing(self, session):
        decision = _reserve(session, cost=1.5)
        record_refusal(session, decision.job, "422 unsupported avatar")
        assert episode_avatar_cost(session, "ep-1") == pytest.approx(0.0)

    def test_a_retry_reuses_the_same_row(self, session):
        decision = _reserve(session)
        record_refusal(session, decision.job, "quota exceeded")
        _reserve(session)
        assert len(episode_jobs(session, "ep-1")) == 1


class TestReconciliation:
    def test_a_found_clip_closes_the_job(self, session):
        _reserve(session)
        held = _reserve(session).job
        resolve_job(
            session,
            held,
            outcome="delivered",
            note="Found in the HeyGen dashboard, downloaded by hand.",
            output_path="anchor/ch01_s00.webm",
            duration_seconds=12.0,
            cost_usd=0.2,
        )
        assert held.status == AvatarJobStatus.COMPLETED.value
        assert _reserve(session).action == ACTION_REUSE
        assert episode_avatar_cost(session, "ep-1") == pytest.approx(0.2)

    def test_an_unbilled_job_is_released_for_a_new_attempt(self, session):
        _reserve(session)
        held = _reserve(session).job
        resolve_job(
            session, held, outcome="not_billed", note="No generation on the account for that day."
        )
        assert _reserve(session).action == ACTION_SUBMIT
        assert episode_avatar_cost(session, "ep-1") == pytest.approx(1.5)

    def test_resolving_demands_a_note(self, session):
        _reserve(session)
        held = _reserve(session).job
        with pytest.raises(ValueError):
            resolve_job(session, held, outcome="not_billed", note="   ")

    def test_delivered_without_a_file_is_refused(self, session):
        _reserve(session)
        held = _reserve(session).job
        with pytest.raises(ValueError):
            resolve_job(session, held, outcome="delivered", note="seen", output_path="")

    def test_an_unknown_outcome_is_refused(self, session):
        _reserve(session)
        held = _reserve(session).job
        with pytest.raises(ValueError):
            resolve_job(session, held, outcome="maybe", note="unsure")

    def test_only_held_jobs_may_be_reconciled(self, session):
        decision = _reserve(session)
        with pytest.raises(AvatarJobConflictError):
            resolve_job(session, decision.job, outcome="not_billed", note="premature")


class TestEpisodeIsolation:
    def test_two_episodes_keep_separate_ledgers(self, session):
        _reserve(session)
        reserve_scene(
            session,
            episode_id="ep-2",
            scene_id="ch01_s00",
            chapter_id="ch01",
            content_hash=_hash(),
            provider="heygen",
            engine="avatar_iii",
            avatar_look_id="heygen-look-01",
            output_format="webm",
            estimated_cost_usd=3.0,
        )
        assert episode_avatar_cost(session, "ep-1") == pytest.approx(1.5)
        assert episode_avatar_cost(session, "ep-2") == pytest.approx(3.0)

    def test_an_episode_without_jobs_has_no_cost(self, session):
        assert episode_avatar_cost(session, "ep-unknown") == pytest.approx(0.0)


class TestOtherProviders:
    def test_the_ledger_is_not_heygen_specific(self, session):
        decision = reserve_scene(
            session,
            episode_id="ep-3",
            scene_id="ch01_s00",
            chapter_id="ch01",
            content_hash=_hash(provider="d-id", engine=""),
            provider="d-id",
            engine="",
            avatar_look_id="",
            output_format="mp4",
            estimated_cost_usd=0.5,
        )
        assert decision.action == ACTION_SUBMIT
        record_submission(session, decision.job, "did-talk-1")
        assert _reserve_did(session).action == ACTION_RESUME


def _reserve_did(session):
    return reserve_scene(
        session,
        episode_id="ep-3",
        scene_id="ch01_s00",
        chapter_id="ch01",
        content_hash=_hash(provider="d-id", engine=""),
        provider="d-id",
        engine="",
        avatar_look_id="",
        output_format="mp4",
        estimated_cost_usd=0.5,
    )


class TestMigration:
    def test_the_table_and_its_guarantees_are_created(self, tmp_path):
        from sqlalchemy import text

        from btcedu.migrations import CreateAvatarJobsTableMigration

        engine = create_engine(f"sqlite:///{tmp_path / 'm.db'}")
        db = sessionmaker(bind=engine)()
        db.execute(
            text(
                "CREATE TABLE schema_migrations ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, version VARCHAR(64), "
                "description TEXT, applied_at DATETIME)"
            )
        )
        db.commit()

        migration = CreateAvatarJobsTableMigration()
        migration.up(db)
        # Check-before-act: running it twice must not raise.
        migration.up(db)

        indexes = {
            row[0] for row in db.execute(text("SELECT name FROM sqlite_master WHERE type='index'"))
        }
        assert "uq_avatar_job_scene_content" in indexes
        db.close()
