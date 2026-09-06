"""The avatar job ledger: what may be bought, what must not be bought twice.

Every provider call for an avatar clip goes through here. The rule it enforces
is narrow and absolute: a clip that has already been paid for is never bought
again, and a clip whose outcome is unknown is never bought again *automatically*.

The sequence around a single call is:

    decision = reserve_scene(...)      # durable row exists before the call
    if decision.action == "submit":
        job_id = provider.start(...)
        record_submission(session, decision.job, job_id)   # id persisted at once
        ...
        record_completion(session, decision.job, ...)

A crash between ``reserve_scene`` and ``record_submission`` leaves a ``reserved``
row. On the next run that row is not retried — it becomes ``reconcile_required``,
because HeyGen may well have started (and billed) the generation whose id we
never saw. Resolving it is an operator decision, the same way an ambiguous
YouTube upload is.
"""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from btcedu.models.avatar_job import AvatarJob, AvatarJobStatus

logger = logging.getLogger(__name__)

# What reserve_scene() tells the caller to do.
ACTION_SUBMIT = "submit"  # nothing bought yet, go ahead
ACTION_REUSE = "reuse"  # already completed, use the file on disk
ACTION_RESUME = "resume"  # already submitted, poll this provider job id
ACTION_RECONCILE = "reconcile"  # outcome unknown, an operator must decide


