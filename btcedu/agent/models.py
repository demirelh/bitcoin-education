"""SQLAlchemy models for agent run tracking."""

from datetime import UTC, datetime

from sqlalchemy import Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from btcedu.db import Base


def _utcnow() -> datetime:
    return datetime.now(UTC)


class AgentRun(Base):
    """A single analysis-plan-execute cycle of the meta-agent."""

    __tablename__ = "agent_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[str] = mapped_column(String(30), default=lambda: _utcnow().isoformat())
    finished_at: Mapped[str | None] = mapped_column(String(30), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="running")  # running|success|failed
    findings_count: Mapped[int] = mapped_column(Integer, default=0)
    issues_created: Mapped[int] = mapped_column(Integer, default=0)
    issues_skipped: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)


class AgentAction(Base):
    """An individual action taken (or skipped) by the agent."""

    __tablename__ = "agent_actions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    created_at: Mapped[str] = mapped_column(String(30), default=lambda: _utcnow().isoformat())
    action_type: Mapped[str] = mapped_column(String(20))  # created|skipped|failed
    title: Mapped[str] = mapped_column(String(200))
    body_hash: Mapped[str] = mapped_column(String(64))  # SHA-256 for dedup
    issue_url: Mapped[str | None] = mapped_column(String(300), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
