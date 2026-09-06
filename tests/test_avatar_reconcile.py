"""Operator reconciliation: only provable transitions, always on the record.

These tests exercise the half of the ledger a human drives. The recurring
question is the same as in the ledger's own tests — can this path cause a
second charge? — with one addition: does the row that was touched carry a
trail afterwards.

Nothing here contacts a provider.
"""

import pytest
from click.testing import CliRunner
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.core.avatar_jobs import (
    ACTION_RECONCILE,
    ACTION_SUBMIT,
    episode_avatar_cost,
    hold_for_reconciliation,
    record_completion,
    record_submission,
    reserve_scene,
)
from btcedu.core.avatar_reconcile import (
    DECISION_ABANDON,
    DECISION_DELIVERED,
    DECISION_NOT_BILLED,
    DECISION_RUNNING,
    DECISION_UNRESOLVED,
    ReconciliationError,
    attach_provider_job_id,
    audit_entries,
    inspect_job,
    list_jobs,
    next_safe_action,
    resolve,
)
from btcedu.db import Base
from btcedu.models.avatar_job import AvatarJob, AvatarJobStatus
from btcedu.models.avatar_job_audit import AvatarAuditAction, AvatarJobAudit


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    db = factory()
    yield db
    db.close()


def _reserve(session, *, scene_id="ch01_s00", episode_id="ep1", cost=1.5) -> AvatarJob:
    decision = reserve_scene(
        session,
        episode_id=episode_id,
        scene_id=scene_id,
        chapter_id="ch01",
        content_hash=f"hash-{episode_id}-{scene_id}",
        provider="heygen",
        engine="avatar_iii",
        avatar_look_id="look-1",
        output_format="webm",
        estimated_cost_usd=cost,
    )
    return decision.job


def _held(session, **kwargs) -> AvatarJob:
    """A job in the one state these decisions apply to."""
    job = _reserve(session, **kwargs)
    hold_for_reconciliation(session, job, "run died before the provider answered")
    return job


class TestListing:
    def test_only_unresolved_jobs_are_listed_by_default(self, session):
        held = _held(session, scene_id="ch01_s00")
        done = _reserve(session, scene_id="ch01_s01")
        record_submission(session, done, "prov-1")
        record_completion(session, done, output_path="a.webm", duration_seconds=4.0, cost_usd=1.0)

        views = list_jobs(session)

        assert [v.scene_id for v in views] == [held.scene_id]

    def test_everything_can_be_listed_on_request(self, session):
        _held(session, scene_id="ch01_s00")
        _reserve(session, scene_id="ch01_s01")

        assert len(list_jobs(session, unresolved_only=False)) == 2

    def test_a_reserved_job_counts_as_unresolved(self, session):
        _reserve(session, scene_id="ch01_s00")

        assert [v.status for v in list_jobs(session)] == [AvatarJobStatus.RESERVED.value]

    def test_listing_can_be_narrowed_to_one_episode(self, session):
        _held(session, episode_id="ep1")
        _held(session, episode_id="ep2")

        views = list_jobs(session, episode_id="ep2")

        assert [v.episode_id for v in views] == ["ep2"]

    def test_a_view_carries_what_an_operator_needs(self, session):
        _held(session, cost=2.25)

        view = list_jobs(session)[0].to_dict()

        assert view["episode_id"] == "ep1"
        assert view["scene_id"] == "ch01_s00"
        assert view["reserved_cost_usd"] == 2.25
        assert view["age_hours"] >= 0.0
        assert "look_id" in view

    def test_a_view_exposes_no_local_paths(self, session):
        job = _held(session)
        job.output_path = "/srv/private/outputs/ep1/anchor/clip.webm"
        session.commit()

        assert "/srv/private" not in str(list_jobs(session)[0].to_dict())