class AvatarJobConflictError(RuntimeError):
    """Raised when work is demanded that the ledger refuses to authorise."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class ReservationDecision:
    """What the ledger permits for one scene."""

    action: str
    job: AvatarJob
    reason: str = ""

    @property
    def may_call_provider(self) -> bool:
        return self.action == ACTION_SUBMIT


def compute_job_hash(
    *,
    scene_id: str,
    text_hash: str,
    audio_hash: str,
    avatar_look_id: str,
    provider: str,
    engine: str,
    output_format: str,
    resolution: str,
    aspect_ratio: str,
) -> str:
    """Fingerprint one clip's identity.

    Includes the audio hash rather than only the text: the same words
    resynthesised are a different performance of a different length, and the
    clip has to be lip-synced to the take that will actually be in the video.
    """
    payload = json.dumps(
        {
            "scene_id": scene_id,
            "text_hash": text_hash,
            "audio_hash": audio_hash,
            "avatar_look_id": avatar_look_id,
            "provider": provider,
            "engine": engine,
            "output_format": output_format,
            "resolution": resolution,
            "aspect_ratio": aspect_ratio,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_job(
    session: Session, episode_id: str, scene_id: str, content_hash: str
) -> AvatarJob | None:
    return (
        session.query(AvatarJob)
        .filter(
            AvatarJob.episode_id == episode_id,
            AvatarJob.scene_id == scene_id,
            AvatarJob.content_hash == content_hash,
        )
        .first()
    )


def episode_jobs(session: Session, episode_id: str) -> list[AvatarJob]:
    return (
        session.query(AvatarJob)
        .filter(AvatarJob.episode_id == episode_id)
        .order_by(AvatarJob.reserved_at, AvatarJob.id)
        .all()
    )


def episode_avatar_cost(session: Session, episode_id: str) -> float:
    """Everything this episode has spent on avatar clips.

    Reserved and reconcile-required rows count. Their outcome is unknown, and a
    budget that assumed unknown meant free would authorise spending twice.
    """
    total = (
        session.query(func.coalesce(func.sum(AvatarJob.cost_usd), 0.0))
        .filter(AvatarJob.episode_id == episode_id)
        .scalar()
    )
    return float(total or 0.0)


def blocked_jobs(session: Session, episode_id: str) -> list[AvatarJob]:
    """Rows an operator has to resolve before the stage can finish."""
    return [
        job
        for job in episode_jobs(session, episode_id)
        if job.status
        in {AvatarJobStatus.RECONCILE_REQUIRED.value, AvatarJobStatus.ABANDONED.value}
    ]


def unresolved_jobs(session: Session, episode_id: str | None = None) -> list[AvatarJob]:
    """Every row whose outcome nobody has established yet.

    ``reserved`` is included even though it is not yet formally held: a row that
    has been sitting in ``reserved`` since a crash is precisely what an operator
    needs to see, and pretending it is fine until the next run promotes it would
    hide the one state that can cost money twice.
    """
    query = session.query(AvatarJob).filter(
        AvatarJob.status.in_(
            [
                AvatarJobStatus.RESERVED.value,
                AvatarJobStatus.RECONCILE_REQUIRED.value,
            ]
        )
    )
    if episode_id:
        query = query.filter(AvatarJob.episode_id == episode_id)
    return query.order_by(AvatarJob.reserved_at, AvatarJob.id).all()


def reserve_scene(
    session: Session,
    *,
    episode_id: str,
    scene_id: str,
    chapter_id: str,
    content_hash: str,
    provider: str,
    engine: str,
    avatar_look_id: str,
    output_format: str,
    estimated_cost_usd: float,
) -> ReservationDecision:
    """Claim the right to generate one clip, durably, before any call is made."""
    existing = get_job(session, episode_id, scene_id, content_hash)

    if existing is not None:
        if existing.status == AvatarJobStatus.COMPLETED.value:
            return ReservationDecision(ACTION_REUSE, existing, "already generated")

        if existing.status == AvatarJobStatus.SUBMITTED.value:
            if existing.provider_job_id:
                return ReservationDecision(
                    ACTION_RESUME, existing, f"job {existing.provider_job_id} already submitted"
                )
            # Submitted without an id cannot happen through this module, but a
            # hand-edited row must not become a second purchase.
            return _hold_for_reconciliation(
                session, existing, "submitted without a provider job id"
            )

        if existing.status == AvatarJobStatus.RESERVED.value:
            # The previous run died between the reservation and the provider's
            # answer. Whether it was billed is exactly what nobody knows.
            return _hold_for_reconciliation(
                session, existing, "reserved but never confirmed by the provider"
            )

        if existing.status == AvatarJobStatus.RECONCILE_REQUIRED.value:
            return ReservationDecision(
                ACTION_RECONCILE, existing, existing.error_message or "awaiting reconciliation"
            )

        if existing.status == AvatarJobStatus.ABANDONED.value:
            # An operator gave this clip up knowing it might have been billed.
            # Buying it again behind their back is exactly what they decided
            # against, so the stage stops instead.
            return ReservationDecision(
                ACTION_RECONCILE,
                existing,
                existing.resolution_note or "abandoned by an operator",
            )

        # FAILED: the provider refused before generating, so nothing was billed
        # and the same row may be tried again.
        existing.status = AvatarJobStatus.RESERVED.value
        existing.reserved_at = _utcnow()
        existing.attempt_count += 1
        existing.error_message = None
        existing.cost_usd = float(estimated_cost_usd)
        session.commit()
        return ReservationDecision(ACTION_SUBMIT, existing, "retry after a refused request")

    job = AvatarJob(
        episode_id=episode_id,
        scene_id=scene_id,
        chapter_id=chapter_id,
        content_hash=content_hash,
        provider=provider,
        engine=engine,
        avatar_look_id=avatar_look_id,
        output_format=output_format,
        status=AvatarJobStatus.RESERVED.value,
        # Charged from the moment of reservation, not on completion: a crash
        # must not make the spend invisible to the next budget check.
        cost_usd=float(estimated_cost_usd),
        attempt_count=1,
        reserved_at=_utcnow(),
    )
    session.add(job)
    session.commit()
    return ReservationDecision(ACTION_SUBMIT, job, "new clip")


def _hold_for_reconciliation(session: Session, job: AvatarJob, reason: str) -> ReservationDecision:
    job.status = AvatarJobStatus.RECONCILE_REQUIRED.value
    job.error_message = reason
    session.commit()
    logger.warning(
        "Avatar job %s/%s held for reconciliation: %s", job.episode_id, job.scene_id, reason
    )
    return ReservationDecision(ACTION_RECONCILE, job, reason)


def record_submission(session: Session, job: AvatarJob, provider_job_id: str) -> AvatarJob:
    """Persist the provider's job id before anything else can fail."""
    if not provider_job_id:
        raise ValueError("provider_job_id must not be empty")
    job.provider_job_id = provider_job_id
    job.status = AvatarJobStatus.SUBMITTED.value
    job.submitted_at = _utcnow()
    session.commit()
    return job


def record_completion(
    session: Session,
    job: AvatarJob,
    *,
    output_path: str,
    duration_seconds: float,
    cost_usd: float,
) -> AvatarJob:
    job.status = AvatarJobStatus.COMPLETED.value
    job.output_path = output_path
    job.duration_seconds = float(duration_seconds)
    job.cost_usd = float(cost_usd)
    job.completed_at = _utcnow()
    job.error_message = None
    session.commit()
    return job


def record_refusal(session: Session, job: AvatarJob, error: str) -> AvatarJob:
    """The provider rejected the request without generating anything.

    Only for errors that are known not to be billed — authentication, quota,
    validation. Anything ambiguous belongs in ``hold_for_reconciliation``.
    """
    job.status = AvatarJobStatus.FAILED.value
    job.error_message = str(error)[:1000]
    job.completed_at = _utcnow()
    job.cost_usd = 0.0
    session.commit()
    return job


