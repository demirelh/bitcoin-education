"""Pipeline orchestration: end-to-end episode processing with retry and reporting."""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.models.episode import Episode, EpisodeStatus

# Lazy import to avoid circular imports at module level:
# from btcedu.core.reviewer import has_pending_review

logger = logging.getLogger(__name__)

# Map statuses to their pipeline stage order (lower = earlier)
_STATUS_ORDER = {
    EpisodeStatus.NEW: 0,
    EpisodeStatus.DOWNLOADED: 1,
    EpisodeStatus.TRANSCRIBED: 2,
    EpisodeStatus.CHUNKED: 3,
    EpisodeStatus.GENERATED: 4,
    EpisodeStatus.REFINED: 5,
    EpisodeStatus.COMPLETED: 6,
    EpisodeStatus.FAILED: -1,
    # v2 pipeline statuses
    EpisodeStatus.CORRECTED: 10,
    EpisodeStatus.TRANSLATED: 11,
    EpisodeStatus.ADAPTED: 12,
    EpisodeStatus.CHAPTERIZED: 13,
    EpisodeStatus.IMAGES_GENERATED: 14,
    EpisodeStatus.TTS_DONE: 15,
    EpisodeStatus.RENDERED: 16,
    EpisodeStatus.APPROVED: 17,
    EpisodeStatus.PUBLISHED: 18,
    EpisodeStatus.COST_LIMIT: -2,
}

# v1 stages in execution order, with the status required to enter each stage
_V1_STAGES = [
    ("download", EpisodeStatus.NEW),
    ("transcribe", EpisodeStatus.DOWNLOADED),
    ("chunk", EpisodeStatus.TRANSCRIBED),
    ("generate", EpisodeStatus.CHUNKED),
    ("refine", EpisodeStatus.GENERATED),
]

# v2 stages (extends after TRANSCRIBED with CORRECT instead of CHUNK)
_V2_STAGES = [
    ("download", EpisodeStatus.NEW),
    ("transcribe", EpisodeStatus.DOWNLOADED),
    ("correct", EpisodeStatus.TRANSCRIBED),
    ("review_gate_1", EpisodeStatus.CORRECTED),
    ("translate", EpisodeStatus.CORRECTED),  # after review approved
    ("adapt", EpisodeStatus.TRANSLATED),
    ("review_gate_2", EpisodeStatus.ADAPTED),
    ("chapterize", EpisodeStatus.ADAPTED),  # after review approved
    ("imagegen", EpisodeStatus.CHAPTERIZED),  # Sprint 7
    ("tts", EpisodeStatus.IMAGES_GENERATED),  # Sprint 8
    ("render", EpisodeStatus.TTS_DONE),  # Sprint 9
]

# Keep _STAGES as alias for backward compat
_STAGES = _V1_STAGES


def _get_stages(settings: Settings) -> list[tuple[str, EpisodeStatus]]:
    """Return the appropriate stages list based on pipeline version."""
    if settings.pipeline_version >= 2:
        return _V2_STAGES
    return _V1_STAGES


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass
class StagePlan:
    """One stage decision in a pipeline plan (produced before execution)."""

    stage: str
    decision: str  # "run", "skip", "pending"
    reason: str


@dataclass
class StageResult:
    stage: str
    status: str  # "success", "skipped", "failed"
    duration_seconds: float
    detail: str = ""
    error: str | None = None


@dataclass
class PipelineReport:
    episode_id: str
    title: str
    started_at: datetime = field(default_factory=_utcnow)
    completed_at: datetime | None = None
    stages: list[StageResult] = field(default_factory=list)
    total_cost_usd: float = 0.0
    success: bool = False
    error: str | None = None


def resolve_pipeline_plan(
    session: Session,
    episode: Episode,
    force: bool = False,
    settings: Settings | None = None,
) -> list[StagePlan]:
    """Determine what each stage would do without executing anything.

    Returns a list of StagePlan entries — one per stage — showing whether
    each stage would run, be skipped, or is pending (will run if prior
    stages succeed).

    Args:
        settings: If provided, selects v1 or v2 stages based on pipeline_version.
            If None, uses v1 stages for backward compatibility.
    """
    session.refresh(episode)
    current_order = _STATUS_ORDER.get(episode.status, -1)
    plan: list[StagePlan] = []
    will_advance = False

    stages = _get_stages(settings) if settings else _V1_STAGES
    for stage_name, required_status in stages:
        required_order = _STATUS_ORDER[required_status]

        if current_order > required_order and not force:
            plan.append(StagePlan(stage_name, "skip", "already completed"))
        elif current_order == required_order or force:
            plan.append(
                StagePlan(
                    stage_name,
                    "run",
                    "forced"
                    if force and current_order > required_order
                    else f"status={episode.status.value}",
                )
            )
            will_advance = True
        elif will_advance:
            plan.append(StagePlan(stage_name, "pending", "after prior stages"))
        else:
            plan.append(StagePlan(stage_name, "skip", "not ready"))

    return plan


