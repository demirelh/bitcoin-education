"""Topic continuity, dependencies and rechecks (N6).

A running story is not one broadcast. These tables record how topics relate,
what a published article depends on, and what still has to be looked at again
after the world moved. None of them can approve anything: a recheck may block
or ask for a review, and a merge needs an operator who can be named later.
"""

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


class AliasKind(str, enum.Enum):
    ENTITY = "entity"
    KEYWORD = "keyword"
    EVENT_DATE = "event_date"


class MergeStatus(str, enum.Enum):
    ACTIVE = "active"
    REVERTED = "reverted"


class ProposalStatus(str, enum.Enum):
    OPEN = "open"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class DependencyKind(str, enum.Enum):
    SOURCE_OBSERVATION = "source_observation"
    MEDIA_ASSET = "media_asset"
    SOURCE_REVISION = "source_revision"


class RecheckStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    CLEAR = "clear"
    REVIEW_REQUESTED = "review_requested"
    BLOCKED = "blocked"
    FAILED = "failed"


class IssueStatus(str, enum.Enum):
    OPEN = "open"
    RESOLVED = "resolved"


class TopicAlias(Base):
    """A canonical handle a topic can be recognised by."""

    __tablename__ = "news_topic_aliases"
    __table_args__ = (
        UniqueConstraint("topic_id", "kind", "value", name="uq_news_topic_alias"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    topic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_topics.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class TopicMerge(Base):
    """One operator's decision that two topics are the same story.

    The merged topic is never deleted. Reverting sets this row back rather than
    reconstructing what was thrown away, which is the only way a wrong merge
    stays repairable.
    """

    __tablename__ = "news_topic_merges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    merge_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    primary_topic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_topics.id"), nullable=False, index=True
    )
    merged_topic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_topics.id"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=MergeStatus.ACTIVE.value, index=True
    )
    operator_ref: Mapped[str] = mapped_column(String(200), nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Exactly the links this merge added, so a revert removes those and nothing
    # else. Without it, a source the primary topic already had would be dropped.
    added_source_ids: Mapped[str] = mapped_column(Text, nullable=False, default="[]")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    reverted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    reverted_by: Mapped[str | None] = mapped_column(String(200), nullable=True)


class UpdateProposal(Base):
    """A suggestion that a new broadcast continues an existing topic."""

    __tablename__ = "news_update_proposals"
    __table_args__ = (
        UniqueConstraint(
            "topic_id", "source_revision_id", name="uq_news_update_proposal"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    proposal_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    topic_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_topics.id"), nullable=False, index=True
    )
    source_revision_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_source_revisions.id"), nullable=False, index=True
    )
    score: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    signals: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ProposalStatus.OPEN.value, index=True
    )
    operator_ref: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PublicationDependency(Base):
    """What a published article stands on, so a change can find it again."""

    __tablename__ = "news_publication_dependencies"
    __table_args__ = (
        UniqueConstraint(
            "publication_id", "kind", "ref", name="uq_news_publication_dependency"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    publication_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_publications.id"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    material: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class RecheckJob(Base):
    """A queued reason to look at a publication again.

    A job may end as ``clear``, ``review_requested`` or ``blocked``. There is no
    status that means "approved": nothing in this table can put text in front of
    readers.
    """

    # No uniqueness on (publication, reason, status): two finished jobs for the
    # same reason are a normal history. Duplicate *pending* work is prevented
    # when a job is queued, not by the schema.
    __tablename__ = "news_recheck_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    publication_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("news_publications.id"), nullable=False, index=True
    )
    reason: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=RecheckStatus.PENDING.value, index=True
    )
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class SourceIssue(Base):
    """The inbox: a source or an asset stopped being what it was."""

    __tablename__ = "news_source_issues"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    issue_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    ref: Mapped[str] = mapped_column(String(200), nullable=False, index=True)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=IssueStatus.OPEN.value, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