def hold_for_reconciliation(session: Session, job: AvatarJob, reason: str) -> AvatarJob:
    """A timeout or an interrupted download: the clip may exist and be billed."""
    return _hold_for_reconciliation(session, job, str(reason)[:1000]).job


#: Outcomes an operator may record against a job whose fate was unknown.
OUTCOME_DELIVERED = "delivered"
OUTCOME_NOT_BILLED = "not_billed"
OUTCOME_IN_PROGRESS = "in_progress"
OUTCOME_ABANDONED = "abandoned"
RESOLUTION_OUTCOMES = (
    OUTCOME_DELIVERED,
    OUTCOME_NOT_BILLED,
    OUTCOME_IN_PROGRESS,
    OUTCOME_ABANDONED,
)


def resolve_job(
    session: Session,
    job: AvatarJob,
    *,
    outcome: str,
    note: str,
    output_path: str = "",
    duration_seconds: float = 0.0,
    cost_usd: float | None = None,
    provider_job_id: str = "",
) -> AvatarJob:
    """Operator resolution of an unknown outcome.

    ``delivered`` closes the row against a clip that was found and paid for;
    ``not_billed`` releases it for a fresh attempt; ``in_progress`` hands it back
    to the poller because the provider confirmed it is still generating; and
    ``abandoned`` gives the clip up without pretending it was free. All four
    demand a note, because the whole point of the state is that only a human
    knows which it was.

    The transition itself is a single conditional UPDATE. Two operators
    resolving the same row at the same time is not hypothetical — it is what
    happens when one of them is a cron-driven dashboard — and the loser has to
    be told rather than silently overwrite the winner.
    """
    if job.status != AvatarJobStatus.RECONCILE_REQUIRED.value:
        raise AvatarJobConflictError(
            f"Avatar job {job.scene_id} is '{job.status}', not awaiting reconciliation"
        )
    if not note.strip():
        raise ValueError("Reconciliation requires a note describing the finding")
    if outcome not in RESOLUTION_OUTCOMES:
        raise ValueError(f"Unknown reconciliation outcome: {outcome!r}")

    now = _utcnow()
    changes: dict = {
        AvatarJob.resolution_note: note.strip()[:1000],
        AvatarJob.error_message: None,
    }

    if outcome == OUTCOME_DELIVERED:
        if not output_path:
            raise ValueError("Resolving as delivered requires the path of the clip")
        changes[AvatarJob.status] = AvatarJobStatus.COMPLETED.value
        changes[AvatarJob.output_path] = output_path
        changes[AvatarJob.duration_seconds] = float(duration_seconds)
        changes[AvatarJob.completed_at] = now
        if cost_usd is not None:
            changes[AvatarJob.cost_usd] = float(cost_usd)
    elif outcome == OUTCOME_NOT_BILLED:
        changes[AvatarJob.status] = AvatarJobStatus.FAILED.value
        changes[AvatarJob.cost_usd] = 0.0
        changes[AvatarJob.completed_at] = now
    elif outcome == OUTCOME_IN_PROGRESS:
        resolved_job_id = (provider_job_id or job.provider_job_id or "").strip()
        if not resolved_job_id:
            raise ValueError(
                "Resolving as in_progress requires the provider job id that is still running"
            )
        changes[AvatarJob.status] = AvatarJobStatus.SUBMITTED.value
        changes[AvatarJob.provider_job_id] = resolved_job_id
        changes[AvatarJob.submitted_at] = job.submitted_at or now
        if cost_usd is not None:
            changes[AvatarJob.cost_usd] = float(cost_usd)
    else:  # OUTCOME_ABANDONED
        # The cost is deliberately left alone. Abandoning a clip is not a
        # statement that it was free, and the budget must keep assuming it was
        # billed until somebody proves otherwise.
        changes[AvatarJob.status] = AvatarJobStatus.ABANDONED.value
        changes[AvatarJob.completed_at] = now
        if cost_usd is not None:
            changes[AvatarJob.cost_usd] = float(cost_usd)

    updated = (
        session.query(AvatarJob)
        .filter(
            AvatarJob.id == job.id,
            AvatarJob.status == AvatarJobStatus.RECONCILE_REQUIRED.value,
        )
        .update(changes, synchronize_session=False)
    )
    if updated == 0:
        session.rollback()
        raise AvatarJobConflictError(
            f"Avatar job {job.scene_id} was resolved by someone else while this "
            "reconciliation was being prepared"
        )
    session.commit()
    session.refresh(job)
    logger.info(
        "Avatar job %s/%s reconciled as %s", job.episode_id, job.scene_id, outcome
    )
    return job
