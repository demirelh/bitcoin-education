"""Deliberately buying a presenter clip a second time.

Two words that look alike and must never be confused:

* **retry** — the request or the poll failed and the clip we already paid for
  is still owed to us. The ledger handles it; no human is involved.
* **regenerate** — the clip arrived, an operator watched it and wants a
  different take. That is a purchase. It needs a reason, an explicit
  confirmation of the extra cost, and it leaves the old clip untouched.

This module is only the second one. Nothing here calls a provider: a web
request must never turn into a HeyGen invoice. It records an intent that the
next anchor run acts on, which also means an operator can change their mind
before anything is bought.
"""

import hashlib
import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from btcedu.models.avatar_job import AvatarJob, AvatarJobStatus
from btcedu.models.avatar_regeneration import (
    OPEN_STATUSES,
    AvatarRegenerationRequest,
    RegenerationStatus,
)

logger = logging.getLogger(__name__)


class RegenerationError(RuntimeError):
    """The requested re-purchase is not permitted, or not yet authorised."""


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class RegenerationQuote:
    """What an operator is agreeing to before they confirm."""

    episode_id: str
    scene_id: str
    revision: int
    reason: str
    previous_cost_usd: float
    estimated_cost_usd: float
    duration_seconds: float
    status: str
    request_id: int

    def to_dict(self) -> dict:
        return {
            "episode_id": self.episode_id,
            "scene_id": self.scene_id,
            "revision": self.revision,
            "reason": self.reason,
            "previous_cost_usd": round(self.previous_cost_usd, 6),
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "duration_seconds": round(self.duration_seconds, 3),
            "status": self.status,
            "request_id": self.request_id,
            "warning": (
                "Confirming places a new paid HeyGen order for this scene. "
                "The existing clip is kept for audit and rollback."
            ),
        }


def idempotency_key(episode_id: str, scene_id: str, revision: int) -> str:
    """Stable across retries of the same intent, distinct across revisions."""
    raw = f"{episode_id}\x00{scene_id}\x00{int(revision)}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:48]


def scene_jobs(session: Session, episode_id: str, scene_id: str) -> list[AvatarJob]:
    return (
        session.query(AvatarJob)
        .filter(AvatarJob.episode_id == episode_id, AvatarJob.scene_id == scene_id)
        .order_by(AvatarJob.reserved_at, AvatarJob.id)
        .all()
    )


def requests_for_episode(session: Session, episode_id: str) -> list[AvatarRegenerationRequest]:
    return (
        session.query(AvatarRegenerationRequest)
        .filter(AvatarRegenerationRequest.episode_id == episode_id)
        .order_by(AvatarRegenerationRequest.scene_id, AvatarRegenerationRequest.revision)
        .all()
    )


def active_revision(session: Session, episode_id: str, scene_id: str) -> int:
    """The revision the anchor stage should generate for this scene.

    Only a *confirmed* or already *consumed* request counts. A request that was
    merely opened in the dashboard authorises nothing, which is the whole point
    of splitting preparation from confirmation.
    """
    row = (
        session.query(AvatarRegenerationRequest)
        .filter(
            AvatarRegenerationRequest.episode_id == episode_id,
            AvatarRegenerationRequest.scene_id == scene_id,
            AvatarRegenerationRequest.status.in_(
                [RegenerationStatus.CONFIRMED.value, RegenerationStatus.CONSUMED.value]
            ),
        )
        .order_by(AvatarRegenerationRequest.revision.desc())
        .first()
    )
    return int(row.revision) if row is not None else 0


def episode_revisions(session: Session, episode_id: str) -> dict[str, int]:
    """Active revision per scene, for the scenes that have one."""
    rows = (
        session.query(AvatarRegenerationRequest)
        .filter(
            AvatarRegenerationRequest.episode_id == episode_id,
            AvatarRegenerationRequest.status.in_(
                [RegenerationStatus.CONFIRMED.value, RegenerationStatus.CONSUMED.value]
            ),
        )
        .all()
    )
    revisions: dict[str, int] = {}
    for row in rows:
        revisions[row.scene_id] = max(revisions.get(row.scene_id, 0), int(row.revision))
    return revisions


