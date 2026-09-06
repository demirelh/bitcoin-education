"""AvatarRegenerationRequest: buying a second take of a clip, on purpose.

A finished presenter clip has been paid for. Asking for another one is
therefore not a retry — it is a purchase — and the two must not share a button,
a verb or a code path. "Retry" means the transport or the poll failed and the
same clip is still owed to us. "Regenerate" means the clip arrived, an operator
looked at it and wants a different one, knowing it costs again.

Why a request table rather than a column on ``AvatarJob``:

* The intent exists *before* the work does. Between an operator confirming a
  regeneration in the dashboard and the anchor stage acting on it, there is no
  job to hang the decision on — the new one does not exist yet and the old one
  must not be touched, because it is the audit trail and the rollback.
* The unique constraint on ``(episode_id, scene_id, revision)`` is what makes a
  double click or an HTTP retry produce one purchase instead of two. Putting a
  counter on the job row would have to be read, incremented and written, which
  is exactly the race the constraint removes.
* Nothing about the ledger's identity rule needs bending. The revision is
  folded into ``compute_job_hash``, so the new attempt has a genuinely
  different content hash: the ledger sees different work, which is the truth,
  rather than the same work squeezed past its own uniqueness check.
"""

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from btcedu.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class RegenerationStatus(str, enum.Enum):
    """How far one deliberate re-purchase has got."""

    #: The operator opened the dialogue. Nothing is authorised yet.
    REQUESTED = "requested"
    #: The operator confirmed the cost. The anchor stage may act on this.
    CONFIRMED = "confirmed"
    #: The anchor stage generated against this revision.
    CONSUMED = "consumed"
    #: Withdrawn before anything was bought.
    CANCELLED = "cancelled"


#: Statuses that still authorise (or may come to authorise) a purchase.
OPEN_STATUSES = frozenset({RegenerationStatus.REQUESTED.value, RegenerationStatus.CONFIRMED.value})


class AvatarRegenerationRequest(Base):
    """One operator's request for a fresh take of one presenter scene."""

    __tablename__ = "avatar_regeneration_requests"
    __table_args__ = (
        UniqueConstraint(
            "episode_id", "scene_id", "revision", name="uq_avatar_regeneration_scene_revision"
        ),
        UniqueConstraint("idempotency_key", name="uq_avatar_regeneration_idempotency"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scene_id: Mapped[str] = mapped_column(String(128), nullable=False)
    #: 1 for the first re-purchase; the original clip is revision 0 and has no row.
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RegenerationStatus.REQUESTED.value, index=True
    )
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    requested_by_ref: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    #: Who agreed to pay. Kept apart from the requester so the audit trail
    #: shows that two acts happened, even when one person did both.
    confirmed_by_ref: Mapped[str] = mapped_column(String(64), nullable=False, default="")

    #: Kept so the dashboard can show what has already been spent on this scene
    #: without having to reconstruct it from a job that may later be superseded.
    previous_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    previous_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    #: Derived from episode, scene and revision, so the same intent submitted
    #: twice collides instead of billing twice.
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<AvatarRegenerationRequest(episode_id='{self.episode_id}', "
            f"scene_id='{self.scene_id}', revision={self.revision}, status='{self.status}')>"
        )
