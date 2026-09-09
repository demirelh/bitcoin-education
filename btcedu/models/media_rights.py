"""Durable media rights ledger for editorial illustrations.

Bytes, offers, licence evidence and use decisions are separate records on
purpose. The same picture may be offered by several sources under different
licence claims, and a decision must stay attributable to the exact evidence
it was taken from, even after the offer page changes.
"""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from btcedu.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MediaUseStatus(str, enum.Enum):
    APPROVED = "approved"
    BLOCKED = "blocked"
    MANUAL_REVIEW = "manual_review"
    REVOKED = "revoked"


class MediaRole(str, enum.Enum):
    EVENT = "event"
    ARCHIVE = "archive"
    SYMBOLIC = "symbolic"
    PORTRAIT = "portrait"


class LicenseFamily(str, enum.Enum):
    PUBLIC_DOMAIN = "public_domain"
    CC0 = "cc0"
    CC_BY = "cc_by"
    CC_BY_SA = "cc_by_sa"
    CC_BY_NC = "cc_by_nc"
    CC_BY_ND = "cc_by_nd"
    UNKNOWN = "unknown"


class NewsroomMediaAsset(Base):
    """Exactly one row per distinct byte sequence."""

    __tablename__ = "news_media_assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    blob_path: Mapped[str] = mapped_column(String(1000), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class MediaSourceOffer(Base):
    """One provider's offer of an asset, with its own metadata and licence claim."""

    __tablename__ = "news_media_source_offers"
    __table_args__ = (
        UniqueConstraint("provider", "offer_key", name="uq_news_media_offer_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    offer_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    media_asset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_media_assets.id"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    offer_key: Mapped[str] = mapped_column(String(500), nullable=False)
    page_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    file_url: Mapped[str] = mapped_column(String(2000), nullable=False)
    title: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    uploader: Mapped[str | None] = mapped_column(String(500), nullable=True)
    author: Mapped[str | None] = mapped_column(String(500), nullable=True)
    captured_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    captured_at_text: Mapped[str | None] = mapped_column(String(200), nullable=True)
    depicted_subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    depicted_location: Mapped[str | None] = mapped_column(String(500), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    metadata_json: Mapped[str | None] = mapped_column(Text, nullable=True)


class LicenseEvidence(Base):
    """What a source claimed about the rights, exactly as read and when."""

    __tablename__ = "news_license_evidence"
    __table_args__ = (
        UniqueConstraint(
            "media_source_offer_id",
            "evidence_hash",
            name="uq_news_license_evidence_offer",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    evidence_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    media_source_offer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_media_source_offers.id"), nullable=False, index=True
    )
    license_family: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    license_id: Mapped[str] = mapped_column(String(128), nullable=False)
    license_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    license_url: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    attribution_required: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    share_alike: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    commercial_use_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    derivatives_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    credit_author: Mapped[str | None] = mapped_column(String(500), nullable=True)
    credit_source: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    raw_statement: Mapped[str | None] = mapped_column(Text, nullable=True)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class MediaUseDecision(Base):
    """A decision to use, block or hold one asset for one editorial revision."""

    __tablename__ = "news_media_use_decisions"
    __table_args__ = (
        UniqueConstraint(
            "editorial_revision_id",
            "media_asset_id",
            name="uq_news_media_decision_revision_asset",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    editorial_revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_editorial_revisions.id"), nullable=False, index=True
    )
    media_asset_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_media_assets.id"), nullable=False, index=True
    )
    media_source_offer_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_media_source_offers.id"), nullable=False, index=True
    )
    license_evidence_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_license_evidence.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    attribution_text: Mapped[str] = mapped_column(Text, nullable=False)
    edit_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    derivative_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_by: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)


class RevisionMedia(Base):
    """Ordered, approved illustrations bound to one editorial revision."""

    __tablename__ = "news_revision_media"
    __table_args__ = (
        UniqueConstraint(
            "editorial_revision_id",
            "media_use_decision_id",
            name="uq_news_revision_media_decision",
        ),
        UniqueConstraint(
            "editorial_revision_id",
            "position",
            name="uq_news_revision_media_position",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    editorial_revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_editorial_revisions.id"), nullable=False, index=True
    )
    media_use_decision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_media_use_decisions.id"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    caption: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