def _existing_open_request(
    session: Session, episode_id: str, scene_id: str
) -> AvatarRegenerationRequest | None:
    return (
        session.query(AvatarRegenerationRequest)
        .filter(
            AvatarRegenerationRequest.episode_id == episode_id,
            AvatarRegenerationRequest.scene_id == scene_id,
            AvatarRegenerationRequest.status.in_(sorted(OPEN_STATUSES)),
        )
        .order_by(AvatarRegenerationRequest.revision.desc())
        .first()
    )


def prepare(
    session: Session,
    episode_id: str,
    scene_id: str,
    *,
    reason: str,
    requested_by_ref: str,
    cost_per_second_usd: float,
) -> RegenerationQuote:
    """Open a regeneration and quote its cost. Nothing is authorised yet.

    Idempotent by construction: a second call for the same scene returns the
    open request rather than creating another, so a double-clicked button, a
    retried POST and an impatient operator all produce one purchase.
    """
    if not reason.strip():
        raise RegenerationError("A regeneration needs a reason for the record")
    if not requested_by_ref.strip():
        raise RegenerationError("A regeneration needs an operator reference")

    jobs = scene_jobs(session, episode_id, scene_id)
    if not jobs:
        raise RegenerationError(
            f"No avatar job exists for scene {scene_id!r}; there is nothing to regenerate. "
            "Run the anchor stage first."
        )

    blocked = [j for j in jobs if j.status == AvatarJobStatus.RECONCILE_REQUIRED.value]
    if blocked:
        raise RegenerationError(
            f"Scene {scene_id!r} has an unreconciled avatar job. Resolve it with "
            "`btcedu avatar-reconcile` before ordering another clip — buying a "
            "replacement for a job whose outcome is unknown pays twice."
        )

    open_request = _existing_open_request(session, episode_id, scene_id)
    if open_request is not None:
        return _quote(open_request, jobs)

    previous_cost = sum(float(j.cost_usd or 0.0) for j in jobs)
    duration = max((float(j.duration_seconds or 0.0) for j in jobs), default=0.0)
    estimate = round(duration * float(cost_per_second_usd), 6)
    known = [int(r.revision) for r in _scene_requests(session, episode_id, scene_id)]
    revision = max(known, default=0) + 1

    request = AvatarRegenerationRequest(
        episode_id=episode_id,
        scene_id=scene_id,
        revision=revision,
        status=RegenerationStatus.REQUESTED.value,
        reason=reason.strip()[:2000],
        requested_by_ref=requested_by_ref.strip()[:64],
        previous_job_id=int(jobs[-1].id),
        previous_cost_usd=previous_cost,
        estimated_cost_usd=estimate,
        idempotency_key=idempotency_key(episode_id, scene_id, revision),
        created_at=_utcnow(),
    )
    session.add(request)
    try:
        session.commit()
    except IntegrityError:
        # Two requests raced. The constraint decided; report the winner.
        session.rollback()
        winner = _existing_open_request(session, episode_id, scene_id)
        if winner is None:
            raise
        return _quote(winner, jobs)
    return _quote(request, jobs)


def _scene_requests(
    session: Session, episode_id: str, scene_id: str
) -> list[AvatarRegenerationRequest]:
    return (
        session.query(AvatarRegenerationRequest)
        .filter(
            AvatarRegenerationRequest.episode_id == episode_id,
            AvatarRegenerationRequest.scene_id == scene_id,
        )
        .all()
    )


def _quote(request: AvatarRegenerationRequest, jobs: list[AvatarJob]) -> RegenerationQuote:
    return RegenerationQuote(
        episode_id=request.episode_id,
        scene_id=request.scene_id,
        revision=int(request.revision),
        reason=request.reason,
        previous_cost_usd=float(request.previous_cost_usd or 0.0),
        estimated_cost_usd=float(request.estimated_cost_usd or 0.0),
        duration_seconds=max((float(j.duration_seconds or 0.0) for j in jobs), default=0.0),
        status=request.status,
        request_id=int(request.id),
    )


