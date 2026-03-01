"""PublishJob ORM model for tracking YouTube publishing operations."""

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from btcedu.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class PublishJobStatus(str, enum.Enum):
    PENDING = "pending"
    UPLOADING = "uploading"
    PUBLISHED = "published"
    FAILED = "failed"


class PublishJob(Base):
    """Track YouTube publishing operations per episode."""

    __tablename__ = "publish_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PublishJobStatus.PENDING.value
    )
    youtube_video_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    youtube_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    metadata_snapshot: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    def __repr__(self) -> str:
        return (
            f"<PublishJob(id={self.id}, episode_id='{self.episode_id}', "
            f"status='{self.status}', video_id='{self.youtube_video_id}')>"
        )
