"""What the avatar stage is doing right now, in operator language.

The ledger records what was decided; this module answers the questions an
operator actually asks while a bulletin is being produced: how many clips are
being generated, how many more may be started, when the next poll happens, what
the provider last complained about, whether the breaker is holding submissions
back and how much money is committed, spent or unaccounted for.

Everything is derived from persisted state, so the answer survives a restart and
is identical in the CLI and in the dashboard. Nothing here performs a provider
call, and nothing here may expose a key, a signed download URL or a payload.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from btcedu.core import avatar_breaker
from btcedu.models.avatar_job import AvatarJob, AvatarJobStatus

#: Statuses that occupy a slot at the provider — the job is out there being
#: worked on and must be counted before another one is started.
ACTIVE_STATUSES = frozenset({AvatarJobStatus.SUBMITTED.value})

#: Statuses whose cost is real and irrecoverable.
SPENT_STATUSES = frozenset({AvatarJobStatus.COMPLETED.value})

#: Statuses whose cost is committed but whose outcome is not yet known.
RESERVED_STATUSES = frozenset(
    {AvatarJobStatus.RESERVED.value, AvatarJobStatus.SUBMITTED.value}
)

#: Statuses whose cost may or may not have been charged. This is the number an
#: operator has to reconcile by hand; it must never be silently dropped.
UNRESOLVED_STATUSES = frozenset({AvatarJobStatus.RECONCILE_REQUIRED.value})


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.isoformat()


def runtime_snapshot(
    session: Session,
    episode_id: str,
    *,
    provider: str,
    max_concurrent_jobs: int = 1,
    engine: str = "",
    studio_mode: str = "",
    max_cost_usd: float | None = None,
    planned_cost_usd: float | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> dict:
    """Summarise the live avatar state of one episode.

    ``max_concurrent_jobs`` comes from the profile, so ``available_slots``
    reflects what the next run would actually be allowed to start rather than a
    theoretical provider maximum.
    """
    jobs = (
        session.query(AvatarJob)
        .filter(AvatarJob.episode_id == episode_id, AvatarJob.provider == provider)
        .order_by(AvatarJob.id)
        .all()
    )

    active = [job for job in jobs if job.status in ACTIVE_STATUSES]
    waiting = [job for job in jobs if job.status == AvatarJobStatus.RESERVED.value]
    unresolved = [job for job in jobs if job.status in UNRESOLVED_STATUSES]

    next_poll = min(
        (job.next_poll_at for job in active if job.next_poll_at is not None),
        default=None,
    )

    # The most recent complaint, not an aggregate: an operator wants to know
    # what the provider said last, not a histogram of a bad afternoon.
    # AvatarJob writes a column per transition rather than a single
    # ``updated_at``; the most recent of them is what "last" means here.
    def _touched(job) -> float:
        stamps = [job.completed_at, job.submitted_at, job.next_poll_at, job.reserved_at]
        moments = [
            stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp
            for stamp in stamps
            if stamp is not None
        ]
        return max((moment.timestamp() for moment in moments), default=0.0)

    latest_error = None
    for job in jobs:
        if not job.last_error_type:
            continue
        if latest_error is None or _touched(job) >= _touched(latest_error):
            latest_error = job

    breaker = avatar_breaker.status(session, provider, now=now)

    cost_reserved = sum(job.cost_usd or 0.0 for job in jobs if job.status in RESERVED_STATUSES)
    cost_actual = sum(job.cost_usd or 0.0 for job in jobs if job.status in SPENT_STATUSES)
    cost_unresolved = sum(job.cost_usd or 0.0 for job in unresolved)
    # Committed, not merely spent: an unresolved job may still turn out to have
    # been billed, so the operator's remaining room has to assume it was.
    committed = round(cost_actual + cost_reserved + cost_unresolved, 6)
    budget_remaining = (
        None if max_cost_usd is None else round(max(0.0, float(max_cost_usd) - committed), 6)
    )
    projected = committed + float(planned_cost_usd or 0.0)
    over_budget = (
        None if max_cost_usd is None else bool(projected > float(max_cost_usd) + 1e-9)
    )

    return {
        "provider": provider,
        "engine": engine,
        "studio_mode": studio_mode,
        "max_concurrent_jobs": max_concurrent_jobs,
        "active_jobs": len(active),
        "available_slots": max(0, max_concurrent_jobs - len(active)),
        "waiting_submits": len(waiting),
        "next_poll_at": _iso(next_poll),
        "retry_count": sum(job.retry_count or 0 for job in jobs),
        "last_error_type": latest_error.last_error_type if latest_error else None,
        "last_status_code": latest_error.last_status_code if latest_error else None,
        "retry_after_seconds": latest_error.retry_after_seconds if latest_error else None,
        "circuit_breaker": breaker.to_dict(),
        "cost_reserved_usd": round(cost_reserved, 6),
        "cost_actual_usd": round(cost_actual, 6),
        "cost_unresolved_usd": round(cost_unresolved, 6),
        "cost_committed_usd": committed,
        "cost_planned_usd": (
            None if planned_cost_usd is None else round(float(planned_cost_usd), 6)
        ),
        "max_cost_usd": None if max_cost_usd is None else round(float(max_cost_usd), 6),
        "budget_remaining_usd": budget_remaining,
        "budget_exceeded_expected": over_budget,
        "unresolved_jobs": len(unresolved),
        "polling_jobs": len(active),
        "downloading_jobs": sum(
            1
            for job in jobs
            if job.status == AvatarJobStatus.COMPLETED.value and not job.output_path
        ),
        "validating_jobs": sum(
            1
            for job in jobs
            if job.status == AvatarJobStatus.COMPLETED.value
            and job.output_path
            and job.validation_status == "pending"
        ),
        "retrying_jobs": sum(1 for job in jobs if (job.retry_count or 0) > 0),
        "validation": {
            "pending": sum(1 for job in jobs if job.validation_status == "pending"),
            "validated": sum(1 for job in jobs if job.validation_status == "validated"),
            "quarantined": sum(1 for job in jobs if job.validation_status == "quarantined"),
            "legacy": sum(1 for job in jobs if job.validation_status == "legacy"),
        },
        "jobs": [
            {
                "job_id": job.id,
                "scene_id": job.scene_id,
                "chapter_id": job.chapter_id,
                "status": job.status,
                "validation_status": job.validation_status,
                "retry_count": job.retry_count or 0,
                "last_error_type": job.last_error_type,
                "last_status_code": job.last_status_code,
                "next_poll_at": _iso(job.next_poll_at),
                "has_provider_job": bool(job.provider_job_id),
                "audio_asset_reused": bool(job.audio_asset_id),
                "cost_usd": job.cost_usd or 0.0,
            }
            for job in jobs
        ],
    }