def confirm(
    session: Session,
    episode_id: str,
    scene_id: str,
    *,
    revision: int,
    confirmed_by_ref: str,
) -> AvatarRegenerationRequest:
    """Authorise the purchase an operator has just been quoted.

    Takes the revision the dashboard showed rather than "the latest one": if
    something changed between the quote and the click, the operator is
    confirming a number they were never shown, and that is exactly the moment
    to stop.
    """
    if not confirmed_by_ref.strip():
        raise RegenerationError("Confirming a regeneration needs an operator reference")

    request = (
        session.query(AvatarRegenerationRequest)
        .filter(
            AvatarRegenerationRequest.episode_id == episode_id,
            AvatarRegenerationRequest.scene_id == scene_id,
            AvatarRegenerationRequest.revision == int(revision),
        )
        .first()
    )
    if request is None:
        raise RegenerationError(
            f"No regeneration request for scene {scene_id!r} revision {revision}. "
            "Prepare the regeneration first so the cost is shown before it is agreed."
        )
    if request.status == RegenerationStatus.CONFIRMED.value:
        # A second confirmation of the same request is not an error and must
        # not become a second order.
        return request
    if request.status != RegenerationStatus.REQUESTED.value:
        raise RegenerationError(
            f"Regeneration for {scene_id!r} revision {revision} is "
            f"'{request.status}' and cannot be confirmed."
        )

    updated = (
        session.query(AvatarRegenerationRequest)
        .filter(
            AvatarRegenerationRequest.id == request.id,
            AvatarRegenerationRequest.status == RegenerationStatus.REQUESTED.value,
        )
        .update(
            {
                AvatarRegenerationRequest.status: RegenerationStatus.CONFIRMED.value,
                AvatarRegenerationRequest.confirmed_at: _utcnow(),
                AvatarRegenerationRequest.confirmed_by_ref: confirmed_by_ref.strip()[:64],
            },
            synchronize_session=False,
        )
    )
    if updated == 0:
        session.rollback()
        session.refresh(request)
        if request.status == RegenerationStatus.CONFIRMED.value:
            return request
        raise RegenerationError(
            f"Regeneration for {scene_id!r} revision {revision} changed while being confirmed"
        )
    session.commit()
    session.refresh(request)
    logger.warning(
        "Regeneration confirmed for %s/%s revision %d (~$%.4f): %s",
        episode_id,
        scene_id,
        request.revision,
        request.estimated_cost_usd,
        request.reason,
    )
    return request


def cancel(
    session: Session, episode_id: str, scene_id: str, *, revision: int
) -> AvatarRegenerationRequest:
    """Withdraw a regeneration that has not been acted on."""
    request = (
        session.query(AvatarRegenerationRequest)
        .filter(
            AvatarRegenerationRequest.episode_id == episode_id,
            AvatarRegenerationRequest.scene_id == scene_id,
            AvatarRegenerationRequest.revision == int(revision),
        )
        .first()
    )
    if request is None:
        raise RegenerationError(f"No regeneration request for {scene_id!r} revision {revision}")
    if request.status == RegenerationStatus.CONSUMED.value:
        raise RegenerationError(
            "That regeneration has already been generated; cancelling it would not "
            "unspend the money. Use `avatar-reconcile` if the clip is wrong."
        )
    request.status = RegenerationStatus.CANCELLED.value
    session.commit()
    return request


def mark_consumed(session: Session, episode_id: str, scene_ids: list[str]) -> int:
    """Close the confirmed requests the anchor stage has just acted on."""
    if not scene_ids:
        return 0
    rows = (
        session.query(AvatarRegenerationRequest)
        .filter(
            AvatarRegenerationRequest.episode_id == episode_id,
            AvatarRegenerationRequest.scene_id.in_(scene_ids),
            AvatarRegenerationRequest.status == RegenerationStatus.CONFIRMED.value,
        )
        .all()
    )
    now = _utcnow()
    for row in rows:
        row.status = RegenerationStatus.CONSUMED.value
        row.consumed_at = now
    session.commit()
    return len(rows)
