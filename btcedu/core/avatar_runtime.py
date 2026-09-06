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
    latest_error = None
    for job in jobs:
        if not job.last_error_type:
            continue
        if latest_error is None or (job.updated_at or job.created_at) >= (
            latest_error.updated_at or latest_error.created_at
        ):
            latest_error = job

    breaker = avatar_breaker.status(session, provider, now=now)

    return {
        "provider": provider,
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
        "cost_reserved_usd": round(
            sum(job.cost_usd or 0.0 for job in jobs if job.status in RESERVED_STATUSES), 6
        ),
        "cost_actual_usd": round(
            sum(job.cost_usd or 0.0 for job in jobs if job.status in SPENT_STATUSES), 6
        ),
        "cost_unresolved_usd": round(sum(job.cost_usd or 0.0 for job in unresolved), 6),
        "unresolved_jobs": len(unresolved),
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
