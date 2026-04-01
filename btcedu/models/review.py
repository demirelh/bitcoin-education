"""Review ORM models for human review workflow."""

from __future__ import annotations

import enum
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from btcedu.db import Base

# Import ReviewItemDecision at module level so SQLAlchemy can resolve the
# string-based back-reference in the item_decisions relationship.
# review_item.py only uses a forward string ref "ReviewTask" so no circular import.
from btcedu.models.review_item import ReviewItemDecision  # noqa: E402

if TYPE_CHECKING:
    pass


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ReviewStatus(str, enum.Enum):
    PENDING = "pending"
    IN_REVIEW = "in_review"
    APPROVED = "approved"
    REJECTED = "rejected"
    CHANGES_REQUESTED = "changes_requested"


class ReviewTask(Base):
    """A review task created when pipeline reaches a review gate."""

    __tablename__ = "review_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ReviewStatus.PENDING.value
    )
    artifact_paths: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list
    diff_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    prompt_version_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("prompt_versions.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reviewer_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifact_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    decisions: Mapped[list["ReviewDecision"]] = relationship(  # noqa: UP037
        back_populates="review_task", cascade="all, delete-orphan"
    )
    item_decisions: Mapped[list["ReviewItemDecision"]] = relationship(  # noqa: UP037
        "ReviewItemDecision",
        back_populates="review_task",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return (
            f"<ReviewTask(id={self.id}, episode_id='{self.episode_id}', "
            f"stage='{self.stage}', status='{self.status}')>"
        )


class ReviewDecision(Base):
    """Audit trail entry for a review decision."""

    __tablename__ = "review_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    review_task_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("review_tasks.id"), nullable=False
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    quality_rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    review_task: Mapped["ReviewTask"] = relationship(  # noqa: UP037
        back_populates="decisions"
    )

    def __repr__(self) -> str:
        return (
            f"<ReviewDecision(id={self.id}, review_task_id={self.review_task_id}, "
            f"decision='{self.decision}')>"
        )
