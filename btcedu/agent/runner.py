"""Agent runner — orchestrates analyze → plan → execute cycle."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.agent.analyzer import run_analysis
from btcedu.agent.executor import execute_suggestions
from btcedu.agent.models import AgentRun
from btcedu.agent.planner import generate_suggestions
from btcedu.config import Settings

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def run_once(
    db_session: Session,
    settings: Settings,
    project_root: Path | None = None,
    dry_run: bool | None = None,
) -> AgentRun:
    """Execute a single analyze → plan → execute cycle.

    Returns the AgentRun record with results.
    """
    if project_root is None:
        project_root = Path.cwd()

    if dry_run is None:
        dry_run = settings.agent_dry_run

    run = AgentRun(status="running")
    db_session.add(run)
    db_session.flush()  # get run.id

    try:
        # Phase 1: Analyze
        logger.info("🔍 Phase 1: Analyzing project...")
        analysis = run_analysis(project_root)

        if analysis.error:
            logger.warning("Analysis had errors: %s", analysis.error)

        if not analysis.has_findings:
            logger.info("✅ No findings — project looks clean!")
            run.status = "success"
            run.findings_count = 0
            run.finished_at = _utcnow().isoformat()
            db_session.commit()
            return run

        summary = analysis.to_summary()
        finding_count = (
            len(analysis.ruff_errors)
            + len(analysis.todo_items)
            + len(analysis.large_files)
            + (0 if analysis.test_passed else 1)
        )
        run.findings_count = finding_count
        logger.info("📊 Found %d findings", finding_count)

        # Phase 2: Plan
        logger.info("🧠 Phase 2: Generating improvement suggestions...")
        suggestions = generate_suggestions(
            analysis_summary=summary,
            settings=settings,
            max_issues=settings.agent_max_issues_per_run,
        )
        logger.info("📝 Planner returned %d suggestions", len(suggestions))

        if not suggestions:
            run.status = "success"
            run.finished_at = _utcnow().isoformat()
            db_session.commit()
            return run

        # Phase 3: Execute
        mode = "DRY-RUN" if dry_run else "LIVE"
        logger.info("🚀 Phase 3: Executing suggestions (%s)...", mode)
        result = execute_suggestions(
            suggestions=suggestions,
            settings=settings,
            db_session=db_session,
            run_id=run.id,
            dry_run=dry_run,
        )

        run.issues_created = result.created
        run.issues_skipped = result.skipped
        run.status = "success"
        logger.info(
            "✅ Done: %d created, %d skipped, %d failed",
            result.created,
            result.skipped,
            result.failed,
        )

    except Exception as e:
        logger.error("Agent run failed: %s", e, exc_info=True)
        run.status = "failed"
        run.error = str(e)[:500]

    run.finished_at = _utcnow().isoformat()
    db_session.commit()
    return run
