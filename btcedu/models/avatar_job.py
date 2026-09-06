"""AvatarJob: the durable record of every avatar clip an episode has paid for.

HeyGen bills a generation the moment it starts, not when the file is collected.
So the only thing standing between a reboot and a second invoice is a record
that outlives the process — written *before* the request leaves the machine and
carrying the provider's job id the instant it comes back.

Identity is ``(episode_id, scene_id, content_hash)``. The hash is what makes the
row mean "this exact clip": the same scene with a different look, a different
text or a different length is different work and gets its own row, while a rerun
of unchanged work finds the finished one and pays nothing.
"""

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from btcedu.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AvatarJobStatus(str, enum.Enum):
    """Lifecycle of one avatar clip.

    ``RESERVED`` is deliberately not a safe state to retry from: it means the
    request was about to be sent and the outcome is unknown, which is exactly
    the situation in which a blind retry buys the same clip twice.
    """

    RESERVED = "reserved"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    # The provider refused before generating. Nothing was billed, so a retry is
    # safe — this is the only failure state that allows one.
    FAILED = "failed"
    # Outcome unknown. Blocks automatic retry until an operator resolves it.
    RECONCILE_REQUIRED = "reconcile_required"
    # An operator gave the clip up. It may well have been billed, so the cost
    # stays on the episode — and unlike FAILED this is not a retryable state:
    # abandoning is a decision, not a transient error.
    ABANDONED = "abandoned"


#: States from which no new provider call may be made automatically.
BLOCKING_STATUSES = frozenset(
    {
        AvatarJobStatus.RESERVED.value,
        AvatarJobStatus.RECONCILE_REQUIRED.value,
        AvatarJobStatus.ABANDONED.value,
    }
)


class AvatarJob(Base):
    """One avatar clip: reserved, submitted, collected — or held for an operator."""

    __tablename__ = "avatar_jobs"
    __table_args__ = (
        UniqueConstraint(
            "episode_id", "scene_id", "content_hash", name="uq_avatar_job_scene_content"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scene_id: Mapped[str] = mapped_column(String(128), nullable=False)
    chapter_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    # Fingerprint of the work: look, audio, engine and output parameters.
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    engine: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    avatar_look_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    output_format: Mapped[str] = mapped_column(String(16), nullable=False, default="mp4")

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AvatarJobStatus.RESERVED.value, index=True
    )
    # Written the instant the provider accepts, before anything fallible.
    provider_job_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    output_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    # What was actually billed. A reserved or reconcile-required row keeps the
    # estimate, because an unknown outcome must be assumed to have cost money.
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)

    # -- Retry, idempotency and validation state ----------------------------
    # All of it persisted rather than held in the run: a process that dies
    # mid-generation must come back knowing which key it used, how often it has
    # already tried and whether the file it downloaded was ever checked.

    #: Sent as ``Idempotency-Key``. Generated and stored *before* the first
    #: create call so a restart repeats the same key instead of ordering again.
    idempotency_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    #: When the provider stops honouring that key. Past this point an unclear
    #: outcome may not be retried automatically at any price.
    idempotency_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    #: The provider's id for the uploaded narration, so a resumed run reuses
    #: the asset instead of pushing the same audio again.
    audio_asset_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    audio_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)

    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    last_status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    retry_after_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    #: Earliest sensible time to ask the provider again. Keeps the poller off a
    #: busy-wait loop and survives a restart.
    next_poll_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    #: ``pending`` / ``valid`` / ``invalid`` / ``quarantined``. A downloaded
    #: file that was never probed is not a usable clip.
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    validation_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<AvatarJob(episode_id='{self.episode_id}', scene_id='{self.scene_id}', "
            f"status='{self.status}', provider_job_id='{self.provider_job_id}')>"
        )
