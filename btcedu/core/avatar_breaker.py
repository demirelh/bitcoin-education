"""The provider circuit breaker: stop ordering when the provider keeps refusing.

Deliberately the simplest thing that works. There is one row per provider, a
counter, a cooldown and a reason. No sliding windows, no percentile error rates,
no external service — because the failure mode this defends against is blunt:
an expired key, an exhausted quota or an outage, repeated by a systemd timer
every ten minutes until somebody notices.

What it does and does not stop is the important part:

* It blocks **new submissions**. That is the only operation that can cost money.
* It never blocks **polling or downloading** an already-submitted job. Those are
  read-only, and refusing them would strand jobs that are already paid for.
* It never resolves anything. A blocked run leaves its jobs exactly as they
  were; nothing is quietly written off as failed.

Opening is automatic. Closing is a human act with a note, recorded in the same
audit table as every other operator override.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.orm.exc import ObjectDeletedError

from btcedu.core.avatar_retry import FailureClass, FailureVerdict
from btcedu.models.avatar_job_audit import AvatarAuditAction, AvatarJobAudit
from btcedu.models.avatar_provider_breaker import AvatarProviderBreaker, BreakerState

logger = logging.getLogger(__name__)

#: Consecutive qualifying failures before submissions stop.
DEFAULT_FAILURE_THRESHOLD = 3
#: How long the breaker stays open before one trial submission is allowed.
DEFAULT_COOLDOWN_SECONDS = 15 * 60
#: Authentication failures get a longer cooldown: a wrong key will not fix
#: itself in fifteen minutes, and retrying it only invites a provider-side lock.
AUTH_COOLDOWN_SECONDS = 60 * 60

#: Failure classes that count. A malformed request of ours is not the
#: provider's fault and must not trip the breaker for everybody else.
COUNTING_CLASSES = frozenset(
    {
        FailureClass.AUTH,
        FailureClass.RATE_LIMITED,
        FailureClass.SERVER_ERROR,
        FailureClass.NETWORK,
    }
)


class BreakerOpenError(RuntimeError):
    """Raised when a submission is attempted while the breaker is open."""

    def __init__(self, provider: str, reason: str, cooldown_until: datetime | None):
        self.provider = provider
        self.reason = reason
        self.cooldown_until = cooldown_until
        until = cooldown_until.isoformat() if cooldown_until else "unknown"
        super().__init__(
            f"Avatar provider {provider!r} is circuit-broken until {until}: {reason}"
        )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


@dataclass(frozen=True)
class BreakerView:
    """What an operator sees, in CLI and dashboard alike."""

    provider: str
    state: str
    consecutive_failures: int
    submissions_allowed: bool
    reason: str = ""
    last_failure_class: str = ""
    last_status_code: int | None = None
    opened_at: datetime | None = None
    cooldown_until: datetime | None = None
    cooldown_remaining_seconds: float = 0.0
    last_failure_at: datetime | None = None
    last_success_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        def iso(value: datetime | None) -> str | None:
            return value.isoformat() if value else None

        return {
            "provider": self.provider,
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "submissions_allowed": self.submissions_allowed,
            "reason": self.reason,
            "last_failure_class": self.last_failure_class,
            "last_status_code": self.last_status_code,
            "opened_at": iso(self.opened_at),
            "cooldown_until": iso(self.cooldown_until),
            "cooldown_remaining_seconds": round(self.cooldown_remaining_seconds, 1),
            "last_failure_at": iso(self.last_failure_at),
            "last_success_at": iso(self.last_success_at),
        }


#: How often first use may lose the race before it gives up. Two other writers
#: winning in a row is already implausible; a third means something else is
#: wrong and looping forever would only hide it.
_FIRST_USE_ATTEMPTS = 3


def _fetch(session: Session, provider: str) -> AvatarProviderBreaker | None:
    return (
        session.query(AvatarProviderBreaker)
        .filter(AvatarProviderBreaker.provider == provider)
        .first()
    )


def _is_present(row: AvatarProviderBreaker) -> bool:
    """Whether the row still exists behind an instance we are holding.

    The breaker row is not owned by one session. Between the SELECT that found
    it and the first attribute read, another writer may have removed it, or an
    interleaved rollback may have undone the INSERT we just committed. Reading
    an expired attribute then raises ``ObjectDeletedError`` deep inside whoever
    asked for the breaker state — on the dashboard that is a 500 on a page that
    only wanted to display a status. One extra read here turns it into a
    recoverable answer.
    """
    try:
        row.state
    except ObjectDeletedError:
        return False
    return True


def get_or_create(session: Session, provider: str) -> AvatarProviderBreaker:
    """Fetch the breaker row, creating a closed one on first use."""
    for _ in range(_FIRST_USE_ATTEMPTS):
        row = _fetch(session, provider)
        if row is None:
            row = AvatarProviderBreaker(
                provider=provider,
                state=BreakerState.CLOSED.value,
                consecutive_failures=0,
                updated_at=_utcnow(),
            )
            session.add(row)
            try:
                session.commit()
            except IntegrityError:
                # Two runs reached first use at the same moment. The other one
                # won; its row is just as good as the one we were about to
                # write, so read it back on the next pass.
                session.rollback()
                continue
        if _is_present(row):
            return row
        session.expunge(row)
    raise RuntimeError(f"Could not establish the breaker row for provider {provider!r}")


def _refresh_state(
    session: Session,
    row: AvatarProviderBreaker,
    *,
    now: Callable[[], datetime],
) -> AvatarProviderBreaker:
    """Move an expired cooldown to half-open before anyone reads the state."""
    if row.state != BreakerState.OPEN.value:
        return row
    cooldown = _aware(row.cooldown_until)
    if cooldown is not None and cooldown > now():
        return row
    row.state = BreakerState.HALF_OPEN.value
    row.updated_at = now()
    session.commit()
    return row


def status(
    session: Session, provider: str, *, now: Callable[[], datetime] = _utcnow
) -> BreakerView:
    """Current breaker state, with the cooldown already aged."""
    row = _refresh_state(session, get_or_create(session, provider), now=now)
    cooldown = _aware(row.cooldown_until)
    remaining = 0.0
    if cooldown is not None:
        remaining = max(0.0, (cooldown - now()).total_seconds())
    return BreakerView(
        provider=row.provider,
        state=row.state,
        consecutive_failures=int(row.consecutive_failures or 0),
        submissions_allowed=row.state != BreakerState.OPEN.value,
        reason=row.reason or "",
        last_failure_class=row.last_failure_class or "",
        last_status_code=row.last_status_code,
        opened_at=_aware(row.opened_at),
        cooldown_until=cooldown,
        cooldown_remaining_seconds=remaining,
        last_failure_at=_aware(row.last_failure_at),
        last_success_at=_aware(row.last_success_at),
    )


def ensure_submission_allowed(
    session: Session, provider: str, *, now: Callable[[], datetime] = _utcnow
) -> None:
    """Raise if a new, billable provider order must not be sent right now."""
    view = status(session, provider, now=now)
    if not view.submissions_allowed:
        raise BreakerOpenError(provider, view.reason, view.cooldown_until)


def record_success(
    session: Session, provider: str, *, now: Callable[[], datetime] = _utcnow
) -> BreakerView:
    """One clean call closes the breaker and clears the counter."""
    row = get_or_create(session, provider)
    moment = now()
    row.state = BreakerState.CLOSED.value
    row.consecutive_failures = 0
    row.last_failure_class = None
    row.last_status_code = None
    row.reason = None
    row.opened_at = None
    row.cooldown_until = None
    row.last_success_at = moment
    row.updated_at = moment
    session.commit()
    return status(session, provider, now=now)


def record_failure(
    session: Session,
    provider: str,
    verdict: FailureVerdict,
    *,
    threshold: int = DEFAULT_FAILURE_THRESHOLD,
    cooldown_seconds: float | None = None,
    now: Callable[[], datetime] = _utcnow,
) -> BreakerView:
    """Count a failure and open the breaker once they pile up.

    Only the classes in :data:`COUNTING_CLASSES` count. A 400 because we sent a
    bad payload says nothing about the provider's health, and letting it trip
    the breaker would punish every other episode for our own bug.
    """
    row = get_or_create(session, provider)
    moment = now()
    row.last_failure_at = moment
    row.updated_at = moment

    if verdict.failure_class not in COUNTING_CLASSES:
        session.commit()
        return status(session, provider, now=now)

    row.consecutive_failures = int(row.consecutive_failures or 0) + 1
    row.last_failure_class = verdict.failure_class.value
    row.last_status_code = verdict.status_code

    # A half-open trial that fails goes straight back to open: the point of the
    # trial was to find out, and it did.
    trip = row.consecutive_failures >= threshold or row.state == BreakerState.HALF_OPEN.value
    if trip:
        if cooldown_seconds is None:
            cooldown_seconds = (
                AUTH_COOLDOWN_SECONDS
                if verdict.failure_class is FailureClass.AUTH
                else DEFAULT_COOLDOWN_SECONDS
            )
        # A provider that named its own delay knows better than our default.
        if verdict.retry_after_seconds:
            cooldown_seconds = max(cooldown_seconds, float(verdict.retry_after_seconds))
        row.state = BreakerState.OPEN.value
        row.opened_at = row.opened_at or moment
        row.cooldown_until = moment + timedelta(seconds=float(cooldown_seconds))
        row.reason = (
            f"{row.consecutive_failures} consecutive {verdict.failure_class.value} "
            f"failures; {verdict.reason}"
        )[:1000]
        logger.error(
            "Avatar provider %s circuit breaker opened: %s", provider, row.reason
        )
    session.commit()
    return status(session, provider, now=now)


def reset(
    session: Session,
    provider: str,
    *,
    operator_ref: str,
    note: str,
    now: Callable[[], datetime] = _utcnow,
) -> BreakerView:
    """Close the breaker by hand. Requires a reason and leaves an audit row.

    The note is not bureaucracy: the breaker only ever opens for a reason that
    needed a human, and "who decided it was fixed, and on what basis" is the
    single most useful thing to have when it opens again an hour later.
    """
    if not note.strip():
        raise ValueError("Resetting the avatar circuit breaker requires a note")
    if not operator_ref.strip():
        raise ValueError("Resetting the avatar circuit breaker requires an operator reference")

    row = get_or_create(session, provider)
    previous_state = row.state
    moment = now()
    row.state = BreakerState.CLOSED.value
    row.consecutive_failures = 0
    row.cooldown_until = None
    row.opened_at = None
    row.reason = None
    row.reset_by_ref = operator_ref.strip()[:64]
    row.reset_note = note.strip()[:2000]
    row.reset_at = moment
    row.updated_at = moment

    session.add(
        AvatarJobAudit(
            # Provider-wide, not tied to one episode. The empty id is what the
            # audit table already uses for episode-less decisions.
            episode_id="",
            job_id=None,
            scene_id="",
            action=AvatarAuditAction.BREAKER_RESET.value,
            from_status=previous_state,
            to_status=BreakerState.CLOSED.value,
            provider_job_id=None,
            operator_ref=operator_ref.strip()[:64],
            note=note.strip()[:2000],
            created_at=moment,
        )
    )
    session.commit()
    logger.warning(
        "Avatar provider %s circuit breaker reset by %s", provider, operator_ref.strip()[:64]
    )
    return status(session, provider, now=now)
