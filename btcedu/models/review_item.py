"""Per-item review decisions for granular diff review (Phase 5)."""

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from btcedu.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ReviewItemAction(str, enum.Enum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EDITED = "edited"
    # UNCHANGED is kept as a distinct action for semantic clarity
    # (reviewer explicitly marked it as "keep original, this change is wrong").
    # Assembly behavior is identical to REJECTED: emit original words.
    UNCHANGED = "unchanged"


class ReviewItemDecision(Base):
    """Per-item decision for a single change in a correction or adaptation diff."""

    __tablename__ = "review_item_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("review_tasks.id"), nullable=False, index=True
    )
    item_id: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    original_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    action: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ReviewItemAction.PENDING.value
    )
    edited_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    review_task: Mapped["ReviewTask"] = relationship(back_populates="item_decisions")  # type: ignore[name-defined]

    __table_args__ = (
        Index("idx_review_item_decisions_task", "review_task_id"),
        Index("idx_review_item_decisions_task_item", "review_task_id", "item_id"),
    )

    def __repr__(self) -> str:
        return (
            f"<ReviewItemDecision(id={self.id}, review_task_id={self.review_task_id}, "
            f"item_id='{self.item_id}', action='{self.action}')>"
        )