class TestInspect:
    def test_inspect_reports_local_facts_only(self, session, tmp_path):
        _held(session)
        job_id = list_jobs(session)[0].job_id

        report = inspect_job(session, job_id, outputs_dir=tmp_path)

        assert report["schema_version"] == 1
        assert report["job"]["scene_id"] == "ch01_s00"
        assert report["hashes"]["content_hash"] == "hash-ep1-ch01_s00"
        assert report["files"]["output_exists"] is False
        assert report["manifest_status"] == "absent"
        assert report["next_safe_action"]

    def test_inspect_finds_a_downloaded_clip(self, session, tmp_path):
        job = _held(session)
        clip = tmp_path / "ep1" / "anchor" / "clip.webm"
        clip.parent.mkdir(parents=True)
        clip.write_bytes(b"x" * 32)
        job.output_path = "ep1/anchor/clip.webm"
        session.commit()

        report = inspect_job(session, int(job.id), outputs_dir=tmp_path)

        assert report["files"]["output_exists"] is True

    def test_inspect_refuses_an_unknown_id(self, session):
        with pytest.raises(ReconciliationError):
            inspect_job(session, 4711)

    def test_the_safe_action_never_suggests_reordering(self, session):
        job = _held(session)

        advice = next_safe_action(job)

        assert "not-billed" in advice or "unresolved" in advice
        assert "retry" not in advice.lower()

    def test_a_completed_job_needs_no_action(self, session):
        job = _reserve(session)
        record_submission(session, job, "prov-1")
        record_completion(session, job, output_path="a.webm", duration_seconds=4.0, cost_usd=1.0)

        assert "Nothing" in next_safe_action(job)


class TestPermittedTransitions:
    def test_a_confirmed_running_job_returns_to_the_poller(self, session):
        job = _held(session)

        resolve(
            session,
            int(job.id),
            decision=DECISION_RUNNING,
            note="dashboard shows it generating",
            operator_ref="ops-1",
            provider_job_id="prov-77",
        )

        assert job.status == AvatarJobStatus.SUBMITTED.value
        assert job.provider_job_id == "prov-77"

    def test_a_delivered_clip_is_closed_against_a_real_file(self, session, tmp_path):
        job = _held(session)
        clip = tmp_path / "clip.webm"
        clip.write_bytes(b"x" * 64)

        resolve(
            session,
            int(job.id),
            decision=DECISION_DELIVERED,
            note="downloaded from the dashboard",
            operator_ref="ops-1",
            output_path=str(clip),
            duration_seconds=6.5,
        )

        assert job.status == AvatarJobStatus.COMPLETED.value
        assert job.duration_seconds == 6.5

    def test_a_delivered_claim_without_a_file_is_refused(self, session, tmp_path):
        job = _held(session)

        with pytest.raises(ReconciliationError, match="No clip"):
            resolve(
                session,
                int(job.id),
                decision=DECISION_DELIVERED,
                note="trust me",
                operator_ref="ops-1",
                output_path=str(tmp_path / "absent.webm"),
            )
        assert job.status == AvatarJobStatus.RECONCILE_REQUIRED.value

    def test_an_empty_clip_is_refused(self, session, tmp_path):
        job = _held(session)
        clip = tmp_path / "clip.webm"
        clip.write_bytes(b"")

        with pytest.raises(ReconciliationError, match="empty"):
            resolve(
                session,
                int(job.id),
                decision=DECISION_DELIVERED,
                note="downloaded",
                operator_ref="ops-1",
                output_path=str(clip),
            )

    def test_a_confirmed_refusal_releases_the_row_and_the_money(self, session):
        job = _held(session)
        attach_provider_job_id(
            session,
            int(job.id),
            provider_job_id="prov-9",
            operator_ref="ops-1",
            note="found in the dashboard",
            confirm=True,
        )

        resolve(
            session,
            int(job.id),
            decision=DECISION_NOT_BILLED,
            note="provider says the request was rejected and not charged",
            operator_ref="ops-1",
        )

        assert job.status == AvatarJobStatus.FAILED.value
        assert job.cost_usd == 0.0
        assert episode_avatar_cost(session, "ep1") == 0.0

    def test_only_a_confirmed_refusal_lets_the_stage_buy_again(self, session):
        job = _held(session)
        resolve(
            session,
            int(job.id),
            decision=DECISION_NOT_BILLED,
            note="provider rejected the request",
            operator_ref="ops-1",
            provider_job_id="prov-9",
        )

        decision = reserve_scene(
            session,
            episode_id="ep1",
            scene_id="ch01_s00",
            chapter_id="ch01",
            content_hash="hash-ep1-ch01_s00",
            provider="heygen",
            engine="avatar_iii",
            avatar_look_id="look-1",
            output_format="webm",
            estimated_cost_usd=1.5,
        )

        assert decision.action == ACTION_SUBMIT


