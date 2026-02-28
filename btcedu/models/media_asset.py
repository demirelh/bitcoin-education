"""MediaAsset model for tracking generated media files (images, audio, video)."""

import enum
from datetime import UTC, datetime

from sqlalchemy import JSON, Column, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class MediaAssetType(str, enum.Enum):
    """Types of media assets."""

    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"


class MediaAsset(Base):
    """
    Track generated media assets (images, audio, video) per episode/chapter.

    This table stores metadata about all media files generated during
    the video production pipeline, including images from IMAGE_GEN stage,
    audio from TTS stage, and videos from RENDER stage.
    """

    __tablename__ = "media_assets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    episode_id = Column(String(64), nullable=False, index=True)
    asset_type = Column(Enum(MediaAssetType), nullable=False, index=True)
    chapter_id = Column(String(32), nullable=True, index=True)  # Optional: for per-chapter assets
    file_path = Column(Text, nullable=False)  # Relative path from outputs_dir
    mime_type = Column(String(64), nullable=False)  # e.g., "image/png", "audio/mp3"
    size_bytes = Column(Integer, nullable=False)
    duration_seconds = Column(Float, nullable=True)  # For audio/video assets
    metadata = Column(JSON, nullable=True)  # JSON with generation params, cost, etc.
    prompt_version_id = Column(Integer, ForeignKey("prompt_versions.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=lambda: datetime.now(UTC))

    def __repr__(self) -> str:
        return (
            f"<MediaAsset(id={self.id}, episode_id='{self.episode_id}', "
            f"type={self.asset_type.value}, chapter_id='{self.chapter_id}', "
            f"file='{self.file_path}')>"
        )
