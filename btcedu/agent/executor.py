"""Issue executor — creates GitHub Issues via gh CLI with deduplication."""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess
from dataclasses import dataclass

from sqlalchemy.orm import Session

from btcedu.agent.models import AgentAction
from btcedu.agent.planner import Suggestion
from btcedu.config import Settings

logger = logging.getLogger(__name__)

ISSUE_FOOTER = (
    "\n\n---\n"
    "🤖 *This issue was automatically created by the btcedu meta-agent. "
    "Label: `agent-generated`*"
)


@dataclass
class ExecutionResult:
    created: int = 0
    skipped: int = 0
    failed: int = 0


def check_gh_available(repo: str) -> tuple[bool, str]:
    """Check if gh CLI is available and authenticated."""
    try:
        proc = subprocess.run(
            ["gh", "auth", "status"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return False, f"gh not authenticated: {proc.stderr.strip()}"
    except FileNotFoundError:
        return False, "gh CLI not found in PATH"
    except Exception as e:
        return False, f"gh check failed: {e}"
    return True, "ok"


def execute_suggestions(
    suggestions: list[Suggestion],
    settings: Settings,
    db_session: Session,
    run_id: int,
    dry_run: bool = True,
) -> ExecutionResult:
    """Create GitHub Issues for non-duplicate suggestions."""
    result = ExecutionResult()
    repo = settings.agent_github_repo

    if not repo:
        logger.error("agent_github_repo not configured")
        return result

    if not dry_run:
        ok, msg = check_gh_available(repo)
        if not ok:
            logger.error(msg)
            return result

    existing_titles = _get_open_issue_titles(repo, settings.agent_label) if not dry_run else set()

    for suggestion in suggestions:
        body_hash = hashlib.sha256(
            (suggestion.title + suggestion.body).encode()
        ).hexdigest()

        # Check DB dedup
        existing = db_session.query(AgentAction).filter(
            AgentAction.body_hash == body_hash,
            AgentAction.action_type == "created",
        ).first()
        if existing:
            logger.info("Skipping (DB duplicate): %s", suggestion.title)
            _record_action(db_session, run_id, "skipped", suggestion.title, body_hash,
                           reason="duplicate in DB")
            result.skipped += 1
            continue

        # Check GitHub dedup (normalized title match)
        normalized = suggestion.title.lower().strip()
        if any(normalized in t.lower() for t in existing_titles):
            logger.info("Skipping (GitHub duplicate): %s", suggestion.title)
            _record_action(db_session, run_id, "skipped", suggestion.title, body_hash,
                           reason="duplicate on GitHub")
            result.skipped += 1
            continue

        if dry_run:
            logger.info("[DRY-RUN] Would create issue: %s", suggestion.title)
            _record_action(db_session, run_id, "dry_run", suggestion.title, body_hash,
                           reason="dry_run mode")
            result.skipped += 1
            continue

        # Create the issue
        issue_url = _create_issue(repo, suggestion, settings)
        if issue_url:
            logger.info("Created issue: %s → %s", suggestion.title, issue_url)
            _record_action(db_session, run_id, "created", suggestion.title, body_hash,
                           issue_url=issue_url)
            result.created += 1
        else:
            _record_action(db_session, run_id, "failed", suggestion.title, body_hash,
                           reason="gh issue create failed")
            result.failed += 1

    db_session.commit()
    return result


def _get_open_issue_titles(repo: str, label: str) -> set[str]:
    """Fetch open issue titles from GitHub for dedup."""
    try:
        proc = subprocess.run(
            ["gh", "issue", "list", "--repo", repo, "--label", label,
             "--state", "open", "--json", "title", "--limit", "50"],
            capture_output=True, text=True, timeout=30,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            issues = json.loads(proc.stdout)
            return {i["title"] for i in issues}
    except Exception as e:
        logger.warning("Failed to fetch existing issues: %s", e)
    return set()


def _create_issue(repo: str, suggestion: Suggestion, settings: Settings) -> str | None:
    """Create a GitHub issue and return the URL."""
    body = suggestion.body + ISSUE_FOOTER
    labels = [settings.agent_label] + suggestion.labels

    cmd = [
        "gh", "issue", "create",
        "--repo", repo,
        "--title", suggestion.title,
        "--body", body,
        "--assignee", settings.agent_assignee,
    ]
    for label in labels:
        cmd.extend(["--label", label])

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if proc.returncode == 0:
            url = proc.stdout.strip()
            return url if url.startswith("http") else None
        logger.error("gh issue create failed: %s", proc.stderr.strip())
    except Exception as e:
        logger.error("gh issue create error: %s", e)
    return None


def _record_action(
    db_session: Session,
    run_id: int,
    action_type: str,
    title: str,
    body_hash: str,
    issue_url: str | None = None,
    reason: str | None = None,
) -> None:
    action = AgentAction(
        run_id=run_id,
        action_type=action_type,
        title=title,
        body_hash=body_hash,
        issue_url=issue_url,
        reason=reason,
    )
    db_session.add(action)
