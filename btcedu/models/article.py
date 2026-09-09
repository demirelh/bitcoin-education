"""Article revisions and the operator decisions taken on them.

An article is a revision of its own, bound to the editorial revision, the
claim revisions it rests on and the media decisions it displays. Approval is
recorded against the exact hashes that were on screen, so a later change to
text, evidence or pictures cannot inherit an older approval.
"""

from __future__ import annotations

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


class ArticleStatus(str, enum.Enum):
    DRAFT = "draft"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class EditorialDecisionType(str, enum.Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ParagraphKind(str, enum.Enum):
    LEDE = "lede"
    BODY = "body"
    CONTEXT = "context"


class ArticleRevision(Base):
    __tablename__ = "news_article_revisions"
    __table_args__ = (
        UniqueConstraint(
            "editorial_revision_id",
            "language",
            "content_hash",
            "evidence_hash",
            "media_hash",
            name="uq_news_article_revision_content",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_revision_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    editorial_revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_editorial_revisions.id"), nullable=False, index=True
    )
    language: Mapped[str] = mapped_column(String(16), nullable=False, default="tr")
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    lede: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    media_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ArticleStatus.DRAFT.value, index=True
    )
    block_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class ArticleParagraph(Base):
    __tablename__ = "news_article_paragraphs"
    __table_args__ = (
        UniqueConstraint(
            "article_revision_id",
            "position",
            name="uq_news_article_paragraph_position",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_article_revisions.id"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default=ParagraphKind.BODY.value)
    text: Mapped[str] = mapped_column(Text, nullable=False)


class ArticleParagraphClaim(Base):
    """Which approved claim revision a paragraph is allowed to be saying."""

    __tablename__ = "news_article_paragraph_claims"
    __table_args__ = (
        UniqueConstraint(
            "article_paragraph_id",
            "claim_revision_id",
            name="uq_news_article_paragraph_claim",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    article_paragraph_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_article_paragraphs.id"), nullable=False, index=True
    )
    claim_revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_claim_revisions.id"), nullable=False, index=True
    )


class EditorialDecision(Base):
    """An operator's decision, bound to the exact state that was reviewed."""

    __tablename__ = "news_editorial_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    decision_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    article_revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_article_revisions.id"), nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    operator_ref: Mapped[str] = mapped_column(String(128), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewed_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    reviewed_media_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
