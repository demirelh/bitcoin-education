"""AvatarProviderBreaker: when to stop asking a provider that keeps saying no.

The failure this guards against is not a single bad request but a run that
keeps hammering a provider which is rate limiting, down, or refusing the key —
and, in the worst case, keeps *ordering* while doing so. After a handful of
consecutive failures of that kind the sensible answer is to stop submitting,
say why, and let a person look.

Two deliberate limitations keep this simple enough to reason about:

* It is persistent, not in-memory. A breaker that forgets itself on restart is
  no protection at all against a systemd timer that restarts every ten minutes.
* It stops *new submissions only*. Jobs already running at the provider are
  still polled read-only, because abandoning them would turn a provider outage
  into unresolved money.

Opening is automatic; closing is not. A human closes it, with a reason, and the
reset is written to the same audit table as every other operator override.
"""

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from btcedu.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class BreakerState(str, enum.Enum):
    """Three states, no more."""

    CLOSED = "closed"  # normal operation
    OPEN = "open"  # new submissions refused until the cooldown expires
    #: Cooldown elapsed. One trial submission is allowed; it either closes the
    #: breaker or opens it again for a fresh cooldown.
    HALF_OPEN = "half_open"


class AvatarProviderBreaker(Base):
    """One row per provider, holding why submissions are (not) allowed."""

    __tablename__ = "avatar_provider_breakers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)

    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default=BreakerState.CLOSED.value
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    #: The classified failure that last counted, e.g. ``rate_limited``. A class,
    #: never a provider payload and never a header that might carry a token.
    last_failure_class: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cooldown_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: Who closed it by hand, and why. Non-confidential reference only.
    reset_by_ref: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reset_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reset_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def __repr__(self) -> str:
        return (
            f"<AvatarProviderBreaker(provider='{self.provider}', state='{self.state}', "
            f"failures={self.consecutive_failures})>"
        )
