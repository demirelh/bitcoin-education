"""A video built from the same approved editorial revision as the article (N7).

The point of these tables is that a video is not a second piece of journalism.
It carries the claims that were already checked, the media that was already
cleared, and it records which spoken part rests on which claim — so a later
change to a claim or a licence can name the segments it touches instead of
invalidating a whole render on suspicion.

Dramaturgy and length may differ from the article. What may not differ is the
substance, and that is why the edition stores the article's three hashes: text,
evidence and media. Any drift makes the script and render approvals stale.
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


class EditionStatus(str, enum.Enum):
    DRAFT = "draft"
    SCRIPT_APPROVED = "script_approved"
    FINAL_APPROVED = "final_approved"
    BLOCKED = "blocked"
    SUPERSEDED = "superseded"


class EditionDecisionKind(str, enum.Enum):
    """The two decisions this package keeps deliberately apart."""

    SCRIPT = "script"
    FINAL_VIDEO = "final_video"


class EditionDecisionType(str, enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"


class VideoEdition(Base):
    """One video derived from one approved article revision."""

    __tablename__ = "news_video_editions"
    __table_args__ = (
        UniqueConstraint(
            "article_revision_id",
            "script_hash",
            "media_hash",
            name="uq_news_video_edition_state",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    edition_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    article_revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_article_revisions.id"), nullable=False, index=True
    )
    #: Set once an episode has actually rendered this edition. Nullable, because
    #: an edition exists as a plan before any production run touches it.
    episode_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("episodes.id"), nullable=True, index=True
    )
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="tr")
    profile: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    show_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    script_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    media_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=EditionStatus.DRAFT.value, index=True
    )
    block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    #: Path of the accepted rendered file, recorded only when a person approved
    #: that exact file. It is never filled by the renderer itself.
    final_video_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class EditionSegment(Base):
    """One continuous spoken part of the edition."""

    __tablename__ = "news_edition_segments"
    __table_args__ = (
        UniqueConstraint(
            "video_edition_id", "position", name="uq_news_edition_segment_position"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_edition_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_video_editions.id"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    #: Position of the article paragraph this part was derived from, so a text
    #: change can be traced to the segments that repeat it.
    source_paragraph_position: Mapped[int | None] = mapped_column(Integer, nullable=True)


class EditionSegmentClaim(Base):
    """Which approved claim a spoken part is allowed to be asserting."""

    __tablename__ = "news_edition_segment_claims"
    __table_args__ = (
        UniqueConstraint(
            "edition_segment_id",
            "claim_revision_id",
            name="uq_news_edition_segment_claim",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    edition_segment_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_edition_segments.id"), nullable=False, index=True
    )
    claim_revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_claim_revisions.id"), nullable=False, index=True
    )


class EditionMedia(Base):
    """An illustration cleared for this video, with the credit it must show."""

    __tablename__ = "news_edition_media"
    __table_args__ = (
        UniqueConstraint(
            "video_edition_id", "position", name="uq_news_edition_media_position"
        ),
        UniqueConstraint(
            "video_edition_id",
            "media_use_decision_id",
            name="uq_news_edition_media_decision",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    video_edition_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_video_editions.id"), nullable=False, index=True
    )
    media_use_decision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_media_use_decisions.id"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False)
    caption: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Burned into the picture, not only listed in the description.
    on_screen_credit: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Short label such as "ARŞİV" for anything that is not event footage.
    on_screen_notice: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    requires_notice: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")


class EditionDecision(Base):
    """A named person's decision about the script or about the finished file.

    The hashes are stored with the decision because that is what makes it
    revocable by evidence rather than by memory: if the article moved, the
    approval no longer describes anything that exists.
    """

    __tablename__ = "news_edition_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    video_edition_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_video_editions.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)
    operator_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False, default="")
    script_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    media_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    video_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