class TestFailClosedRules:
    def test_an_unidentifiable_job_stays_blocked(self, session):
        job = _held(session)

        resolve(
            session,
            int(job.id),
            decision=DECISION_UNRESOLVED,
            note="nothing in the dashboard matches this scene",
            operator_ref="ops-1",
        )

        assert job.status == AvatarJobStatus.RECONCILE_REQUIRED.value
        assert audit_entries(session, int(job.id))[-1].action == AvatarAuditAction.UNRESOLVED.value

    def test_a_missing_generation_is_not_evidence_of_a_free_one(self, session):
        job = _held(session)

        # A 404 weeks later only proves the retention window expired.
        with pytest.raises(ReconciliationError, match="not evidence"):
            resolve(
                session,
                int(job.id),
                decision=DECISION_NOT_BILLED,
                note="the dashboard returns 404 for this scene",
                operator_ref="ops-1",
            )
        assert job.status == AvatarJobStatus.RECONCILE_REQUIRED.value

    def test_abandoning_keeps_the_cost_on_the_episode(self, session):
        job = _held(session, cost=2.0)

        resolve(
            session,
            int(job.id),
            decision=DECISION_ABANDON,
            note="deadline passed, cutting the scene",
            operator_ref="ops-1",
        )

        assert job.status == AvatarJobStatus.ABANDONED.value
        assert episode_avatar_cost(session, "ep1") == 2.0

    def test_an_abandoned_clip_is_never_bought_again(self, session):
        job = _held(session)
        resolve(
            session,
            int(job.id),
            decision=DECISION_ABANDON,
            note="cutting the scene",
            operator_ref="ops-1",
        )

        decision = reserve_scene(
            session,
            episode_id="ep1",
            scene_id="ch01_s00",
            chapter_id="ch01",
            content_hash="hash-ep1-ch01_s00",
            provider="heygen",
            engine="avatar_iii",
            avatar_look_id="look-1",
            output_format="webm",
            estimated_cost_usd=1.5,
        )

        assert decision.action == ACTION_RECONCILE

    def test_an_abandoned_decision_is_final(self, session):
        job = _held(session)
        resolve(
            session,
            int(job.id),
            decision=DECISION_ABANDON,
            note="cutting the scene",
            operator_ref="ops-1",
        )

        with pytest.raises(ReconciliationError, match="final"):
            resolve(
                session,
                int(job.id),
                decision=DECISION_RUNNING,
                note="changed my mind",
                operator_ref="ops-1",
                provider_job_id="prov-1",
            )

    def test_a_settled_job_is_never_reopened(self, session, tmp_path):
        job = _reserve(session)
        record_submission(session, job, "prov-1")
        record_completion(session, job, output_path="a.webm", duration_seconds=4.0, cost_usd=1.0)

        with pytest.raises(ReconciliationError, match="never reopened"):
            resolve(
                session,
                int(job.id),
                decision=DECISION_ABANDON,
                note="second thoughts",
                operator_ref="ops-1",
            )

    def test_a_still_reserved_job_is_not_resolvable_yet(self, session):
        job = _reserve(session)

        with pytest.raises(ReconciliationError, match="reserved"):
            resolve(
                session,
                int(job.id),
                decision=DECISION_UNRESOLVED,
                note="looking into it",
                operator_ref="ops-1",
            )

    def test_every_resolution_needs_a_reason(self, session):
        job = _held(session)

        with pytest.raises(ReconciliationError, match="reason"):
            resolve(
                session, int(job.id), decision=DECISION_UNRESOLVED, note="  ", operator_ref="ops-1"
            )

    def test_every_resolution_needs_an_operator_reference(self, session):
        job = _held(session)

        with pytest.raises(ReconciliationError, match="operator"):
            resolve(
                session, int(job.id), decision=DECISION_UNRESOLVED, note="checked", operator_ref=""
            )

    def test_an_unknown_decision_is_refused(self, session):
        job = _held(session)

        with pytest.raises(ReconciliationError, match="Unknown decision"):
            resolve(
                session, int(job.id), decision="retry", note="just do it", operator_ref="ops-1"
            )

    def test_a_lost_race_is_reported_not_overwritten(self, session, tmp_path):
        job = _held(session)
        clip = tmp_path / "clip.webm"
        clip.write_bytes(b"x" * 32)

        # A second operator (or the dashboard) settles the row first.
        session.query(AvatarJob).filter(AvatarJob.id == job.id).update(
            {AvatarJob.status: AvatarJobStatus.COMPLETED.value}, synchronize_session=False
        )
        session.commit()

        with pytest.raises(ReconciliationError):
            resolve(
                session,
                int(job.id),
                decision=DECISION_DELIVERED,
                note="downloaded",
                operator_ref="ops-2",
                output_path=str(clip),
            )


