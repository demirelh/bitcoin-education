"""PresenterAssignment: the one outfit an episode wears, fixed for its lifetime."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from btcedu.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PresenterAssignment(Base):
    """Which avatar look an episode uses, chosen once and never re-rolled.

    The unique constraint on episode_id is the mechanism, not a formality: it
    makes a second selection for the same episode fail at the database rather
    than quietly changing the presenter's outfit halfway through a retry or
    after a reboot. Rotation reads this table across episodes to find the look
    that has gone unused longest, so it also has to be the durable record.
    """

    __tablename__ = "presenter_assignments"
    __table_args__ = (
        UniqueConstraint("episode_id", name="uq_presenter_assignment_episode"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    avatar_type: Mapped[str] = mapped_column(String(32), nullable=False)
    avatar_look_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    look_name: Mapped[str] = mapped_column(String(64), nullable=False)

    strategy: Mapped[str] = mapped_column(String(32), nullable=False)
    config_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="assigned")

    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    provenance_path: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Rotation orders by this, so it is read far more often than written.
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )
    # Set when an operator deliberately swaps the outfit; the row is rewritten
    # in place so an episode still has exactly one assignment.
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cost_per_second_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    def __repr__(self) -> str:
        return (
            f"<PresenterAssignment(episode_id='{self.episode_id}', "
            f"look_name='{self.look_name}', engine='{self.engine}')>"
        )
