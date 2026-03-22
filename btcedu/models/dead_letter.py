"""Dead-letter queue model for permanently failed pipeline stages."""

from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from btcedu.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DeadLetterEntry(Base):
    """Record of a permanent pipeline failure requiring human intervention."""

    __tablename__ = "dead_letter_queue"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    error_category: Mapped[str] = mapped_column(String(32), nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    suggestion: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    resolved_by: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )  # "auto_retry" | "manual" | "skip"
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        resolved = "resolved" if self.resolved_at else "pending"
        return (
            f"<DeadLetterEntry(id={self.id}, episode='{self.episode_id}', "
            f"stage='{self.stage}', category='{self.error_category}', {resolved})>"
        )
