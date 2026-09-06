"""AvatarJobAudit: who decided what about a clip whose outcome nobody knew.

Reconciliation is the one place in the avatar path where a human overrules the
ledger. That makes it the one place where "the row says completed" is not
self-explanatory: the row says so because somebody claimed to have seen the
clip in the provider's dashboard. This table keeps that claim, with the time,
the action and a non-confidential operator reference.

It is deliberately append-only in practice — nothing in the code updates a row
once written — and deliberately free of personal data: an operator reference is
an initial, a shift name or a ticket id, never a name or an address.
"""

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from btcedu.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AvatarAuditAction(str, enum.Enum):
    """Every operator action the reconciliation CLI can record."""

    # Provider confirms the job exists and is still generating.
    CONFIRM_RUNNING = "confirm_running"
    # Provider confirms the job finished; the file was collected and validated.
    CONFIRM_DELIVERED = "confirm_delivered"
    # Provider confirms it refused the request and did not bill it.
    CONFIRM_NOT_BILLED = "confirm_not_billed"
    # Provider cannot identify the job: the row stays blocked on purpose.
    UNRESOLVED = "unresolved"
    # The operator accepts the loss. Paid or not, this clip is given up.
    ABANDONED = "abandoned"
    # A provider job id was attached to a row by hand.
    ATTACH_JOB_ID = "attach_job_id"
    # An episode was allowed to fall back to the old voice-over presentation.
    VOICE_OVER_OVERRIDE = "voice_over_override"


class AvatarJobAudit(Base):
    """One operator decision about one avatar job, or about one episode."""

    __tablename__ = "avatar_job_audit"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # Null for episode-wide decisions such as the voice-over override.
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    scene_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")

    action: Mapped[str] = mapped_column(String(32), nullable=False)
    from_status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    to_status: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    provider_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    # An initial, a shift or a ticket id. Never a name, never contact data.
    operator_ref: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")

    # What the budget believed before and after, so a reconciliation that moves
    # money can be read back without re-deriving it from the job row.
    cost_before_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cost_after_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def __repr__(self) -> str:
        return (
            f"<AvatarJobAudit(episode_id='{self.episode_id}', scene_id='{self.scene_id}', "
            f"action='{self.action}', to_status='{self.to_status}')>"
        )
