"""Public identity, releases and corrections for the newsroom site (N5).

The private tables answer "what did the newsroom do"; these answer "what is
publicly claimed right now". They are deliberately separate: a public build
reads only from here, never from a draft, and a withdrawal has to leave a
durable trace rather than delete a row.
"""

import enum
from datetime import UTC, datetime

from sqlalchemy import (
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


class PublicationStatus(str, enum.Enum):
    PUBLISHED = "published"
    CORRECTED = "corrected"
    WITHDRAWN = "withdrawn"


class ReleaseStatus(str, enum.Enum):
    BUILDING = "building"
    BUILT = "built"
    LIVE = "live"
    SUPERSEDED = "superseded"
    FAILED = "failed"


class CorrectionKind(str, enum.Enum):
    CORRECTION = "correction"
    WITHDRAWAL = "withdrawal"


class Publication(Base):
    """The stable public identity of one topic.

    The slug never changes once it exists, because a URL that has been read is
    a promise. A later article revision publishes under the same slug; that is
    what makes a correction a correction rather than a second article.
    """

    __tablename__ = "news_publications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    publication_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    topic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_topics.id"), nullable=False, unique=True
    )
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    section: Mapped[str] = mapped_column(String(64), nullable=False, default="haber")
    language: Mapped[str] = mapped_column(String(8), nullable=False, default="tr")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PublicationStatus.PUBLISHED.value, index=True
    )
    current_article_revision_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("news_article_revisions.id"), nullable=True
    )
    first_published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class PublicationVersion(Base):
    """One article revision that was actually offered to the public."""

    __tablename__ = "news_publication_versions"
    __table_args__ = (
        UniqueConstraint(
            "publication_id",
            "article_revision_id",
            name="uq_news_publication_version",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    version_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    publication_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_publications.id"), nullable=False, index=True
    )
    article_revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_article_revisions.id"), nullable=False, index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    media_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class CorrectionNotice(Base):
    """A visible note that something published was wrong or is gone.

    Withdrawal keeps its row: the tombstone the site serves is generated from
    it, so removing the article never means removing the record that it existed.
    """

    __tablename__ = "news_correction_notices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    notice_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    publication_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_publications.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(
        String(32), nullable=False, default=CorrectionKind.CORRECTION.value
    )
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    operator_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SiteRelease(Base):
    """One complete static build of the public site."""

    __tablename__ = "news_site_releases"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    release_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    directory: Mapped[str] = mapped_column(String(1000), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ReleaseStatus.BUILDING.value, index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    article_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    operator_ref: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    switched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
