"""Human review system: create, approve, reject, and track review tasks."""

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.review import ReviewDecision, ReviewStatus, ReviewTask

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _get_task_or_raise(session: Session, task_id: int) -> ReviewTask:
    """Get a ReviewTask by ID or raise ValueError."""
    task = session.query(ReviewTask).filter(ReviewTask.id == task_id).first()
    if not task:
        raise ValueError(f"Review task not found: {task_id}")
    return task


def _validate_actionable(task: ReviewTask) -> None:
    """Raise ValueError if task is not in an actionable state."""
    actionable = {ReviewStatus.PENDING.value, ReviewStatus.IN_REVIEW.value}
    if task.status not in actionable:
        raise ValueError(
            f"Review task {task.id} is in status '{task.status}', "
            f"cannot act on it (must be pending or in_review)"
        )


def _revert_episode(session: Session, episode_id: str) -> None:
    """Revert episode to previous stage based on current status.

    Reversion map:
    - CORRECTED → TRANSCRIBED (Review Gate 1)
    - ADAPTED → TRANSLATED (Review Gate 2)
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        logger.warning("Cannot revert episode %s: not found", episode_id)
        return

    # Reversion mapping for review gates 1 and 2 only.
    # Review Gate 3 rejections keep the episode at RENDERED.
    _REVERT_MAP = {
        EpisodeStatus.CORRECTED: EpisodeStatus.TRANSCRIBED,  # RG1
        EpisodeStatus.ADAPTED: EpisodeStatus.TRANSLATED,  # RG2
    }

    target_status = _REVERT_MAP.get(episode.status)
    if target_status:
        logger.info(
            "Reverted episode %s from %s to %s",
            episode_id,
            episode.status.value,
            target_status.value,
        )
        episode.status = target_status
    else:
        logger.warning(
            "Episode %s is in status '%s', no reversion mapping defined",
            episode_id,
            episode.status.value,
        )


def _mark_output_stale(task: ReviewTask) -> None:
    """Create .stale marker files for artifact paths.

    The corrector's _is_correction_current() checks for these markers.
    """
    if not task.artifact_paths:
        return

    try:
        paths = json.loads(task.artifact_paths)
    except (json.JSONDecodeError, TypeError):
        return

    for path_str in paths:
        stale_marker = Path(path_str + ".stale")
        try:
            stale_marker.parent.mkdir(parents=True, exist_ok=True)
            stale_marker.write_text(
                json.dumps(
                    {
                        "invalidated_by": "review_rejection",
                        "invalidated_at": _utcnow().isoformat(),
                        "reason": "changes_requested",
                        "review_task_id": task.id,
                    }
                ),
                encoding="utf-8",
            )
        except OSError as e:
            logger.warning("Could not create stale marker %s: %s", stale_marker, e)


def _compute_artifact_hash(paths: list[str]) -> str:
    """Compute SHA-256 hash over file contents of all artifact paths."""
    h = hashlib.sha256()
    for path_str in sorted(paths):
        path = Path(path_str)
        if path.exists():
            h.update(path.read_bytes())
    return h.hexdigest()


def _get_runtime_settings():
    """Return active app settings if available, else default Settings()."""
    try:
        from flask import current_app

        settings = current_app.config.get("settings")
        if settings is not None:
            return settings
    except (ImportError, RuntimeError):
        pass

    from btcedu.config import Settings

    return Settings()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def create_review_task(
    session: Session,
    episode_id: str,
    stage: str,
    artifact_paths: list[str],
    diff_path: str | None = None,
    prompt_version_id: int | None = None,
) -> ReviewTask:
    """Create a new PENDING ReviewTask.

    Args:
        session: DB session.
        episode_id: Episode identifier.
        stage: Pipeline stage (e.g. "correct").
        artifact_paths: List of file paths to review.
        diff_path: Optional path to diff JSON.
        prompt_version_id: Optional FK to PromptVersion.

    Returns:
        The created ReviewTask.
    """
    artifact_hash = _compute_artifact_hash(artifact_paths)

    task = ReviewTask(
        episode_id=episode_id,
        stage=stage,
        status=ReviewStatus.PENDING.value,
        artifact_paths=json.dumps(artifact_paths),
        diff_path=diff_path,
        prompt_version_id=prompt_version_id,
        artifact_hash=artifact_hash,
    )
    session.add(task)
    session.commit()

    logger.info(
        "Created review task %d for episode %s stage '%s'",
        task.id,
        episode_id,
        stage,
    )
    return task


def approve_review(
    session: Session,
    review_task_id: int,
    notes: str | None = None,
) -> ReviewDecision:
    """Approve a review task.

    Sets task status to APPROVED and creates a ReviewDecision.
    Does NOT advance episode status — pipeline checks on next run.

    Returns:
        The created ReviewDecision.
    """
    task = _get_task_or_raise(session, review_task_id)
    _validate_actionable(task)

    now = _utcnow()
    task.status = ReviewStatus.APPROVED.value
    task.reviewed_at = now
    if notes:
        task.reviewer_notes = notes

    # Recompute artifact hash at approval time
    if task.artifact_paths:
        try:
            paths = json.loads(task.artifact_paths)
            task.artifact_hash = _compute_artifact_hash(paths)
        except (json.JSONDecodeError, TypeError):
            pass

    decision = ReviewDecision(
        review_task_id=task.id,
        decision="approved",
        notes=notes,
    )
    session.add(decision)
    session.commit()

    logger.info("Approved review task %d (episode %s)", task.id, task.episode_id)
    return decision


def reject_review(
    session: Session,
    review_task_id: int,
    notes: str | None = None,
) -> ReviewDecision:
    """Reject a review task.

    Sets task status to REJECTED, reverts episode for RG1/RG2 only.
    Render reviews keep the episode at RENDERED.

    Returns:
        The created ReviewDecision.
    """
    task = _get_task_or_raise(session, review_task_id)
    _validate_actionable(task)

    if task.stage == "render" and (notes is None or not notes.strip()):
        raise ValueError("Notes are required when rejecting a render review")

    now = _utcnow()
    task.status = ReviewStatus.REJECTED.value
    task.reviewed_at = now
    if notes:
        task.reviewer_notes = notes

    if task.stage != "render":
        _revert_episode(session, task.episode_id)

    decision = ReviewDecision(
        review_task_id=task.id,
        decision="rejected",
        notes=notes,
    )
    session.add(decision)
    session.commit()

    logger.info("Rejected review task %d (episode %s)", task.id, task.episode_id)
    return decision


def request_changes(
    session: Session,
    review_task_id: int,
    notes: str,
) -> ReviewDecision:
    """Request changes on a review task.

    Sets task status to CHANGES_REQUESTED, stores reviewer notes,
    reverts episode for RG1/RG2, and creates .stale markers on artifacts.
    Render reviews keep the episode at RENDERED.

    Args:
        notes: Required — reviewer feedback for the re-correction.

    Returns:
        The created ReviewDecision.

    Raises:
        ValueError: If notes is empty.
    """
    if not notes or not notes.strip():
        raise ValueError("Notes are required when requesting changes")

    task = _get_task_or_raise(session, review_task_id)
    _validate_actionable(task)

    now = _utcnow()
    task.status = ReviewStatus.CHANGES_REQUESTED.value
    task.reviewed_at = now
    task.reviewer_notes = notes

    if task.stage != "render":
        _revert_episode(session, task.episode_id)
    _mark_output_stale(task)

    decision = ReviewDecision(
        review_task_id=task.id,
        decision="changes_requested",
        notes=notes,
    )
    session.add(decision)
    session.commit()

    logger.info(
        "Requested changes on review task %d (episode %s)",
        task.id,
        task.episode_id,
    )
    return decision


def get_pending_reviews(session: Session) -> list[ReviewTask]:
    """Return all PENDING and IN_REVIEW tasks, newest first."""
    return (
        session.query(ReviewTask)
        .filter(ReviewTask.status.in_([ReviewStatus.PENDING.value, ReviewStatus.IN_REVIEW.value]))
        .order_by(ReviewTask.created_at.desc())
        .all()
    )


def get_review_detail(session: Session, review_task_id: int) -> dict:
    """Return detailed information about a review task.

    Returns dict with: task info, episode title, diff data, original text,
    artifact content, and decision history.
    """
    task = _get_task_or_raise(session, review_task_id)

    episode = session.query(Episode).filter(Episode.episode_id == task.episode_id).first()

    # Load diff data
    diff_data = None
    if task.diff_path:
        diff_file = Path(task.diff_path)
        if diff_file.exists():
            try:
                diff_data = json.loads(diff_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                diff_data = {"error": "Could not load diff file"}

    # Load original transcript text
    original_text = None
    if episode and episode.transcript_path:
        transcript_file = Path(episode.transcript_path)
        if transcript_file.exists():
            try:
                original_text = transcript_file.read_text(encoding="utf-8")
            except OSError:
                pass

    # Load corrected text from first artifact
    corrected_text = None
    if task.artifact_paths:
        try:
            paths = json.loads(task.artifact_paths)
            if paths:
                artifact_file = Path(paths[0])
                if artifact_file.exists():
                    corrected_text = artifact_file.read_text(encoding="utf-8")
        except (json.JSONDecodeError, TypeError, OSError):
            pass

    # Decision history
    decisions = [
        {
            "id": d.id,
            "decision": d.decision,
            "notes": d.notes,
            "decided_at": d.decided_at.isoformat() if d.decided_at else None,
        }
        for d in task.decisions
    ]

    # Video-specific fields for render review (Review Gate 3)
    video_url = None
    render_manifest = None
    chapter_script = None
    if task.stage == "render" and episode:
        # Check if draft.mp4 exists
        settings = _get_runtime_settings()
        draft_path = Path(settings.outputs_dir) / episode.episode_id / "render" / "draft.mp4"
        if draft_path.exists():
            video_url = f"/api/episodes/{episode.episode_id}/render/draft.mp4"

        # Load render manifest
        manifest_path = (
            Path(settings.outputs_dir) / episode.episode_id / "render" / "render_manifest.json"
        )
        if manifest_path.exists():
            try:
                render_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                render_manifest = {"error": "Could not load render manifest"}

        chapters_path = Path(settings.outputs_dir) / episode.episode_id / "chapters.json"
        if chapters_path.exists():
            try:
                chapters_data = json.loads(chapters_path.read_text(encoding="utf-8"))
                chapters = chapters_data.get("chapters", [])
                chapter_script = [
                    {
                        "chapter_id": ch.get("chapter_id"),
                        "title": ch.get("title"),
                        "order": ch.get("order"),
                        "text": (ch.get("narration") or {}).get("text"),
                    }
                    for ch in chapters
                ]
            except (json.JSONDecodeError, OSError, TypeError):
                chapter_script = None

    return {
        "id": task.id,
        "episode_id": task.episode_id,
        "episode_title": episode.title if episode else None,
        "stage": task.stage,
        "status": task.status,
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "reviewed_at": task.reviewed_at.isoformat() if task.reviewed_at else None,
        "reviewer_notes": task.reviewer_notes,
        "artifact_hash": task.artifact_hash,
        "diff": diff_data,
        "original_text": original_text,
        "corrected_text": corrected_text,
        "decisions": decisions,
        "video_url": video_url,  # Sprint 10: for render review
        "render_manifest": render_manifest,  # Sprint 10: for render review
        "chapter_script": chapter_script,  # Sprint 10: for render review
    }


def get_latest_reviewer_feedback(
    session: Session,
    episode_id: str,
    stage: str,
) -> str | None:
    """Return reviewer notes from the most recent CHANGES_REQUESTED task.

    Used by corrector to inject feedback into re-correction prompts.

    Returns:
        The reviewer notes string, or None if no feedback exists.
    """
    task = (
        session.query(ReviewTask)
        .filter(
            ReviewTask.episode_id == episode_id,
            ReviewTask.stage == stage,
            ReviewTask.status == ReviewStatus.CHANGES_REQUESTED.value,
        )
        .order_by(ReviewTask.created_at.desc())
        .first()
    )
    if task and task.reviewer_notes:
        return task.reviewer_notes
    return None


def has_approved_review(
    session: Session,
    episode_id: str,
    stage: str,
) -> bool:
    """True if the latest ReviewTask for episode+stage is APPROVED."""
    task = (
        session.query(ReviewTask)
        .filter(
            ReviewTask.episode_id == episode_id,
            ReviewTask.stage == stage,
        )
        .order_by(ReviewTask.created_at.desc())
        .first()
    )
    return task is not None and task.status == ReviewStatus.APPROVED.value


def has_pending_review(
    session: Session,
    episode_id: str,
) -> bool:
    """True if any PENDING or IN_REVIEW task exists for this episode."""
    count = (
        session.query(ReviewTask)
        .filter(
            ReviewTask.episode_id == episode_id,
            ReviewTask.status.in_([ReviewStatus.PENDING.value, ReviewStatus.IN_REVIEW.value]),
        )
        .count()
    )
    return count > 0


def pending_review_count(session: Session) -> int:
    """Count of PENDING + IN_REVIEW tasks. Used for dashboard badge."""
    return (
        session.query(ReviewTask)
        .filter(ReviewTask.status.in_([ReviewStatus.PENDING.value, ReviewStatus.IN_REVIEW.value]))
        .count()
    )