class TestManualAttachment:
    def test_attaching_requires_confirmation(self, session):
        job = _held(session)

        with pytest.raises(ReconciliationError, match="confirmation"):
            attach_provider_job_id(
                session,
                int(job.id),
                provider_job_id="prov-9",
                operator_ref="ops-1",
                note="found it",
            )

    def test_attaching_requires_a_note(self, session):
        job = _held(session)

        with pytest.raises(ReconciliationError, match="note"):
            attach_provider_job_id(
                session,
                int(job.id),
                provider_job_id="prov-9",
                operator_ref="ops-1",
                note="",
                confirm=True,
            )

    def test_a_confirmed_attachment_is_audited(self, session):
        job = _held(session)

        attach_provider_job_id(
            session,
            int(job.id),
            provider_job_id="prov-9",
            operator_ref="ops-1",
            note="matched by episode and duration in the dashboard",
            confirm=True,
        )

        entries = audit_entries(session, int(job.id))
        assert job.provider_job_id == "prov-9"
        assert entries[-1].action == AvatarAuditAction.ATTACH_JOB_ID.value
        assert entries[-1].provider_job_id == "prov-9"

    def test_an_existing_job_id_is_not_silently_replaced(self, session):
        job = _held(session)
        job.provider_job_id = "prov-1"
        session.commit()

        with pytest.raises(ReconciliationError, match="already carries"):
            attach_provider_job_id(
                session,
                int(job.id),
                provider_job_id="prov-2",
                operator_ref="ops-1",
                note="different id",
                confirm=True,
            )

    def test_a_settled_job_takes_no_attachment(self, session):
        job = _reserve(session)
        record_submission(session, job, "prov-1")
        record_completion(session, job, output_path="a.webm", duration_seconds=4.0, cost_usd=1.0)

        with pytest.raises(ReconciliationError, match="held job"):
            attach_provider_job_id(
                session,
                int(job.id),
                provider_job_id="prov-2",
                operator_ref="ops-1",
                note="regrouping",
                confirm=True,
            )