def _run_stage(
    session: Session,
    episode: Episode,
    settings: Settings,
    stage_name: str,
    force: bool = False,
) -> StageResult:
    """Run a single pipeline stage. Returns StageResult."""
    t0 = time.monotonic()

    try:
        if stage_name == "download":
            from btcedu.core.detector import download_episode

            path = download_episode(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0
            return StageResult("download", "success", elapsed, detail=path)

        elif stage_name == "transcribe":
            from btcedu.core.transcriber import transcribe_episode

            path = transcribe_episode(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0
            return StageResult("transcribe", "success", elapsed, detail=path)

        elif stage_name == "chunk":
            from btcedu.core.transcriber import chunk_episode

            count = chunk_episode(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0
            return StageResult("chunk", "success", elapsed, detail=f"{count} chunks")

        elif stage_name == "generate":
            from btcedu.core.generator import generate_content

            result = generate_content(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0
            return StageResult(
                "generate",
                "success",
                elapsed,
                detail=f"{len(result.artifacts)} artifacts (${result.total_cost_usd:.4f})",
            )

        elif stage_name == "refine":
            from btcedu.core.generator import refine_content

            result = refine_content(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0
            return StageResult(
                "refine",
                "success",
                elapsed,
                detail=f"{len(result.artifacts)} artifacts (${result.total_cost_usd:.4f})",
            )

        elif stage_name == "correct":
            from btcedu.core.corrector import correct_transcript

            result = correct_transcript(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0
            return StageResult(
                "correct",
                "success",
                elapsed,
                detail=f"{result.change_count} corrections (${result.cost_usd:.4f})",
            )

        elif stage_name == "review_gate_1":
            from btcedu.core.reviewer import (
                create_review_task,
                has_approved_review,
                has_pending_review,
            )

            # Check if already approved
            if has_approved_review(session, episode.episode_id, "correct"):
                elapsed = time.monotonic() - t0
                return StageResult("review_gate_1", "success", elapsed, detail="review approved")

            # Check if a pending review already exists
            if has_pending_review(session, episode.episode_id):
                elapsed = time.monotonic() - t0
                return StageResult(
                    "review_gate_1",
                    "review_pending",
                    elapsed,
                    detail="awaiting review",
                )

            # Create a new review task
            corrected_path = (
                Path(settings.transcripts_dir) / episode.episode_id / "transcript.corrected.de.txt"
            )
            diff_path = (
                Path(settings.outputs_dir) / episode.episode_id / "review" / "correction_diff.json"
            )

            create_review_task(
                session,
                episode.episode_id,
                stage="correct",
                artifact_paths=[str(corrected_path)],
                diff_path=str(diff_path) if diff_path.exists() else None,
            )
            elapsed = time.monotonic() - t0
            return StageResult(
                "review_gate_1",
                "review_pending",
                elapsed,
                detail="review task created",
            )

        elif stage_name == "translate":
            from btcedu.core.translator import translate_transcript

            result = translate_transcript(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0

            if result.skipped:
                return StageResult("translate", "skipped", elapsed, detail="already up-to-date")
            else:
                return StageResult(
                    "translate",
                    "success",
                    elapsed,
                    detail=f"{result.output_char_count} chars Turkish (${result.cost_usd:.4f})",
                )

        elif stage_name == "adapt":
            from btcedu.core.adapter import adapt_script

            result = adapt_script(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0

            if result.skipped:
                return StageResult("adapt", "skipped", elapsed, detail="already up-to-date")
            else:
                return StageResult(
                    "adapt",
                    "success",
                    elapsed,
                    detail=(
                        f"{result.adaptation_count} adaptations "
                        f"(T1:{result.tier1_count}, T2:{result.tier2_count}, "
                        f"${result.cost_usd:.4f})"
                    ),
                )

        elif stage_name == "review_gate_2":
            from btcedu.core.reviewer import (
                create_review_task,
                has_approved_review,
                has_pending_review,
            )

            # Check if already approved
            if has_approved_review(session, episode.episode_id, "adapt"):
                elapsed = time.monotonic() - t0
                return StageResult(
                    "review_gate_2",
                    "success",
                    elapsed,
                    detail="adaptation review approved",
                )

            # Check if a pending review already exists
            if has_pending_review(session, episode.episode_id):
                elapsed = time.monotonic() - t0
                return StageResult(
                    "review_gate_2",
                    "review_pending",
                    elapsed,
                    detail="awaiting adaptation review",
                )

            # Create a new review task
            adapted_path = Path(settings.outputs_dir) / episode.episode_id / "script.adapted.tr.md"
            diff_path = (
                Path(settings.outputs_dir) / episode.episode_id / "review" / "adaptation_diff.json"
            )

            create_review_task(
                session,
                episode.episode_id,
                stage="adapt",
                artifact_paths=[str(adapted_path)],
                diff_path=str(diff_path) if diff_path.exists() else None,
            )
            elapsed = time.monotonic() - t0
            return StageResult(
                "review_gate_2",
                "review_pending",
                elapsed,
                detail="adaptation review task created",
            )

        elif stage_name == "chapterize":
            from btcedu.core.chapterizer import chapterize_script

            result = chapterize_script(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0

            if result.skipped:
                return StageResult("chapterize", "skipped", elapsed, detail="already up-to-date")
            else:
                return StageResult(
                    "chapterize",
                    "success",
                    elapsed,
                    detail=(
                        f"{result.chapter_count} chapters, "
                        f"~{result.estimated_duration_seconds}s, "
                        f"${result.cost_usd:.4f}"
                    ),
                )

        elif stage_name == "imagegen":
            from btcedu.core.image_generator import generate_images

            result = generate_images(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0

            if result.skipped:
                return StageResult("imagegen", "skipped", elapsed, detail="already up-to-date")
            else:
                return StageResult(
                    "imagegen",
                    "success",
                    elapsed,
                    detail=(
                        f"{result.generated_count}/{result.image_count} images generated "
                        f"({result.template_count} placeholders, "
                        f"{result.failed_count} failed), "
                        f"${result.cost_usd:.4f}"
                    ),
                )

        elif stage_name == "tts":
            from btcedu.core.tts import generate_tts

            result = generate_tts(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0

            if result.skipped:
                return StageResult("tts", "skipped", elapsed, detail="already up-to-date")
            else:
                return StageResult(
                    "tts",
                    "success",
                    elapsed,
                    detail=(
                        f"{result.segment_count} segments, "
                        f"{result.total_duration_seconds:.1f}s total, "
                        f"${result.cost_usd:.4f}"
                    ),
                )

        elif stage_name == "render":
            from btcedu.core.renderer import render_video

            result = render_video(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0

            if result.skipped:
                return StageResult("render", "skipped", elapsed, detail="already up-to-date")
            else:
                return StageResult(
                    "render",
                    "success",
                    elapsed,
                    detail=(
                        f"{result.segment_count} segments, "
                        f"{result.total_duration_seconds:.1f}s, "
                        f"{result.total_size_bytes / 1024 / 1024:.1f}MB"
                    ),
                )

        else:
            raise ValueError(f"Unknown stage: {stage_name}")

    except Exception as e:
        elapsed = time.monotonic() - t0
        return StageResult(stage_name, "failed", elapsed, error=str(e))


def run_episode_pipeline(
    session: Session,
    episode: Episode,
    settings: Settings,
    force: bool = False,
    stage_callback: Callable[[str], None] | None = None,
) -> PipelineReport:
    """Run the full pipeline for a single episode.

    Chains: download -> transcribe -> chunk -> generate -> refine.
    Each stage is skipped if the episode has already passed it.
    On failure: records error, increments retry_count, stops processing.

    Args:
        stage_callback: Optional callback invoked with the stage name
            before each stage executes. Useful for updating progress in UIs.

    Returns:
        PipelineReport with per-stage results.
    """
    report = PipelineReport(
        episode_id=episode.episode_id,
        title=episode.title,
    )

    # Log pipeline plan before execution
    plan = resolve_pipeline_plan(session, episode, force, settings=settings)
    plan_lines = [f"  {p.stage}: {p.decision} ({p.reason})" for p in plan]
    logger.info(
        "Pipeline plan for %s (status: %s):\n%s",
        episode.episode_id,
        episode.status.value,
        "\n".join(plan_lines),
    )

    logger.info("Pipeline start: %s (%s)", episode.episode_id, episode.title)

    stages = _get_stages(settings)
    for stage_name, required_status in stages:
        # Refresh episode status from DB
        session.refresh(episode)

        current_order = _STATUS_ORDER.get(episode.status, -1)
        required_order = _STATUS_ORDER[required_status]

        # Skip if episode is already past this stage
        if current_order > required_order:
            report.stages.append(
                StageResult(stage_name, "skipped", 0.0, detail="already completed")
            )
            continue

        # Skip if episode status doesn't match this stage's requirement
        if current_order < required_order and not force:
            report.stages.append(StageResult(stage_name, "skipped", 0.0, detail="not ready"))
            continue

        logger.info("  Stage: %s", stage_name)
        if stage_callback:
            stage_callback(stage_name)
        result = _run_stage(session, episode, settings, stage_name, force=force)
        report.stages.append(result)

        if result.status == "failed":
            logger.error("  Stage %s failed: %s", stage_name, result.error)
            report.error = f"Stage '{stage_name}' failed: {result.error}"

            # Record failure on the episode
            session.refresh(episode)
            episode.error_message = report.error
            episode.retry_count += 1
            session.commit()
            break
        elif result.status == "review_pending":
            logger.info("  Stage %s: %s", stage_name, result.detail)
            break  # Stop pipeline gracefully, no error
        else:
            logger.info("  Stage %s: %s", stage_name, result.detail)

    # Check final outcome
    session.refresh(episode)
    if report.error is None:
        report.success = True
        # Clear any previous error
        if episode.error_message:
            episode.error_message = None
            session.commit()

    report.completed_at = _utcnow()

    # Calculate total cost from stage results that report costs
    for sr in report.stages:
        success_stages = (
            "generate",
            "refine",
            "correct",
            "translate",
            "adapt",
            "chapterize",
            "imagegen",
            "tts",
        )
        if sr.stage in success_stages and sr.status == "success" and "$" in sr.detail:
            try:
                cost_str = sr.detail.split("$")[1].rstrip(")")
                report.total_cost_usd += float(cost_str)
            except (IndexError, ValueError):
                pass

    logger.info(
        "Pipeline %s: %s (cost=$%.4f)",
        "OK" if report.success else "FAILED",
        episode.episode_id,
        report.total_cost_usd,
    )

    return report


def run_pending(
    session: Session,
    settings: Settings,
    max_episodes: int | None = None,
    since: datetime | None = None,
) -> list[PipelineReport]:
    """Process all pending episodes through the pipeline.

    Queries episodes with status in (NEW, DOWNLOADED, TRANSCRIBED, CHUNKED,
    GENERATED, CORRECTED, TRANSLATED, ADAPTED, CHAPTERIZED, IMAGES_GENERATED,
    TTS_DONE), ordered by published_at ASC (oldest first).

    Args:
        session: DB session.
        settings: Application settings.
        max_episodes: Limit number of episodes to process.
        since: Only process episodes published after this date.

    Returns:
        List of PipelineReports.
    """
    query = (
        session.query(Episode)
        .filter(
            Episode.status.in_(
                [
                    EpisodeStatus.NEW,
                    EpisodeStatus.DOWNLOADED,
                    EpisodeStatus.TRANSCRIBED,
                    EpisodeStatus.CHUNKED,
                    EpisodeStatus.GENERATED,
                    # v2 pipeline statuses
                    EpisodeStatus.CORRECTED,
                    EpisodeStatus.TRANSLATED,
                    EpisodeStatus.ADAPTED,
                    EpisodeStatus.CHAPTERIZED,
                    EpisodeStatus.IMAGES_GENERATED,
                    EpisodeStatus.TTS_DONE,  # Sprint 9: render stage
                ]
            )
        )
        .order_by(Episode.published_at.asc())
    )

    if since is not None:
        query = query.filter(Episode.published_at >= since)

    if max_episodes is not None:
        query = query.limit(max_episodes)

    episodes = query.all()

    # Filter out episodes with active review tasks to avoid wasteful re-processing
    if episodes:
        from btcedu.core.reviewer import has_pending_review

        episodes = [ep for ep in episodes if not has_pending_review(session, ep.episode_id)]

    if not episodes:
        logger.info("No pending episodes to process.")
        return []

    logger.info("Processing %d pending episode(s)...", len(episodes))

    reports = []
    for ep in episodes:
        report = run_episode_pipeline(session, ep, settings)
        reports.append(report)

    return reports


def run_latest(
    session: Session,
    settings: Settings,
) -> PipelineReport | None:
    """Detect new episodes and process the newest pending one.

    Calls detect_episodes first, then finds the newest episode
    with status < GENERATED and runs the pipeline.

    Returns:
        PipelineReport for the processed episode, or None if nothing to do.
    """
    from btcedu.core.detector import detect_episodes

    detect_result = detect_episodes(session, settings)
    logger.info(
        "Detection: found=%d, new=%d, total=%d",
        detect_result.found,
        detect_result.new,
        detect_result.total,
    )

    # Find newest pending episode
    candidates = (
        session.query(Episode)
        .filter(
            Episode.status.in_(
                [
                    EpisodeStatus.NEW,
                    EpisodeStatus.DOWNLOADED,
                    EpisodeStatus.TRANSCRIBED,
                    EpisodeStatus.CHUNKED,
                    EpisodeStatus.GENERATED,
                    # v2 pipeline statuses
                    EpisodeStatus.CORRECTED,
                    EpisodeStatus.TRANSLATED,
                    EpisodeStatus.ADAPTED,
                    EpisodeStatus.CHAPTERIZED,
                    EpisodeStatus.IMAGES_GENERATED,
                ]
            )
        )
        .order_by(Episode.published_at.desc())
        .all()
    )

    # Filter out episodes with active review tasks
    from btcedu.core.reviewer import has_pending_review

    episode = None
    for candidate in candidates:
        if not has_pending_review(session, candidate.episode_id):
            episode = candidate
            break

    if episode is None:
        logger.info("No pending episodes after detection.")
        return None

    return run_episode_pipeline(session, episode, settings)


def retry_episode(
    session: Session,
    episode_id: str,
    settings: Settings,
    stage_callback: Callable[[str], None] | None = None,
) -> PipelineReport:
    """Retry a failed episode from its last successful stage.

    Finds the episode, validates it has a failure state, clears the error,
    and re-runs the pipeline from the current status.

    Args:
        stage_callback: Optional callback invoked with the stage name
            before each stage executes. Useful for updating progress in UIs.

    Returns:
        PipelineReport for the retry.

    Raises:
        ValueError: If episode not found or not in a failed state.
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    if not episode.error_message and episode.status != EpisodeStatus.FAILED:
        raise ValueError(
            f"Episode {episode_id} is not in a failed state "
            f"(status='{episode.status.value}', no error_message). "
            "Use 'run' instead."
        )

    logger.info(
        "Retrying %s from status '%s' (attempt %d)",
        episode_id,
        episode.status.value,
        episode.retry_count + 1,
    )

    # Clear error to allow pipeline to proceed
    episode.error_message = None
    session.commit()

    return run_episode_pipeline(session, episode, settings, stage_callback=stage_callback)


def write_report(report: PipelineReport, reports_dir: str) -> str:
    """Write a PipelineReport as JSON to reports_dir/{episode_id}/.

    Returns:
        Path to the written report file.
    """
    report_dir = Path(reports_dir) / report.episode_id
    report_dir.mkdir(parents=True, exist_ok=True)

    timestamp = report.started_at.strftime("%Y%m%d_%H%M%S")
    path = report_dir / f"report_{timestamp}.json"

    data = {
        "episode_id": report.episode_id,
        "title": report.title,
        "started_at": report.started_at.isoformat(),
        "completed_at": report.completed_at.isoformat() if report.completed_at else None,
        "success": report.success,
        "error": report.error,
        "total_cost_usd": report.total_cost_usd,
        "stages": [
            {
                "stage": sr.stage,
                "status": sr.status,
                "duration_seconds": sr.duration_seconds,
                "detail": sr.detail,
                "error": sr.error,
            }
            for sr in report.stages
        ],
    }

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Report written: %s", path)

    return str(path)
