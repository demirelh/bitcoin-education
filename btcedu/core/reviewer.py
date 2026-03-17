"""Human review system: create, approve, reject, and track review tasks."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.review import ReviewDecision, ReviewStatus, ReviewTask

if TYPE_CHECKING:
    from btcedu.models.review_item import ReviewItemDecision

logger = logging.getLogger(__name__)

# News editorial review checklist for tagesschau_tr profile
_NEWS_REVIEW_CHECKLIST = [
    {"id": "factual_accuracy", "label": "Factual accuracy verified"},
    {"id": "political_neutrality", "label": "No editorialization or political spin"},
    {"id": "attribution_present", "label": "Source attribution included"},
    {"id": "proper_nouns_correct", "label": "Names, places, institutions correct"},
    {"id": "no_hallucination", "label": "No invented facts or figures"},
    {"id": "register_correct", "label": "Formal news register (not conversational)"},
]


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

    # Reversion mapping for review gates 1, 2, and translate only.
    # Review Gate 3 rejections keep the episode at RENDERED.
    _REVERT_MAP = {
        EpisodeStatus.CORRECTED: EpisodeStatus.TRANSCRIBED,  # RG1
        EpisodeStatus.ADAPTED: EpisodeStatus.TRANSLATED,  # RG2
        EpisodeStatus.TRANSLATED: EpisodeStatus.SEGMENTED,  # RG_translate (news profiles)
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


# Regex: strip everything except word characters and whitespace
_STRIP_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)

# Max changes for auto-approve eligibility
_AUTO_APPROVE_MAX_CHANGES = 5


def _is_punctuation_only(original: str, corrected: str) -> bool:
    """Return True if the only difference is punctuation characters."""
    stripped_orig = _STRIP_PUNCT_RE.sub("", original).split()
    stripped_corr = _STRIP_PUNCT_RE.sub("", corrected).split()
    return stripped_orig == stripped_corr


def _is_minor_correction(diff_path: str | None) -> bool:
    """Check if a correction diff qualifies for auto-approval.

    A correction is minor if:
    1. The diff file exists and is valid JSON.
    2. Total changes < _AUTO_APPROVE_MAX_CHANGES.
    3. Every change is punctuation-only (no word content changed).

    Returns False conservatively on any error.
    """
    if not diff_path:
        return False

    path = Path(diff_path)
    if not path.exists():
        return False

    try:
        diff_data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    changes = diff_data.get("changes", [])
    total = diff_data.get("summary", {}).get("total_changes", len(changes))

    if total == 0:
        # Zero changes — trivially minor
        return True

    if total >= _AUTO_APPROVE_MAX_CHANGES:
        return False

    # Every change must be punctuation-only
    for change in changes:
        original = change.get("original", "")
        corrected = change.get("corrected", "")
        if not _is_punctuation_only(original, corrected):
            return False

    return True


def _write_review_history(task: ReviewTask, decision: ReviewDecision) -> None:
    """Append a review decision to the episode's review_history.json.

    Creates the file if it doesn't exist. Each entry captures the decision,
    reviewer notes, and timestamps for file-level audit trail.
    """
    settings = _get_runtime_settings()
    history_path = Path(settings.outputs_dir) / task.episode_id / "review" / "review_history.json"

    # Load existing history or start fresh
    history: list[dict] = []
    if history_path.exists():
        try:
            history = json.loads(history_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            history = []

    entry = {
        "review_task_id": task.id,
        "episode_id": task.episode_id,
        "stage": task.stage,
        "decision": decision.decision,
        "notes": decision.notes,
        "decided_at": decision.decided_at.isoformat() if decision.decided_at else None,
        "artifact_hash": task.artifact_hash,
        "task_status": task.status,
    }
    history.append(entry)

    try:
        history_path.parent.mkdir(parents=True, exist_ok=True)
        history_path.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as e:
        logger.warning("Could not write review history to %s: %s", history_path, e)


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

    # Auto-approve minor corrections (MASTERPLAN §9.4)
    if stage == "correct" and _is_minor_correction(diff_path):
        logger.info(
            "Auto-approving review task %d — minor correction (<5 punctuation-only changes)",
            task.id,
        )
        approve_review(session, task.id, notes="auto-approved: minor punctuation-only correction")

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

    _write_review_history(task, decision)

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

    # Revert for stages that have a reversion mapping (RG1, RG2).
    # stock_images and render rejections keep the episode at current status.
    if task.stage not in ("render", "stock_images"):
        _revert_episode(session, task.episode_id)

    decision = ReviewDecision(
        review_task_id=task.id,
        decision="rejected",
        notes=notes,
    )
    session.add(decision)
    session.commit()

    _write_review_history(task, decision)

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

    if task.stage not in ("render", "stock_images"):
        _revert_episode(session, task.episode_id)
    _mark_output_stale(task)

    decision = ReviewDecision(
        review_task_id=task.id,
        decision="changes_requested",
        notes=notes,
    )
    session.add(decision)
    session.commit()

    _write_review_history(task, decision)

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

    # Load per-item decisions (Phase 5; also for translation review)
    item_decisions_map: dict = {}
    if task.stage in ("correct", "adapt", "translate"):
        from btcedu.models.review_item import ReviewItemDecision

        records = (
            session.query(ReviewItemDecision)
            .filter(ReviewItemDecision.review_task_id == task.id)
            .all()
        )
        item_decisions_map = {
            r.item_id: {
                "action": r.action,
                "edited_text": r.edited_text,
                "decided_at": r.decided_at.isoformat() if r.decided_at else None,
            }
            for r in records
        }

    # Profile-aware review checklist (tagesschau news content)
    content_profile = getattr(episode, "content_profile", "bitcoin_podcast") if episode else None
    review_checklist = None
    if content_profile == "tagesschau_tr":
        review_checklist = _NEWS_REVIEW_CHECKLIST

    # Translation-specific bilingual review data
    review_mode = None
    bilingual_stories = None
    compression_ratio = None
    translation_warnings = None
    if task.stage == "translate" and diff_data and diff_data.get("diff_type") == "translation":
        review_mode = "bilingual"
        bilingual_stories = diff_data.get("stories", [])
        compression_ratio = diff_data.get("summary", {}).get("compression_ratio")
        translation_warnings = diff_data.get("warnings", [])

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
        "item_decisions": item_decisions_map,  # Phase 5: per-item review actions
        "video_url": video_url,  # Sprint 10: for render review
        "render_manifest": render_manifest,  # Sprint 10: for render review
        "chapter_script": chapter_script,  # Sprint 10: for render review
        "review_checklist": review_checklist,  # Phase 3: news editorial checklist
        "review_mode": review_mode,  # Phase 3: "bilingual" for translation reviews
        "stories": bilingual_stories,  # Phase 3: bilingual story pairs
        "compression_ratio": compression_ratio,  # Phase 3: TR/DE word ratio
        "translation_warnings": translation_warnings,  # Phase 3: anomaly warnings
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


# ---------------------------------------------------------------------------
# Phase 5: Granular item-level review
# ---------------------------------------------------------------------------


def upsert_item_decision(
    session: Session,
    review_task_id: int,
    item_id: str,
    action: str,
    edited_text: str | None = None,
) -> ReviewItemDecision:
    """Create or update a per-item decision.

    On first call for a given (review_task_id, item_id): creates record,
    sets decided_at to now.
    On subsequent calls: updates action (and edited_text), updates decided_at.

    Args:
        session: DB session.
        review_task_id: FK to review_tasks.id.
        item_id: Stable item identifier from diff JSON (e.g. "corr-0042").
        action: One of ReviewItemAction values.
        edited_text: Required when action == "edited", else None.

    Returns:
        The created or updated ReviewItemDecision.

    Raises:
        ValueError: If review task not found or not actionable.
    """
    from btcedu.models.review_item import ReviewItemAction, ReviewItemDecision

    task = _get_task_or_raise(session, review_task_id)
    _validate_actionable(task)

    existing = (
        session.query(ReviewItemDecision)
        .filter(
            ReviewItemDecision.review_task_id == review_task_id,
            ReviewItemDecision.item_id == item_id,
        )
        .first()
    )

    now = _utcnow()

    if existing:
        existing.action = action
        existing.edited_text = edited_text if action == ReviewItemAction.EDITED.value else None
        existing.decided_at = now
        session.commit()
        return existing
    else:
        original_text, proposed_text, operation_type = _load_item_texts_from_diff(task, item_id)
        record = ReviewItemDecision(
            review_task_id=review_task_id,
            item_id=item_id,
            operation_type=operation_type,
            original_text=original_text,
            proposed_text=proposed_text,
            action=action,
            edited_text=edited_text if action == ReviewItemAction.EDITED.value else None,
            decided_at=now,
        )
        session.add(record)
        session.commit()
        return record


def get_item_decisions(
    session: Session,
    review_task_id: int,
) -> dict[str, ReviewItemDecision]:
    """Return all item decisions for a review task, keyed by item_id.

    Returns empty dict if no decisions exist yet.
    """
    from btcedu.models.review_item import ReviewItemDecision

    records = (
        session.query(ReviewItemDecision)
        .filter(ReviewItemDecision.review_task_id == review_task_id)
        .all()
    )
    return {r.item_id: r for r in records}


def apply_item_decisions(
    session: Session,
    review_task_id: int,
) -> str:
    """Assemble final reviewed text from per-item decisions and write sidecar file.

    Pending items default to accepting the proposed change.

    Args:
        session: DB session.
        review_task_id: FK to review_tasks.id.

    Returns:
        Absolute path string to the written sidecar file.

    Raises:
        ValueError: If review task not found, diff file missing, or source text missing.
    """
    task = _get_task_or_raise(session, review_task_id)
    item_decisions = get_item_decisions(session, review_task_id)

    if not task.diff_path:
        raise ValueError(f"Review task {review_task_id} has no diff_path")

    diff_file = Path(task.diff_path)
    if not diff_file.exists():
        raise ValueError(f"Diff file not found: {task.diff_path}")

    diff_data = json.loads(diff_file.read_text(encoding="utf-8"))
    settings = _get_runtime_settings()

    if task.stage == "correct":
        episode = session.query(Episode).filter(Episode.episode_id == task.episode_id).first()
        if not episode or not episode.transcript_path:
            raise ValueError(f"Original transcript not found for episode {task.episode_id}")
        original_text = Path(episode.transcript_path).read_text(encoding="utf-8")
        changes = diff_data.get("changes", [])
        _ensure_item_ids_correction(changes)
        reviewed = _assemble_correction_review(original_text, changes, item_decisions)
        out_path = _sidecar_path(task.episode_id, "correct", settings)

    elif task.stage == "adapt":
        adapted_path = Path(settings.outputs_dir) / task.episode_id / "script.adapted.tr.md"
        if not adapted_path.exists():
            raise ValueError(f"Adapted script not found: {adapted_path}")
        adapted_text = adapted_path.read_text(encoding="utf-8")
        adaptations = diff_data.get("adaptations", [])
        _ensure_item_ids_adaptation(adaptations)
        reviewed = _assemble_adaptation_review(adapted_text, adaptations, item_decisions)
        out_path = _sidecar_path(task.episode_id, "adapt", settings)

    elif task.stage == "translate":
        # Find stories_translated.json in artifact_paths
        stories_path = None
        if task.artifact_paths:
            try:
                paths = json.loads(task.artifact_paths)
                for p in paths:
                    if p.endswith("stories_translated.json"):
                        stories_path = p
                        break
            except (json.JSONDecodeError, TypeError):
                pass
        if not stories_path or not Path(stories_path).exists():
            raise ValueError(
                f"stories_translated.json not found in artifact_paths for task {task.id}"
            )
        stories_data = json.loads(Path(stories_path).read_text(encoding="utf-8"))
        reviewed_dict = _assemble_translation_review(stories_data, diff_data, item_decisions)
        out_path = (
            Path(settings.outputs_dir)
            / task.episode_id
            / "review"
            / "stories_translated.reviewed.json"
        )
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(reviewed_dict, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("Wrote reviewed translation sidecar to %s", out_path)
        return str(out_path.resolve())

    else:
        raise ValueError(f"apply_item_decisions not supported for stage '{task.stage}'")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(reviewed, encoding="utf-8")
    logger.info("Wrote reviewed sidecar to %s", out_path)
    return str(out_path)


def _sidecar_path(episode_id: str, stage: str, settings) -> Path:
    """Return the sidecar file path for a reviewed artifact.

    correction stage → data/outputs/{ep_id}/review/transcript.reviewed.de.txt
    adapt stage      → data/outputs/{ep_id}/review/script.adapted.reviewed.tr.md
    """
    base = Path(settings.outputs_dir) / episode_id / "review"
    if stage == "correct":
        return base / "transcript.reviewed.de.txt"
    elif stage == "adapt":
        return base / "script.adapted.reviewed.tr.md"
    else:
        raise ValueError(f"No sidecar path defined for stage '{stage}'")


def _load_item_texts_from_diff(
    task: ReviewTask,
    item_id: str,
) -> tuple[str | None, str | None, str]:
    """Extract original_text, proposed_text, operation_type for a given item_id.

    Returns (original_text, proposed_text, operation_type).
    Falls back to (None, None, "unknown") if item not found.
    """
    if not task.diff_path:
        return (None, None, "unknown")
    diff_file = Path(task.diff_path)
    if not diff_file.exists():
        return (None, None, "unknown")
    try:
        diff_data = json.loads(diff_file.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return (None, None, "unknown")

    if task.stage == "correct":
        changes = diff_data.get("changes", [])
        _ensure_item_ids_correction(changes)
        for c in changes:
            if c.get("item_id") == item_id:
                return (c.get("original", ""), c.get("corrected", ""), c.get("type", "replace"))
    elif task.stage == "adapt":
        adaptations = diff_data.get("adaptations", [])
        _ensure_item_ids_adaptation(adaptations)
        for a in adaptations:
            if a.get("item_id") == item_id:
                return (a.get("original", ""), a.get("adapted", ""), a.get("tier", "T1"))
    elif diff_data.get("diff_type") == "translation":
        # Translation diff: look in "stories" list
        for story in diff_data.get("stories", []):
            if story.get("item_id") == item_id:
                return (
                    story.get("text_de"),
                    story.get("text_tr"),
                    story.get("category", "story"),
                )

    return (None, None, "unknown")


def _assemble_translation_review(
    stories_data: dict,
    diff_data: dict,
    item_decisions: dict[str, ReviewItemDecision],
) -> dict:
    """Reconstruct stories_translated.json from per-story review decisions.

    - ACCEPTED / PENDING: keep text_tr and headline_tr as-is
    - EDITED: replace text_tr with decision.edited_text
    - REJECTED / UNCHANGED: prepend rejection marker to text_tr
    """
    from btcedu.models.review_item import ReviewItemAction

    result = dict(stories_data)
    result["stories"] = []

    for story in stories_data.get("stories", []):
        story_copy = dict(story)
        item_id = f"trans-{story['story_id']}"
        decision = item_decisions.get(item_id)

        if decision is not None and decision.action == ReviewItemAction.EDITED.value:
            story_copy["text_tr"] = decision.edited_text or story_copy.get("text_tr", "")
        elif decision is not None and decision.action in (
            ReviewItemAction.REJECTED.value,
            ReviewItemAction.UNCHANGED.value,
        ):
            story_copy["text_tr"] = "[ÇEVİRİ REDDEDİLDİ \u2014 yeniden çeviri gerekli] " + (
                story_copy.get("text_tr") or ""
            )
        # ACCEPTED / PENDING: keep text_tr as-is

        result["stories"].append(story_copy)

    return result


def _ensure_item_ids_correction(changes: list[dict]) -> None:
    """Mutate changes in-place to add item_id if missing (backward compat)."""
    for i, c in enumerate(changes):
        if "item_id" not in c:
            c["item_id"] = f"corr-{i:04d}"


def _ensure_item_ids_adaptation(adaptations: list[dict]) -> None:
    """Mutate adaptations in-place to add item_id if missing (backward compat)."""
    for i, a in enumerate(adaptations):
        if "item_id" not in a:
            a["item_id"] = f"adap-{i:04d}"


def _assemble_correction_review(
    original_text: str,
    diff_changes: list[dict],
    item_decisions: dict[str, ReviewItemDecision],
) -> str:
    """Reconstruct reviewed transcript from original text + per-item decisions.

    Algorithm (word-level reconstruction):
    1. Tokenize original_text into words by splitting on whitespace.
    2. Sort diff_changes by position.start_word ascending.
    3. Walk through changes + inter-change gaps in order:
       - Gap words: emit as-is.
       - accepted or pending (default): emit proposed (corrected) text
       - rejected or unchanged: emit original words
       - edited: emit edited_text
    4. Join output with single space.

    Note: Pending items (no decision recorded) default to accepting the proposed
    change. This behavior is explicit: pending == accept proposed.
    """
    from btcedu.models.review_item import ReviewItemAction

    orig_words = original_text.split()
    sorted_changes = sorted(diff_changes, key=lambda c: c["position"]["start_word"])

    output_tokens: list[str] = []
    cursor = 0  # current position in orig_words

    for change in sorted_changes:
        start_word = change["position"]["start_word"]
        end_word = change["position"]["end_word"]
        item_id = change.get("item_id", "")
        decision = item_decisions.get(item_id)
        action = decision.action if decision else ReviewItemAction.PENDING.value

        # Emit gap words between last change and this one
        if cursor < start_word:
            output_tokens.extend(orig_words[cursor:start_word])

        # Emit the change based on action
        if action in (ReviewItemAction.ACCEPTED.value, ReviewItemAction.PENDING.value):
            proposed = change.get("corrected", "")
            if proposed:
                output_tokens.extend(proposed.split())
        elif action in (ReviewItemAction.REJECTED.value, ReviewItemAction.UNCHANGED.value):
            output_tokens.extend(orig_words[start_word:end_word])
        elif action == ReviewItemAction.EDITED.value:
            edited = (
                (decision.edited_text or change.get("corrected", ""))
                if decision
                else change.get("corrected", "")
            )
            if edited:
                output_tokens.extend(edited.split())

        # For "insert" type changes, end_word == start_word (no original words consumed)
        cursor = end_word

    # Emit remaining words after last change
    if cursor < len(orig_words):
        output_tokens.extend(orig_words[cursor:])

    return " ".join(output_tokens)


def _assemble_adaptation_review(
    adapted_text: str,
    diff_adaptations: list[dict],
    item_decisions: dict[str, ReviewItemDecision],
) -> str:
    """Reconstruct reviewed adaptation from adapted text + per-item decisions.

    Algorithm (character-level, reverse-order splicing):
    1. Sort adaptations by position.start DESCENDING so splicing doesn't
       shift earlier positions.
    2. For each adaptation:
       - accepted/pending: keep adapted text unchanged (no splice)
       - rejected/unchanged: replace adapted span with original text
       - edited: replace adapted span with edited_text

    Note: Pending items default to accepting the proposed change (keeping
    the adapted text). This is explicit behavior.
    """
    from btcedu.models.review_item import ReviewItemAction

    sorted_adaptations = sorted(
        diff_adaptations,
        key=lambda a: a["position"]["start"],
        reverse=True,
    )

    result = adapted_text

    for adaptation in sorted_adaptations:
        start = adaptation["position"]["start"]
        end = adaptation["position"]["end"]
        item_id = adaptation.get("item_id", "")
        decision = item_decisions.get(item_id)
        action = decision.action if decision else ReviewItemAction.PENDING.value

        if action in (ReviewItemAction.ACCEPTED.value, ReviewItemAction.PENDING.value):
            # Keep the existing adapted text (marker tag remains)
            continue
        elif action in (ReviewItemAction.REJECTED.value, ReviewItemAction.UNCHANGED.value):
            replacement = adaptation.get("original", "")
            result = result[:start] + replacement + result[end:]
        elif action == ReviewItemAction.EDITED.value:
            edited = (
                (decision.edited_text or adaptation.get("adapted", ""))
                if decision
                else adaptation.get("adapted", "")
            )
            result = result[:start] + edited + result[end:]

    return result