class TestAudit:
    def test_every_resolution_leaves_a_trail(self, session, tmp_path):
        job = _held(session, cost=2.0)
        clip = tmp_path / "clip.webm"
        clip.write_bytes(b"x" * 32)

        resolve(
            session,
            int(job.id),
            decision=DECISION_DELIVERED,
            note="collected from the dashboard",
            operator_ref="ops-7",
            output_path=str(clip),
            duration_seconds=5.0,
            cost_usd=1.9,
        )

        entry = audit_entries(session, int(job.id))[-1]
        assert entry.action == AvatarAuditAction.CONFIRM_DELIVERED.value
        assert entry.from_status == AvatarJobStatus.RECONCILE_REQUIRED.value
        assert entry.to_status == AvatarJobStatus.COMPLETED.value
        assert entry.operator_ref == "ops-7"
        assert entry.cost_before_usd == 2.0
        assert entry.cost_after_usd == 1.9

    def test_the_trail_keeps_every_step_in_order(self, session):
        job = _held(session)
        attach_provider_job_id(
            session,
            int(job.id),
            provider_job_id="prov-9",
            operator_ref="ops-1",
            note="found it",
            confirm=True,
        )
        resolve(
            session,
            int(job.id),
            decision=DECISION_RUNNING,
            note="still generating",
            operator_ref="ops-1",
        )

        actions = [e.action for e in audit_entries(session, int(job.id))]
        assert actions == [
            AvatarAuditAction.ATTACH_JOB_ID.value,
            AvatarAuditAction.CONFIRM_RUNNING.value,
        ]

    def test_a_refused_resolution_writes_no_audit_row(self, session):
        job = _held(session)

        with pytest.raises(ReconciliationError):
            resolve(
                session,
                int(job.id),
                decision=DECISION_NOT_BILLED,
                note="404 in the dashboard",
                operator_ref="ops-1",
            )

        assert session.query(AvatarJobAudit).count() == 0


class _NoCloseSession:
    """Hands the CLI the test session but keeps it open afterwards."""

    def __init__(self, session):
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    def close(self):
        return None


class TestTheCommandLine:
    def _invoke(self, session, args):
        from btcedu import cli as cli_module
        from btcedu.config import Settings

        # The CLI accepts an injected factory, so no test ever opens the real
        # production database.
        class _Factory:
            def __call__(self):
                return _NoCloseSession(session)

        return CliRunner().invoke(
            cli_module.cli,
            args,
            obj={"settings": Settings(), "session_factory": _Factory()},
        )

    def test_list_reports_held_jobs_as_json(self, session):
        _held(session)

        result = self._invoke(session, ["avatar-reconcile", "list", "--json"])

        assert result.exit_code == 0
        import json

        payload = json.loads(result.output)
        assert payload["jobs"][0]["scene_id"] == "ch01_s00"

    def test_list_says_so_when_there_is_nothing_to_do(self, session):
        result = self._invoke(session, ["avatar-reconcile", "list"])

        assert result.exit_code == 0
        assert "No" in result.output

    def test_inspect_prints_the_safe_action(self, session):
        job = _held(session)

        result = self._invoke(
            session, ["avatar-reconcile", "inspect", "--job-id", str(job.id)]
        )

        assert result.exit_code == 0
        assert "Next safe action" in result.output

    def test_resolve_refuses_an_unproven_refusal(self, session):
        job = _held(session)

        result = self._invoke(
            session,
            [
                "avatar-reconcile",
                "resolve",
                "--job-id",
                str(job.id),
                "--decision",
                DECISION_NOT_BILLED,
                "--note",
                "404 in the dashboard",
                "--operator-ref",
                "ops-1",
            ],
        )

        assert result.exit_code != 0
        assert "not evidence" in result.output
        assert job.status == AvatarJobStatus.RECONCILE_REQUIRED.value

    def test_resolve_records_a_permitted_decision(self, session):
        job = _held(session)

        result = self._invoke(
            session,
            [
                "avatar-reconcile",
                "resolve",
                "--job-id",
                str(job.id),
                "--decision",
                DECISION_ABANDON,
                "--note",
                "deadline passed, cutting the scene",
                "--operator-ref",
                "ops-1",
            ],
        )

        assert result.exit_code == 0
        session.refresh(job)
        assert job.status == AvatarJobStatus.ABANDONED.value

    def test_attach_requires_the_confirm_flag(self, session):
        job = _held(session)

        result = self._invoke(
            session,
            [
                "avatar-reconcile",
                "attach",
                "--job-id",
                str(job.id),
                "--provider-job-id",
                "prov-9",
                "--operator-ref",
                "ops-1",
                "--note",
                "matched by episode and duration",
            ],
        )

        assert result.exit_code != 0
        session.refresh(job)
        assert not job.provider_job_id
