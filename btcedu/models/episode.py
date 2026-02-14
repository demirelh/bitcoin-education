import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from btcedu.db import Base


class EpisodeStatus(str, enum.Enum):
    NEW = "new"
    DOWNLOADED = "downloaded"
    TRANSCRIBED = "transcribed"
    CHUNKED = "chunked"
    GENERATED = "generated"
    REFINED = "refined"
    COMPLETED = "completed"
    FAILED = "failed"


class PipelineStage(str, enum.Enum):
    DETECT = "detect"
    DOWNLOAD = "download"
    TRANSCRIBE = "transcribe"
    CHUNK = "chunk"
    GENERATE = "generate"
    REFINE = "refine"
    COMPLETE = "complete"


class RunStatus(str, enum.Enum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


def _utcnow() -> datetime:
    return datetime.now(UTC)


class Episode(Base):
    __tablename__ = "episodes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    episode_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False, default="youtube_rss")
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[EpisodeStatus] = mapped_column(
        Enum(EpisodeStatus), nullable=False, default=EpisodeStatus.NEW
    )
    audio_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    transcript_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    output_dir: Mapped[str | None] = mapped_column(String(500), nullable=True)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    pipeline_runs: Mapped[list["PipelineRun"]] = relationship(
        back_populates="episode", cascade="all, delete-orphan"
    )

    def __repr__(self) -> str:
        return (
            f"<Episode(id={self.id}, episode_id='{self.episode_id}', status='{self.status.value}')>"
        )


class PipelineRun(Base):
    __tablename__ = "pipeline_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    episode_id: Mapped[int] = mapped_column(Integer, ForeignKey("episodes.id"), nullable=False)
    stage: Mapped[PipelineStage] = mapped_column(Enum(PipelineStage), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        Enum(RunStatus), nullable=False, default=RunStatus.RUNNING
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    episode: Mapped["Episode"] = relationship(back_populates="pipeline_runs")

    def __repr__(self) -> str:
        return (
            f"<PipelineRun(id={self.id}, stage='{self.stage.value}', status='{self.status.value}')>"
        )


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chunk_id: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    token_estimate: Mapped[int] = mapped_column(Integer, nullable=False)
    start_char: Mapped[int] = mapped_column(Integer, nullable=False)
    end_char: Mapped[int] = mapped_column(Integer, nullable=False)

    def __repr__(self) -> str:
        return f"<Chunk(chunk_id='{self.chunk_id}', ordinal={self.ordinal})>"
