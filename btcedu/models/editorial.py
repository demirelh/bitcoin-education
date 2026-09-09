"""Durable editorial source, claim and research ledger models."""

from __future__ import annotations

import enum
from datetime import UTC, datetime

from sqlalchemy import (
    DateTime,
    Float,
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


class ResearchRunStatus(str, enum.Enum):
    RESERVED = "reserved"
    RUNNING = "running"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    FAILED = "failed"


class ProviderOperationStatus(str, enum.Enum):
    RESERVED = "reserved"
    SUBMITTED = "submitted"
    COMPLETED = "completed"
    RECONCILE_REQUIRED = "reconcile_required"
    FAILED = "failed"


class SourceItem(Base):
    __tablename__ = "news_source_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    source_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    source_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    episode_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SourceRevision(Base):
    __tablename__ = "news_source_revisions"
    __table_args__ = (
        UniqueConstraint("source_item_id", "content_hash", name="uq_news_source_revision_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    source_item_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_source_items.id"), nullable=False, index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(16), nullable=False, default="1.0")
    language: Mapped[str] = mapped_column(String(16), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    source_uri: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retrieved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class SourceSpan(Base):
    __tablename__ = "news_source_spans"
    __table_args__ = (
        UniqueConstraint(
            "source_revision_id", "span_key", name="uq_news_source_span_revision_key"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    span_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    source_revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_source_revisions.id"), nullable=False, index=True
    )
    span_key: Mapped[str] = mapped_column(String(200), nullable=False)
    story_id: Mapped[str] = mapped_column(String(128), nullable=False)
    source_segment_ids_json: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    source_text: Mapped[str] = mapped_column(Text, nullable=False)
    text_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    start_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    end_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    start_char: Mapped[int | None] = mapped_column(Integer, nullable=True)
    end_char: Mapped[int | None] = mapped_column(Integer, nullable=True)


class Topic(Base):
    __tablename__ = "news_topics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    topic_key: Mapped[str] = mapped_column(String(500), nullable=False, unique=True)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class TopicSource(Base):
    __tablename__ = "news_topic_sources"
    __table_args__ = (
        UniqueConstraint(
            "topic_id", "source_revision_id", name="uq_news_topic_source_revision"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_topics.id"), nullable=False, index=True
    )
    source_revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_source_revisions.id"), nullable=False, index=True
    )


class Claim(Base):
    __tablename__ = "news_claims"
    __table_args__ = (
        UniqueConstraint("topic_id", "claim_key", name="uq_news_claim_topic_key"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    claim_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    topic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_topics.id"), nullable=False, index=True
    )
    claim_key: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class ClaimRevision(Base):
    __tablename__ = "news_claim_revisions"
    __table_args__ = (
        UniqueConstraint("claim_id", "content_hash", name="uq_news_claim_revision_hash"),
        UniqueConstraint(
            "claim_id", "revision_number", name="uq_news_claim_revision_number"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    claim_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_claims.id"), nullable=False, index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    claim_type: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    location: Mapped[str | None] = mapped_column(String(300), nullable=True)
    event_date: Mapped[str | None] = mapped_column(String(64), nullable=True)
    numeric_value: Mapped[str | None] = mapped_column(String(128), nullable=True)
    unit: Mapped[str | None] = mapped_column(String(64), nullable=True)
    attribution: Mapped[str | None] = mapped_column(String(500), nullable=True)
    modality: Mapped[str | None] = mapped_column(String(64), nullable=True)
    material: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class ClaimOrigin(Base):
    __tablename__ = "news_claim_origins"
    __table_args__ = (
        UniqueConstraint(
            "claim_revision_id", "source_span_id", name="uq_news_claim_origin_span"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    claim_revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_claim_revisions.id"), nullable=False, index=True
    )
    source_span_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_source_spans.id"), nullable=False, index=True
    )


class ResearchRun(Base):
    __tablename__ = "news_research_runs"
    __table_args__ = (
        UniqueConstraint(
            "topic_id",
            "input_hash",
            "policy_version",
            "model_name",
            name="uq_news_research_run_input",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    topic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_topics.id"), nullable=False, index=True
    )
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ResearchRunStatus.RESERVED.value, index=True
    )
    max_queries: Mapped[int] = mapped_column(Integer, nullable=False)
    used_queries: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_cost_usd: Mapped[float] = mapped_column(Float, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class ProviderOperation(Base):
    __tablename__ = "news_provider_operations"
    __table_args__ = (
        UniqueConstraint(
            "research_run_id", "operation_key", name="uq_news_provider_operation_key"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operation_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    research_run_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_research_runs.id"), nullable=False, index=True
    )
    operation_key: Mapped[str] = mapped_column(String(128), nullable=False)
    operation_type: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    model_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default=ProviderOperationStatus.RESERVED.value,
        index=True,
    )
    provider_operation_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    actual_cost_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    usage_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_ref: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    reserved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EditorialRevision(Base):
    __tablename__ = "news_editorial_revisions"
    __table_args__ = (
        UniqueConstraint("topic_id", "content_hash", name="uq_news_editorial_revision_hash"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    revision_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    topic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_topics.id"), nullable=False, index=True
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="draft")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class RevisionClaim(Base):
    __tablename__ = "news_revision_claims"
    __table_args__ = (
        UniqueConstraint(
            "editorial_revision_id",
            "claim_revision_id",
            name="uq_news_revision_claim",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    editorial_revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_editorial_revisions.id"), nullable=False, index=True
    )
    claim_revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_claim_revisions.id"), nullable=False, index=True
    )
