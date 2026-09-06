"""AvatarAudioAsset: the provider's copy of one narration part.

HeyGen needs the audio on its own side before it can generate anything, and
that upload is neither free of time nor free of failure modes. It is, however,
free of *charge* — which is exactly why it must not be entangled with the video
order: re-uploading audio is a cheap retry, re-ordering a video is an invoice.

Identity is ``(provider, audio_hash)``, deliberately not the file path. A path
says where the bytes happened to sit on this machine at this moment; the hash
says what was uploaded. An episode re-rendered into a different directory, or a
regeneration attempt of the same take, both find the same asset and skip the
upload.

Nothing secret is stored here: an asset id is an opaque handle that is useless
without the API key, and the key never comes near this table.
"""

import enum
from datetime import UTC, datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from btcedu.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AudioAssetStatus(str, enum.Enum):
    """Whether the provider still has the upload."""

    ACTIVE = "active"
    #: The provider no longer knows the id — expired, purged or revoked. The
    #: row is kept so the next run knows to upload again rather than wondering
    #: why a perfectly good id stopped working.
    EXPIRED = "expired"


class AvatarAudioAsset(Base):
    """One uploaded narration part, addressed by its content hash."""

    __tablename__ = "avatar_audio_assets"
    __table_args__ = (
        UniqueConstraint("provider", "audio_hash", name="uq_avatar_audio_asset_identity"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    audio_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(String(128), nullable=False)

    #: Context only. Never used to find the asset — see the module docstring.
    episode_id: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    scene_id: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AudioAssetStatus.ACTIVE.value, index=True
    )
    expired_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<AvatarAudioAsset(provider='{self.provider}', "
            f"audio_hash='{(self.audio_hash or '')[:12]}', status='{self.status}')>"
        )
