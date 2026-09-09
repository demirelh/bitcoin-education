"""Pipeline orchestration: end-to-end episode processing with retry and reporting."""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.models.episode import (
    Episode,
    EpisodeStatus,
    PipelineRun,
    PipelineStage,
    RunStatus,
)

# Lazy import to avoid circular imports at module level:
# from btcedu.core.reviewer import has_pending_review

logger = logging.getLogger(__name__)

# Map statuses to their pipeline stage order (lower = earlier)
_STATUS_ORDER = {
    EpisodeStatus.NEW: 0,
    EpisodeStatus.DOWNLOADED: 1,
    EpisodeStatus.TRANSCRIBED: 2,
    EpisodeStatus.FAILED: -1,
    # v2 pipeline statuses
    EpisodeStatus.CORRECTED: 10,
    EpisodeStatus.SEGMENTED: 10.5,  # Between CORRECTED and TRANSLATED (news profiles)
    EpisodeStatus.TRANSLATED: 11,
    EpisodeStatus.ADAPTED: 12,
    EpisodeStatus.SCRIPTED: 12.5,  # Between ADAPTED and CHAPTERIZED (broadcast profiles)
    EpisodeStatus.CHAPTERIZED: 13,
    EpisodeStatus.FRAMES_EXTRACTED: 13.5,
    EpisodeStatus.IMAGES_GENERATED: 14,
    EpisodeStatus.TTS_DONE: 15,
    EpisodeStatus.SCENE_PLANNED: 15.3,
    EpisodeStatus.ANCHOR_GENERATED: 15.5,
    EpisodeStatus.RENDERED: 16,
    EpisodeStatus.APPROVED: 17,
    EpisodeStatus.PUBLISHED: 18,
    EpisodeStatus.COST_LIMIT: -2,
}

# v2 pipeline stages in execution order, with the status required to enter each.
_V2_STAGES = [
    ("download", EpisodeStatus.NEW),
    ("transcribe", EpisodeStatus.DOWNLOADED),
    ("transcript_analyze", EpisodeStatus.TRANSCRIBED),
    ("transcript_verify", EpisodeStatus.TRANSCRIBED),
    ("correct", EpisodeStatus.TRANSCRIBED),
    ("transcript_qa", EpisodeStatus.CORRECTED),
    ("review_gate_transcript_qa", EpisodeStatus.CORRECTED),
    ("review_gate_1", EpisodeStatus.CORRECTED),
    ("translate", EpisodeStatus.CORRECTED),  # after review approved
    ("adapt", EpisodeStatus.TRANSLATED),
    ("review_gate_2", EpisodeStatus.ADAPTED),
    ("chapterize", EpisodeStatus.ADAPTED),  # after review approved
    ("frameextract", EpisodeStatus.CHAPTERIZED),  # extract keyframes from source video
    ("imagegen", EpisodeStatus.FRAMES_EXTRACTED),  # search + rank (no finalize)
    ("review_gate_stock", EpisodeStatus.FRAMES_EXTRACTED),  # human pins + approve
    ("tts", EpisodeStatus.IMAGES_GENERATED),  # Sprint 8
    ("sceneplan", EpisodeStatus.TTS_DONE),  # deterministic shot list, no provider calls
    ("anchorgen", EpisodeStatus.SCENE_PLANNED),  # D-ID anchor (no-op if disabled)
    # A person looks at the presenter clips before they are composited into
    # anything. Inactive unless the profile asks for it, so every other profile
    # walks straight through.
    ("review_gate_anchor", EpisodeStatus.ANCHOR_GENERATED),
    ("render", EpisodeStatus.ANCHOR_GENERATED),  # Sprint 9
    ("review_gate_3", EpisodeStatus.RENDERED),  # Sprint 10
    ("publish", EpisodeStatus.APPROVED),  # Sprint 11
]

_STAGES = _V2_STAGES


def _get_stages(
    settings: Settings,
    episode: Episode | None = None,
) -> list[tuple[str, EpisodeStatus]]:
    """Return the profile-aware v2 stages list.

    Applies profile-aware modifications:
    - Inserts 'segment' stage for news profiles with segment.enabled=True
    - Removes 'adapt' and 'review_gate_2' for profiles with adapt.skip=True
    - Adjusts required statuses accordingly
    """
    stages = list(_V2_STAGES)

    if episode is None:
        return stages

    # Preserve the repository's current v1 behavior exactly: the newly added
    # v2-only side stage must not become the first failure for legacy episodes.
    if episode.pipeline_version != 2:
        return [
            (name, status)
            for name, status in stages
            if name
            not in {
                "transcript_analyze",
                "transcript_verify",
                "transcript_qa",
                "review_gate_transcript_qa",
            }
        ]

    # Load profile for profile-aware stage modifications
    try:
        from btcedu.profiles import get_registry

        profile_registry = get_registry(settings)
        content_profile = getattr(episode, "content_profile", "bitcoin_podcast")
        profile = profile_registry.get(content_profile)
        stage_config = profile.stage_config
    except Exception:
        # If profile lookup fails, return default v2 stages
        return stages

    analysis_enabled = stage_config.get("transcript_analyze", {}).get("enabled", True)
    if not analysis_enabled:
        stages = [(name, status) for name, status in stages if name != "transcript_analyze"]
    verify_config = stage_config.get("transcript_verify", {}) or {}
    secondary_config = (stage_config.get("transcription", {}) or {}).get("secondary", {}) or {}
    if (
        verify_config.get("enabled") is False
        or secondary_config.get("enabled") is False
        or secondary_config.get("mode") == "disabled"
        or (not analysis_enabled and secondary_config.get("mode") == "suspicious_segments_only")
    ):
        stages = [(name, status) for name, status in stages if name != "transcript_verify"]
    if not stage_config.get("transcript_qa", {}).get("enabled", True):
        stages = [
            (name, status)
            for name, status in stages
            if name not in {"transcript_qa", "review_gate_transcript_qa"}
        ]

    # Insert 'segment' stage for news profiles
    if stage_config.get("segment", {}).get("enabled"):
        # Find position of 'translate' stage and insert segment before it
        translate_idx = next((i for i, (name, _) in enumerate(stages) if name == "translate"), None)
        if translate_idx is not None:
            stages.insert(translate_idx, ("segment", EpisodeStatus.CORRECTED))
            # Adjust translate to require SEGMENTED instead of CORRECTED
            stages[translate_idx + 1] = ("translate", EpisodeStatus.SEGMENTED)

    # Skip 'adapt' for profiles with adapt.skip=True; replace review_gate_2 with
    # review_gate_translate so news translations get a dedicated human review gate.
    adapt_config = stage_config.get("adapt", {})
    if adapt_config.get("skip") or adapt_config.get("mode") == "disabled":
        # Replace review_gate_2 with review_gate_translate (don't remove the gate)
        stages = [
            ("review_gate_translate", EpisodeStatus.TRANSLATED) if n == "review_gate_2" else (n, s)
            for n, s in stages
        ]
        # Remove adapt only (keep the renamed gate)
        stages = [(n, s) for n, s in stages if n != "adapt"]
        # Adjust chapterize to accept TRANSLATED instead of ADAPTED
        stages = [
            ("chapterize", EpisodeStatus.TRANSLATED) if n == "chapterize" else (n, s)
            for n, s in stages
        ]

    # Insert the 'script' stage for profiles that broadcast a two-presenter
    # programme. It runs after the translation review gate and rewrites the
    # approved translation into the spoken script that chapterize then follows.
    if stage_config.get("script", {}).get("enabled"):
        chapterize_idx = next(
            (i for i, (name, _) in enumerate(stages) if name == "chapterize"), None
        )
        if chapterize_idx is not None:
            required = stages[chapterize_idx][1]
            stages.insert(chapterize_idx, ("script", required))
            stages[chapterize_idx + 1] = ("chapterize", EpisodeStatus.SCRIPTED)

    return stages


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _profile_pipeline_flags(settings: Settings, episode: Episode) -> tuple[bool, bool]:
    """Return (auto_approve_reviews, auto_publish) for the episode's profile.

    Defaults to (False, True) — i.e. keep human review gates and allow
    publishing — when the profile cannot be resolved.
    """
    try:
        from btcedu.profiles import get_registry

        profile = get_registry(settings).get(getattr(episode, "content_profile", "bitcoin_podcast"))
        return bool(profile.auto_approve_reviews), bool(profile.auto_publish)
    except Exception:
        return False, False


def _quality_gate_scoped(settings: Settings, episode: Episode) -> bool:
    """True when the factual translation quality gate governs this episode.

    Scoped to profiles that opt in via ``stage_config.qa`` or to story-based
    (news) episodes. Legacy episodes without either keep their existing advisory
    QA + human review flow, preserving backward compatibility.
    """
    try:
        from btcedu.profiles import get_registry

        name = getattr(episode, "content_profile", None)
        if name:
            profile = get_registry(settings).get(name)
            qa_config = profile.stage_config.get("qa")
            if isinstance(qa_config, dict):
                return qa_config.get(
                    "enabled",
                    bool(getattr(settings, "qa_review_enabled", False)),
                )
    except Exception:  # noqa: BLE001
        pass
    stories = Path(settings.outputs_dir) / episode.episode_id / "stories_translated.json"
    return stories.exists() and bool(getattr(settings, "qa_review_enabled", False))


def _imagegen_provider(settings: Settings, episode: Episode) -> str:
    """Return the configured imagegen provider for the episode's profile.

    Empty string when the profile or config cannot be resolved (callers then
    fall back to the default stock-image path).
    """
    try:
        from btcedu.profiles import get_registry

        profile = get_registry(settings).get(getattr(episode, "content_profile", "bitcoin_podcast"))
        imagegen_cfg = profile.stage_config.get("imagegen", {}) or {}
        return str(imagegen_cfg.get("provider", "") or "").lower()
    except Exception:
        return ""


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

    stages = _get_stages(settings, episode) if settings else _STAGES
    for stage_name, required_status in stages:
        required_order = _STATUS_ORDER[required_status]

        if current_order > required_order:
            # Deliberately not overridden by ``force``: the executor never
            # rewinds an episode either, and a plan that promised otherwise
            # was read as "everything will be regenerated" while the run in
            # fact resumed at the current status and rebuilt later stages on
            # top of stale earlier ones.
            plan.append(StagePlan(stage_name, "skip", "already completed"))
        elif current_order == required_order or force:
            plan.append(
                StagePlan(
                    stage_name,
                    "run",
                    "forced (ahead of status)"
                    if force and current_order < required_order
                    else f"status={episode.status.value}",
                )
            )
            will_advance = True
        elif will_advance:
            plan.append(StagePlan(stage_name, "pending", "after prior stages"))
        else:
            plan.append(StagePlan(stage_name, "skip", "not ready"))

    return plan


# Stages that own a genuine PipelineStage timing record. Review gates are
# excluded (near-instant, no timing value).
_STAGE_NAME_TO_PIPELINE_STAGE = {
    "download": PipelineStage.DOWNLOAD,
    "transcribe": PipelineStage.TRANSCRIBE,
    "transcript_analyze": PipelineStage.TRANSCRIPT_ANALYZE,
    "transcript_verify": PipelineStage.TRANSCRIPT_VERIFY,
    "correct": PipelineStage.CORRECT,
    "transcript_qa": PipelineStage.TRANSCRIPT_QA,
    "segment": PipelineStage.SEGMENT,
    "translate": PipelineStage.TRANSLATE,
    "adapt": PipelineStage.ADAPT,
    "script": PipelineStage.SCRIPT,
    "chapterize": PipelineStage.CHAPTERIZE,
    "frameextract": PipelineStage.FRAMEEXTRACT,
    "imagegen": PipelineStage.IMAGEGEN,
    "tts": PipelineStage.TTS,
    "sceneplan": PipelineStage.SCENEPLAN,
    "anchorgen": PipelineStage.ANCHORGEN,
    "render": PipelineStage.RENDER,
    "publish": PipelineStage.PUBLISH,
}

_RESUMABLE_EPISODE_STATUSES = (
    EpisodeStatus.NEW,
    EpisodeStatus.DOWNLOADED,
    EpisodeStatus.TRANSCRIBED,
    EpisodeStatus.CORRECTED,
    EpisodeStatus.SEGMENTED,
    EpisodeStatus.TRANSLATED,
    EpisodeStatus.ADAPTED,
    EpisodeStatus.SCRIPTED,
    EpisodeStatus.CHAPTERIZED,
    EpisodeStatus.FRAMES_EXTRACTED,
    EpisodeStatus.IMAGES_GENERATED,
    EpisodeStatus.TTS_DONE,
    EpisodeStatus.SCENE_PLANNED,
    EpisodeStatus.ANCHOR_GENERATED,
    EpisodeStatus.RENDERED,
    EpisodeStatus.APPROVED,
)


def _mark_orphaned_pipeline_runs_interrupted(session: Session) -> int:
    """Close RUNNING rows left behind by a dead process before resuming.

    Callers hold the exclusive pipeline lock, so no live pipeline can own a
    RUNNING row at this point. The episode status remains unchanged; the next
    idempotent stage resumes from that durable status.
    """
    orphaned = (
        session.query(PipelineRun)
        .filter(PipelineRun.status == RunStatus.RUNNING)
        .all()
    )
    if not orphaned:
        return 0

    now = _utcnow()
    for run in orphaned:
        run.status = RunStatus.FAILED
        run.completed_at = now
        run.error_message = (
            "Pipeline process ended before this stage completed; "
            "the next scheduled run will resume it."
        )
    session.commit()
    logger.warning("Marked %d orphaned pipeline run(s) as interrupted.", len(orphaned))
    return len(orphaned)


def _ensure_stage_pipeline_run(
    session: Session,
    episode: Episode,
    stage_name: str,
    duration_seconds: float,
    since: datetime,
    tracking_run: PipelineRun | None = None,
) -> None:
    """Record a PipelineRun for stages that don't create one themselves.

    Some stages (download, transcribe) never write a PipelineRun, and others
    can return early on skip/idempotency paths before writing one. Without a
    successful PipelineRun record, the dashboard cannot show a stage duration,
    which is why some completed stages appeared with no time. This fills the
    gap using the elapsed time already measured by ``_run_stage`` — but only
    when the stage did not already record its own run during this invocation.

    Args:
        since: Timestamp captured just before the stage started. If a SUCCESS
            run for this stage completed at/after this moment, the stage
            already recorded its own timing and no gap-fill row is added.
    """
    ps = _STAGE_NAME_TO_PIPELINE_STAGE.get(stage_name)
    if ps is None:
        return

    # If the stage recorded its own SUCCESS run during this invocation, don't
    # duplicate it.
    existing = (
        session.query(PipelineRun)
        .filter(
            PipelineRun.episode_id == episode.id,
            PipelineRun.stage == ps,
            PipelineRun.status == RunStatus.SUCCESS,
            PipelineRun.completed_at >= since,
            PipelineRun.id != tracking_run.id if tracking_run is not None else True,
        )
        .first()
    )
    if existing is not None:
        if tracking_run is not None:
            session.delete(tracking_run)
            session.commit()
        return

    now = _utcnow()
    if tracking_run is not None:
        tracking_run.status = RunStatus.SUCCESS
        tracking_run.started_at = now - timedelta(seconds=max(duration_seconds, 0.0))
        tracking_run.completed_at = now
        tracking_run.error_message = None
        session.commit()
        return

    started = now - timedelta(seconds=max(duration_seconds, 0.0))
    from btcedu.version import get_git_commit

    session.add(
        PipelineRun(
            episode_id=episode.id,
            stage=ps,
            status=RunStatus.SUCCESS,
            started_at=started,
            completed_at=now,
            git_commit=get_git_commit(),
        )
    )
    session.commit()


def _start_stage_pipeline_run(
    session: Session,
    episode: Episode,
    stage_name: str,
    started_at: datetime,
) -> PipelineRun | None:
    """Persist an active stage immediately for progress display and reboot recovery."""
    ps = _STAGE_NAME_TO_PIPELINE_STAGE.get(stage_name)
    if ps is None:
        return None

    from btcedu.version import get_git_commit

    run = PipelineRun(
        episode_id=episode.id,
        stage=ps,
        status=RunStatus.RUNNING,
        started_at=started_at,
        git_commit=get_git_commit(),
    )
    session.add(run)
    session.commit()
    return run


def _finish_stage_pipeline_run(
    session: Session,
    tracking_run: PipelineRun | None,
    result: StageResult,
) -> None:
    """Close or discard the active tracking row when a stage does not succeed."""
    if tracking_run is None:
        return
    if result.status == "failed":
        tracking_run.status = RunStatus.FAILED
        tracking_run.completed_at = _utcnow()
        tracking_run.error_message = result.error or result.detail or "unknown error"
        session.commit()
        return

    session.delete(tracking_run)
    session.commit()


def _run_stage(
    session: Session,
    episode: Episode,
    settings: Settings,
    stage_name: str,
    force: bool = False,
) -> StageResult:
    """Run a single pipeline stage. Returns StageResult."""
    t0 = time.monotonic()

    _V2_ONLY_STAGES = {
        "correct",
        "transcript_analyze",
        "transcript_verify",
        "transcript_qa",
        "review_gate_transcript_qa",
        "review_gate_1",
        "segment",
        "translate",
        "adapt",
        "review_gate_2",
        "review_gate_translate",
        "script",
        "chapterize",
        "frameextract",
        "imagegen",
        "review_gate_stock",
        "tts",
        "sceneplan",
        "anchorgen",
        "review_gate_anchor",
        "render",
        "review_gate_3",
        "publish",
    }
    if stage_name in _V2_ONLY_STAGES and episode.pipeline_version != 2:
        raise ValueError(
            f"Stage '{stage_name}' requires v2 pipeline but episode "
            f"{episode.episode_id} has pipeline_version={episode.pipeline_version}. "
            f"Set pipeline_version=2 to proceed."
        )

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

        elif stage_name == "transcript_analyze":
            from btcedu.core.transcript_analyzer import analyze_transcript

            result = analyze_transcript(
                session,
                episode.episode_id,
                settings,
                force=force,
            )
            elapsed = time.monotonic() - t0
            if result.skipped:
                return StageResult(
                    "transcript_analyze",
                    "skipped",
                    elapsed,
                    detail=result.reason,
                )
            return StageResult(
                "transcript_analyze",
                "success",
                elapsed,
                detail=(
                    f"{result.suspicious_count}/{result.segment_count} suspicious "
                    f"({result.critical_count} critical)"
                ),
            )

        elif stage_name == "transcript_verify":
            from btcedu.core.transcript_verifier import verify_transcript

            result = verify_transcript(
                session,
                episode.episode_id,
                settings,
                force=force,
            )
            elapsed = time.monotonic() - t0
            if result.skipped:
                return StageResult(
                    "transcript_verify",
                    "skipped",
                    elapsed,
                    detail=result.reason,
                )
            return StageResult(
                "transcript_verify",
                "success",
                elapsed,
                detail=(
                    f"{result.regions_checked} regions "
                    f"({result.critical_count} critical, ${result.cost_usd:.4f})"
                ),
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

        elif stage_name == "transcript_qa":
            from btcedu.core.transcript_qa import evaluate_transcript_qa

            result = evaluate_transcript_qa(
                session,
                episode.episode_id,
                settings,
                force=force,
            )
            elapsed = time.monotonic() - t0
            if result.skipped:
                return StageResult(
                    "transcript_qa",
                    "skipped",
                    elapsed,
                    detail=result.reason,
                )
            return StageResult(
                "transcript_qa",
                "success",
                elapsed,
                detail=(
                    f"{result.status}: {result.finding_count} findings, "
                    f"{result.blocking_count} blocking"
                ),
            )

        elif stage_name == "review_gate_transcript_qa":
            from btcedu.core.reviewer import (
                approve_stage_for_artifacts,
                create_review_task,
                has_approved_review_for_artifacts,
                has_pending_review_for_artifacts,
                supersede_pending_reviews,
            )
            from btcedu.core.transcript_qa import load_transcript_qa
            from btcedu.core.transcript_qa import review_artifacts as transcript_qa_artifacts

            qa = load_transcript_qa(settings, episode.episode_id)
            if not qa:
                raise ValueError(f"Transcript QA artifact missing for episode {episode.episode_id}")
            if not qa.get("blocked"):
                supersede_pending_reviews(session, episode.episode_id, "transcript_qa")
                elapsed = time.monotonic() - t0
                return StageResult(
                    "review_gate_transcript_qa",
                    "success",
                    elapsed,
                    detail=f"auto-continued ({qa.get('status', 'green')})",
                )
            artifacts = transcript_qa_artifacts(settings, episode.episode_id)
            if has_approved_review_for_artifacts(
                session,
                episode.episode_id,
                "transcript_qa",
                artifacts,
            ):
                elapsed = time.monotonic() - t0
                return StageResult(
                    "review_gate_transcript_qa",
                    "success",
                    elapsed,
                    detail="transcript QA review approved",
                )
            if has_pending_review_for_artifacts(
                session,
                episode.episode_id,
                "transcript_qa",
                artifacts,
            ):
                elapsed = time.monotonic() - t0
                return StageResult(
                    "review_gate_transcript_qa",
                    "review_pending",
                    elapsed,
                    detail="awaiting transcript QA review",
                )

            # Optional automatic adjudication: when the transcript QA gate is
            # blocking and not already human-approved, let an independent model
            # decide whether to auto-continue. On approval we record an
            # artifact-bound transcript_qa approval — exactly what a human
            # reviewer produces.
            from btcedu.core.qa_reviewer import adjudicate_transcript_qa_gate

            adjudication = adjudicate_transcript_qa_gate(
                session, episode.episode_id, settings, qa=qa
            )
            if adjudication.performed and adjudication.approved:
                approve_stage_for_artifacts(
                    session,
                    episode.episode_id,
                    "transcript_qa",
                    artifacts,
                    notes=(
                        "auto-adjudicated "
                        f"({adjudication.verdict or 'approve'}) via "
                        f"{adjudication.model}: {adjudication.reason}"[:480]
                    ),
                )
                elapsed = time.monotonic() - t0
                logger.info(
                    "  review_gate_transcript_qa auto-adjudicated %s → continue (%s)",
                    adjudication.verdict or "approve",
                    episode.episode_id,
                )
                return StageResult(
                    "review_gate_transcript_qa",
                    "success",
                    elapsed,
                    detail="transcript QA auto-adjudicated",
                )

            create_review_task(
                session,
                episode.episode_id,
                stage="transcript_qa",
                artifact_paths=artifacts,
            )
            elapsed = time.monotonic() - t0
            return StageResult(
                "review_gate_transcript_qa",
                "review_pending",
                elapsed,
                detail="blocking transcript QA review created",
            )

        elif stage_name == "review_gate_1":
            from btcedu.core.reviewer import (
                auto_approve_stage,
                create_review_task,
                has_approved_review,
                has_pending_review,
            )

            # Check if already approved
            auto_approve, _ = _profile_pipeline_flags(settings, episode)
            if auto_approve or has_approved_review(session, episode.episode_id, "correct"):
                if auto_approve:
                    corrected = (
                        Path(settings.transcripts_dir)
                        / episode.episode_id
                        / "transcript.corrected.de.txt"
                    )
                    auto_approve_stage(session, episode.episode_id, "correct", [str(corrected)])
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

        elif stage_name == "segment":
            from btcedu.core.segmenter import segment_broadcast

            result = segment_broadcast(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0

            if result.skipped:
                return StageResult("segment", "skipped", elapsed, detail="not enabled or current")
            else:
                return StageResult(
                    "segment",
                    "success",
                    elapsed,
                    detail=f"{result.story_count} stories (${result.cost_usd:.4f})",
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
                approve_stage_for_artifacts,
                auto_approve_stage,
                create_review_task,
                has_approved_review,
                has_approved_review_for_artifacts,
                has_pending_review,
                has_pending_review_for_artifacts,
                supersede_pending_reviews,
            )

            # Phase 7: independent factual quality gate with bounded targeted
            # repairs. RED (or an exhausted non-green gate) blocks the pipeline;
            # GREEN records the narration hash and auto-continues. QA exceptions
            # are NOT swallowed for gate-scoped episodes — a genuine QA failure
            # fails the stage. Legacy (non-scoped) episodes keep advisory QA.
            qa_result = None
            scoped = _quality_gate_scoped(settings, episode)
            if scoped:
                from btcedu.core.qa_reviewer import resolve_translation_quality_gate

                qa_result = resolve_translation_quality_gate(session, episode.episode_id, settings)
            elif getattr(settings, "qa_review_enabled", False):
                # Advisory QA (never blocks) for non-gate-scoped legacy profiles.
                try:
                    from btcedu.core.qa_reviewer import generate_qa_review

                    generate_qa_review(session, episode.episode_id, settings)
                except Exception as qa_exc:  # noqa: BLE001
                    logger.warning(
                        "Advisory QA failed for %s (non-fatal): %s",
                        episode.episode_id,
                        qa_exc,
                    )

            gate_active = (
                scoped
                and qa_result is not None
                and qa_result.decision in {"green", "yellow", "red"}
            )
            if gate_active:
                from btcedu.core.qa_reviewer import gate_review_artifacts

                artifacts = gate_review_artifacts(settings, episode.episode_id)
                if "cost_limit" in qa_result.reasons:
                    elapsed = time.monotonic() - t0
                    return StageResult(
                        "review_gate_2",
                        "failed",
                        elapsed,
                        error=("[cost_limit] Episode cost limit reached during translation QA"),
                    )
                manual_ok = has_approved_review_for_artifacts(
                    session, episode.episode_id, "translation_qa", artifacts
                )

                # Optional automatic adjudication: when the gate is non-green and
                # not already human-approved, let an independent model decide
                # whether to auto-continue. On approval we record an
                # artifact-bound translation_qa approval — exactly what a human
                # reviewer produces — so downstream chapterize proceeds too.
                if qa_result.decision != "green" and not manual_ok:
                    from btcedu.core.qa_reviewer import adjudicate_quality_gate

                    adjudication = adjudicate_quality_gate(session, episode.episode_id, settings)
                    if adjudication.performed and adjudication.approved:
                        approve_stage_for_artifacts(
                            session,
                            episode.episode_id,
                            "translation_qa",
                            artifacts,
                            notes=(
                                "auto-adjudicated "
                                f"({adjudication.verdict or 'approve'}) via "
                                f"{adjudication.model}: {adjudication.reason}"[:480]
                            ),
                        )
                        manual_ok = True
                        logger.info(
                            "  review_gate_2 auto-adjudicated %s → continue (%s)",
                            adjudication.verdict or "approve",
                            episode.episode_id,
                        )

                if qa_result.decision == "green" or manual_ok:
                    if qa_result.decision == "green":
                        supersede_pending_reviews(session, episode.episode_id, "translation_qa")
                    # Keep the legacy adapt-approval invariant for auto-approve
                    # profiles so downstream adapt-review checks still pass.
                    auto_approve, _ = _profile_pipeline_flags(settings, episode)
                    if auto_approve:
                        adapted = (
                            Path(settings.outputs_dir) / episode.episode_id / "script.adapted.tr.md"
                        )
                        auto_approve_stage(session, episode.episode_id, "adapt", [str(adapted)])
                    elapsed = time.monotonic() - t0
                    detail = (
                        "quality gate GREEN — narration approved"
                        if qa_result.decision == "green"
                        else "quality gate manually approved"
                    )
                    logger.info("  review_gate_2 %s (%s)", detail, episode.episode_id)
                    return StageResult("review_gate_2", "success", elapsed, detail=detail)

                # RED or exhausted non-green — block on an artifact-bound review.
                if has_pending_review_for_artifacts(
                    session,
                    episode.episode_id,
                    "translation_qa",
                    artifacts,
                ):
                    elapsed = time.monotonic() - t0
                    return StageResult(
                        "review_gate_2",
                        "review_pending",
                        elapsed,
                        detail=f"awaiting translation QA review ({qa_result.decision})",
                    )
                diff_path = (
                    Path(settings.outputs_dir)
                    / episode.episode_id
                    / "review"
                    / "adaptation_diff.json"
                )
                create_review_task(
                    session,
                    episode.episode_id,
                    stage="translation_qa",
                    artifact_paths=artifacts,
                    diff_path=str(diff_path) if diff_path.exists() else None,
                )
                elapsed = time.monotonic() - t0
                logger.info(
                    "  review_gate_2 quality gate %s — blocking review created (%s)",
                    qa_result.decision.upper(),
                    episode.episode_id,
                )
                return StageResult(
                    "review_gate_2",
                    "review_pending",
                    elapsed,
                    detail=f"quality gate {qa_result.decision.upper()} — review created",
                )

            # QA gate inactive (disabled/dry-run/artifacts missing) — fall back to
            # the legacy adaptation review gate.
            auto_approve, _ = _profile_pipeline_flags(settings, episode)
            if auto_approve or has_approved_review(session, episode.episode_id, "adapt"):
                if auto_approve:
                    adapted = (
                        Path(settings.outputs_dir) / episode.episode_id / "script.adapted.tr.md"
                    )
                    auto_approve_stage(session, episode.episode_id, "adapt", [str(adapted)])
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
            qa_md_path = Path(settings.outputs_dir) / episode.episode_id / "qa_review.md"

            artifact_paths = [str(adapted_path)]
            if qa_md_path.exists():
                artifact_paths.append(str(qa_md_path))

            create_review_task(
                session,
                episode.episode_id,
                stage="adapt",
                artifact_paths=artifact_paths,
                diff_path=str(diff_path) if diff_path.exists() else None,
            )
            elapsed = time.monotonic() - t0
            return StageResult(
                "review_gate_2",
                "review_pending",
                elapsed,
                detail="adaptation review task created",
            )

        elif stage_name == "review_gate_translate":
            from btcedu.core.reviewer import (
                auto_approve_stage,
                create_review_task,
                has_approved_review,
                has_pending_review,
            )
            from btcedu.core.translation_diff import compute_translation_diff

            auto_approve, _ = _profile_pipeline_flags(settings, episode)
            if auto_approve or has_approved_review(session, episode.episode_id, "translate"):
                if auto_approve:
                    stories = (
                        Path(settings.outputs_dir) / episode.episode_id / "stories_translated.json"
                    )
                    auto_approve_stage(session, episode.episode_id, "translate", [str(stories)])
                elapsed = time.monotonic() - t0
                return StageResult(
                    "review_gate_translate",
                    "success",
                    elapsed,
                    detail="translation review approved",
                )

            if has_pending_review(session, episode.episode_id):
                elapsed = time.monotonic() - t0
                return StageResult(
                    "review_gate_translate",
                    "review_pending",
                    elapsed,
                    detail="awaiting translation review",
                )

            # Generate bilingual diff and create review task
            stories_path = (
                Path(settings.outputs_dir) / episode.episode_id / "stories_translated.json"
            )
            diff_path = (
                Path(settings.outputs_dir) / episode.episode_id / "review" / "translation_diff.json"
            )
            if stories_path.exists():
                diff_data = compute_translation_diff(stories_path)
                diff_path.parent.mkdir(parents=True, exist_ok=True)
                diff_path.write_text(json.dumps(diff_data, ensure_ascii=False, indent=2))

            transcript_path = (
                Path(settings.transcripts_dir) / episode.episode_id / "transcript.tr.txt"
            )
            create_review_task(
                session,
                episode.episode_id,
                stage="translate",
                artifact_paths=[str(stories_path), str(transcript_path)],
                diff_path=str(diff_path) if diff_path.exists() else None,
            )
            elapsed = time.monotonic() - t0
            return StageResult(
                "review_gate_translate",
                "review_pending",
                elapsed,
                detail="translation review task created",
            )

        elif stage_name == "script":
            from btcedu.core.scripter import generate_script

            result = generate_script(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0

            if result.skipped:
                return StageResult("script", "skipped", elapsed, detail="already up-to-date")
            return StageResult(
                "script",
                "success",
                elapsed,
                detail=(
                    f"{result.story_count} stories, "
                    f"~{result.estimated_duration_seconds:.0f}s, "
                    f"anchor {result.anchor_share:.0%}, "
                    f"${result.cost_usd:.4f}"
                ),
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

        elif stage_name == "frameextract":
            from btcedu.core.frame_extractor import extract_frames

            result = extract_frames(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0
            if result.skipped:
                return StageResult("frameextract", "skipped", elapsed, "frames current")
            detail = (
                f"{result.total_frames} frames extracted, "
                f"{result.assigned_frames} assigned to chapters"
            )
            if result.cost_usd > 0:
                detail += f" (${result.cost_usd:.4f})"
            return StageResult("frameextract", "success", elapsed, detail)

        elif stage_name == "imagegen":
            provider = _imagegen_provider(settings, episode)

            # Gemini frame-editing (news video frames) — only when explicitly
            # configured and enabled.
            use_gemini = (
                provider == "gemini_frame_edit"
                and settings.gemini_image_edit_enabled
                and settings.gemini_api_key
                and getattr(episode, "content_profile", "") == "tagesschau_tr"
            )
            # Generative per-chapter images (Flux photoreal + Ideogram text/maps,
            # DALL-E 3 fallback) via smart routing.
            from btcedu.core.image_generator import GENERATIVE_PROVIDERS

            use_generative = provider in GENERATIVE_PROVIDERS

            if use_gemini:
                from btcedu.core.frame_editor import edit_frames

                result = edit_frames(session, episode.episode_id, settings, force=force)
                elapsed = time.monotonic() - t0
                if result.skipped:
                    return StageResult("imagegen", "skipped", elapsed, "frame edits current")
                return StageResult(
                    "imagegen",
                    "success",
                    elapsed,
                    detail=(
                        f"{result.chapters_edited} frames edited (Gemini), "
                        f"{result.chapters_skipped} skipped, "
                        f"${result.total_cost_usd:.4f}"
                    ),
                )
            elif use_generative:
                from btcedu.core.image_generator import generate_images

                result = generate_images(session, episode.episode_id, settings, force=force)
                elapsed = time.monotonic() - t0
                if result.skipped:
                    return StageResult("imagegen", "skipped", elapsed, "images current")
                return StageResult(
                    "imagegen",
                    "success",
                    elapsed,
                    detail=(
                        f"{result.generated_count} generated, "
                        f"{result.template_count} placeholders, "
                        f"{result.failed_count} failed "
                        f"(${result.cost_usd:.4f})"
                    ),
                )
            else:
                # Default: stock images (Pexels search + rank)
                from btcedu.core.stock_images import rank_candidates, search_stock_images

                search_stock_images(session, episode.episode_id, settings, force=force)
                rank_result = rank_candidates(session, episode.episode_id, settings, force=force)
                elapsed = time.monotonic() - t0

                return StageResult(
                    "imagegen",
                    "success",
                    elapsed,
                    detail=(
                        f"{rank_result.chapters_ranked} chapters ranked, "
                        f"{rank_result.chapters_skipped} skipped, "
                        f"${rank_result.total_cost_usd:.4f}"
                    ),
                )

        elif stage_name == "review_gate_stock":
            from btcedu.core.reviewer import (
                auto_approve_stage,
                create_review_task,
                has_approved_review,
                has_pending_review,
            )
            from btcedu.core.stock_images import finalize_selections

            # Check if already approved
            auto_approve, _ = _profile_pipeline_flags(settings, episode)
            if auto_approve or has_approved_review(session, episode.episode_id, "stock_images"):
                if auto_approve:
                    auto_approve_stage(session, episode.episode_id, "stock_images")
                select_result = finalize_selections(session, episode.episode_id, settings)
                elapsed = time.monotonic() - t0
                return StageResult(
                    "review_gate_stock",
                    "success",
                    elapsed,
                    detail=(
                        f"stock images approved, "
                        f"{select_result.selected_count} finalized, "
                        f"{select_result.placeholder_count} placeholders"
                    ),
                )

            # Check if pending review exists
            if has_pending_review(session, episode.episode_id):
                elapsed = time.monotonic() - t0
                return StageResult(
                    "review_gate_stock",
                    "review_pending",
                    elapsed,
                    detail="awaiting stock image review",
                )

            # Create review task
            candidates_manifest_path = (
                Path(settings.outputs_dir)
                / episode.episode_id
                / "images"
                / "candidates"
                / "candidates_manifest.json"
            )
            chapters_path = Path(settings.outputs_dir) / episode.episode_id / "chapters.json"

            create_review_task(
                session,
                episode.episode_id,
                stage="stock_images",
                artifact_paths=[
                    str(candidates_manifest_path),
                    str(chapters_path),
                ],
                diff_path=None,
            )
            elapsed = time.monotonic() - t0
            return StageResult(
                "review_gate_stock",
                "review_pending",
                elapsed,
                detail="stock image review task created",
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

        elif stage_name == "sceneplan":
            from btcedu.core.scene_planner import plan_scenes

            result = plan_scenes(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0

            if result.skipped:
                return StageResult("sceneplan", "skipped", elapsed, detail="already up-to-date")
            return StageResult(
                "sceneplan",
                "success",
                elapsed,
                detail=(
                    f"{result.scene_count} scenes, "
                    f"{result.anchor_scene_count} presenter "
                    f"({result.anchor_duration_seconds:.1f}s)"
                ),
            )

        elif stage_name == "anchorgen":
            from btcedu.core.anchor_generator import generate_anchors

            result = generate_anchors(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0

            if result.skipped:
                return StageResult("anchorgen", "skipped", elapsed, detail="anchor current")
            else:
                return StageResult(
                    "anchorgen",
                    "success",
                    elapsed,
                    detail=(
                        f"{result.segment_count} anchor segments, "
                        f"{result.total_duration_seconds:.1f}s, "
                        f"${result.cost_usd:.4f}"
                    ),
                )

        elif stage_name == "render":
            from btcedu.core.remote_render import get_render_mode, render_video_remote
            from btcedu.core.renderer import render_video

            mode = get_render_mode(session, settings)
            where = "local"
            if mode == "github":
                try:
                    result = render_video_remote(session, episode.episode_id, settings, force=force)
                    where = "github"
                except Exception as exc:
                    if not getattr(settings, "github_render_fallback_local", True):
                        raise
                    # The Pi can still do the work, just slower. Losing an
                    # episode because GitHub was unreachable would be worse.
                    logger.warning(
                        "Remote render failed for %s (%s); falling back to local render",
                        episode.episode_id,
                        exc,
                    )
                    session.rollback()
                    episode.error_message = None
                    session.commit()
                    result = render_video(session, episode.episode_id, settings, force=force)
            else:
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
                        f"{result.total_size_bytes / 1024 / 1024:.1f}MB "
                        f"[{where}]"
                    ),
                )

        elif stage_name == "review_gate_anchor":
            from btcedu.core.anchor_review import (
                collect_state,
                ensure_review_task,
                has_current_approval,
                has_current_rejection,
            )

            state = collect_state(session, episode.episode_id, settings)
            elapsed = time.monotonic() - t0

            if not state.enabled:
                return StageResult(
                    "review_gate_anchor",
                    "skipped",
                    elapsed,
                    detail=state.reason or "avatar review not applicable",
                )
            if not state.scenes:
                # Nothing was generated, so there is nothing to look at. A gate
                # here would be a task no operator could ever action.
                return StageResult(
                    "review_gate_anchor",
                    "skipped",
                    elapsed,
                    detail="no presenter scenes in this episode",
                )

            # Deliberately not gated on auto_approve_reviews: a profile may run
            # its editorial gates unattended and still want a human to look at
            # the face that will be broadcast.
            if has_current_approval(session, episode.episode_id, settings):
                return StageResult(
                    "review_gate_anchor",
                    "success",
                    elapsed,
                    detail=f"presenter clips approved ({len(state.scenes)} scenes)",
                )

            if has_current_rejection(session, episode.episode_id, settings):
                return StageResult(
                    "review_gate_anchor",
                    "failed",
                    elapsed,
                    detail="presenter clips rejected",
                    error=(
                        "[review] The avatar stage was rejected. Regenerate the "
                        "flagged scenes or clear the rejection before rendering."
                    ),
                )

            task = ensure_review_task(session, episode.episode_id, settings)
            elapsed = time.monotonic() - t0
            detail = f"awaiting avatar review (task {task.id})"
            if state.blockers:
                detail += f"; {len(state.blockers)} blocker(s)"
            return StageResult(
                "review_gate_anchor",
                "review_pending",
                elapsed,
                detail=detail,
            )

        elif stage_name == "review_gate_3":
            from btcedu.core.reviewer import (
                auto_approve_stage,
                create_review_task,
                has_approved_review,
                has_pending_review,
            )

            # Check if already approved
            auto_approve, auto_publish = _profile_pipeline_flags(settings, episode)

            # Generate proposed YouTube metadata (title/description/tags) so it
            # can be reviewed at this gate and reused at publish time. Never let
            # a metadata failure block the review gate.
            try:
                from btcedu.core.publisher import generate_metadata_suggestion

                generate_metadata_suggestion(session, episode.episode_id, settings)
            except Exception as exc:  # pragma: no cover - defensive
                logger.warning("Gate 3 metadata suggestion failed: %s", exc)

            # Run deterministic weather video checks (blank/freeze/stale).
            # CRITICAL findings with publish_blocked prevent approval.
            # Fail closed: a checker crash means we cannot safely approve.
            _final_review_blocked = False
            _final_review_detail = ""
            _final_review_error = ""

            # WP-8B: the video is only reviewable if it is still made of the
            # files it was rendered from. A picture or a narration take that
            # changed after the render means the draft on disk and the inputs
            # on disk describe two different broadcasts, and the reviewer would
            # be approving a video whose sources no longer exist.
            try:
                from btcedu.core.render_input_collector import require_episode_inputs_intact
                from btcedu.core.render_inputs import RenderInputError

                require_episode_inputs_intact(
                    episode.episode_id, settings, episode, context="final review"
                )
            except RenderInputError as exc:
                _final_review_blocked = True
                _final_review_detail = "render inputs changed since the render"
                _final_review_error = f"[integrity] {exc}"
                logger.warning(
                    "Final review blocked for %s: %s", episode.episode_id, exc
                )
            except Exception as exc:  # noqa: BLE001 - fail closed on a checker crash
                _final_review_blocked = True
                _final_review_detail = f"render input verification crashed: {exc}"
                _final_review_error = f"[unknown] render input verification crashed: {exc}"
                logger.error(
                    "Render input verification crashed for %s (fail-closed): %s",
                    episode.episode_id,
                    exc,
                )
            _fr_output = (
                Path(settings.outputs_dir)
                / episode.episode_id
                / "render"
                / "final_review_findings.json"
            )
            try:
                from btcedu.core.final_review import run_weather_video_checks

                _fr_result = run_weather_video_checks(episode.episode_id, settings.outputs_dir)
                if _fr_result.publish_blocked:
                    _final_review_blocked = True
                    _blocking = [f for f in _fr_result.findings if f.publish_blocked]
                    _final_review_detail = (
                        f"weather video checks blocked publish: "
                        f"{len(_blocking)} critical finding(s)"
                    )
                    _final_review_error = "[validation] " + "; ".join(f.message for f in _blocking)
                    logger.warning(
                        "Final review blocked for %s: %s",
                        episode.episode_id,
                        "; ".join(f.message for f in _blocking),
                    )
                    # Persist findings for dashboard visibility
                    _fr_output.parent.mkdir(parents=True, exist_ok=True)
                    _fr_output.write_text(
                        json.dumps(
                            [f.model_dump() for f in _fr_result.findings],
                            indent=2,
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                elif _fr_result.findings:
                    # Non-blocking findings: persist for visibility
                    _fr_output.parent.mkdir(parents=True, exist_ok=True)
                    _fr_output.write_text(
                        json.dumps(
                            [f.model_dump() for f in _fr_result.findings],
                            indent=2,
                            ensure_ascii=False,
                        ),
                        encoding="utf-8",
                    )
                else:
                    # No findings: clear stale findings file from previous runs
                    if _fr_output.exists():
                        _fr_output.unlink()
            except Exception as exc:
                # Fail closed: checker crash blocks the gate so review cannot
                # safely approve. This is intentional — never silently skip.
                _final_review_blocked = True
                _final_review_detail = f"weather video checker crashed: {exc}"
                _final_review_error = f"[unknown] weather video checker crashed: {exc}"
                logger.error(
                    "Final review weather checks crashed for %s (fail-closed): %s",
                    episode.episode_id,
                    exc,
                )

            if _final_review_blocked:
                elapsed = time.monotonic() - t0
                return StageResult(
                    "review_gate_3",
                    "failed",
                    elapsed,
                    detail=_final_review_detail,
                    error=_final_review_error or _final_review_detail,
                )

            if auto_approve or has_approved_review(session, episode.episode_id, "render"):
                if auto_approve:
                    draft = Path(settings.outputs_dir) / episode.episode_id / "render" / "draft.mp4"
                    auto_approve_stage(session, episode.episode_id, "render", [str(draft)])
                # Set episode status to APPROVED (final state before publish)
                episode.status = EpisodeStatus.APPROVED
                session.commit()
                elapsed = time.monotonic() - t0
                return StageResult(
                    "review_gate_3",
                    "success",
                    elapsed,
                    detail="video review approved, episode marked APPROVED",
                )

            # Check if a pending review already exists
            if has_pending_review(session, episode.episode_id):
                elapsed = time.monotonic() - t0
                return StageResult(
                    "review_gate_3",
                    "review_pending",
                    elapsed,
                    detail="awaiting video review",
                )

            # Create a new review task
            draft_path = Path(settings.outputs_dir) / episode.episode_id / "render" / "draft.mp4"
            manifest_path = (
                Path(settings.outputs_dir) / episode.episode_id / "render" / "render_manifest.json"
            )
            chapters_path = Path(settings.outputs_dir) / episode.episode_id / "chapters.json"
            metadata_path = (
                Path(settings.outputs_dir) / episode.episode_id / "render" / "youtube_metadata.json"
            )

            _artifacts = [str(draft_path), str(chapters_path)]
            if metadata_path.exists():
                _artifacts.append(str(metadata_path))

            create_review_task(
                session,
                episode.episode_id,
                stage="render",
                artifact_paths=_artifacts,
                diff_path=str(manifest_path) if manifest_path.exists() else None,
            )
            elapsed = time.monotonic() - t0
            return StageResult(
                "review_gate_3",
                "review_pending",
                elapsed,
                detail="video review task created",
            )

        elif stage_name == "publish":
            _, auto_publish = _profile_pipeline_flags(settings, episode)
            if not auto_publish:
                # auto_publish disabled (e.g. tagesschau): the pipeline must never
                # upload, even after the final publish review is approved. Approval
                # authorizes a later explicit CLI/web publish action only.
                from btcedu.core.publisher import (
                    _publish_artifact_paths,
                    request_publish_review,
                )
                from btcedu.core.reviewer import has_approved_review_for_artifacts

                elapsed = time.monotonic() - t0
                artifacts = _publish_artifact_paths(episode.episode_id, settings)
                if has_approved_review_for_artifacts(
                    session, episode.episode_id, "publish", artifacts
                ):
                    return StageResult(
                        "publish",
                        "skipped",
                        elapsed,
                        detail="final publish approved; explicit manual publish required",
                    )

                try:
                    request_publish_review(session, episode.episode_id, settings)
                except Exception as exc:  # pragma: no cover - defensive
                    logger.warning(
                        "Could not create publish review for %s: %s",
                        episode.episode_id,
                        exc,
                    )
                return StageResult(
                    "publish",
                    "review_pending",
                    elapsed,
                    detail="awaiting final publish approval (auto_publish disabled for profile)",
                )

            from btcedu.core.publisher import publish_video

            result = publish_video(session, episode.episode_id, settings, force=force)
            elapsed = time.monotonic() - t0
            if result.skipped:
                return StageResult("publish", "skipped", elapsed, detail="already published")
            detail = result.youtube_url or "published (dry-run)"
            return StageResult("publish", "success", elapsed, detail=detail)

        else:
            raise ValueError(f"Unknown stage: {stage_name}")

    except Exception as e:
        from btcedu.services.errors import (
            ERROR_SUGGESTIONS,
            PipelineError,
            classify_error,
        )

        elapsed = time.monotonic() - t0
        if isinstance(e, PipelineError):
            category = e.category
            error_msg = str(e)
        else:
            category = classify_error(e)
            suggestion = ERROR_SUGGESTIONS.get(category, "Check logs for details.")
            error_msg = f"[{category.value}] {e} — {suggestion}"
        return StageResult(stage_name, "failed", elapsed, error=error_msg)


def run_episode_pipeline(
    session: Session,
    episode: Episode,
    settings: Settings,
    force: bool = False,
    stage_callback: Callable[[str], None] | None = None,
    lease_guard=None,
) -> PipelineReport:
    """Run the full pipeline for a single episode.

    Chains the v2 stages: download -> transcribe -> correct -> translate ->
    adapt -> chapterize -> imagegen -> tts -> render -> publish (with review gates).
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

    from btcedu.core.profile_validation import assert_episode_profile_valid

    # Before the first stage, not during it: a plan built from a default
    # because the profile could not be read would produce a video nobody
    # asked for, and it would look like an ordinary success.
    assert_episode_profile_valid(settings, episode)

    content_profile = getattr(episode, "content_profile", "bitcoin_podcast")
    from btcedu.version import get_git_commit

    logger.info(
        "Pipeline start: %s (%s) [profile=%s] [commit=%s]",
        episode.episode_id,
        episode.title,
        content_profile,
        get_git_commit(),
    )

    stages = _get_stages(settings, episode)
    for stage_name, required_status in stages:
        if lease_guard is not None:
            try:
                lease_guard.ensure_active()
            except Exception as exc:  # noqa: BLE001
                report.stages.append(StageResult(stage_name, "failed", 0.0, error=str(exc)))
                report.error = f"Stage '{stage_name}' failed: {exc}"
                session.refresh(episode)
                episode.error_message = report.error
                episode.retry_count += 1
                session.commit()
                _trigger_automatic_copilot_fix(
                    settings,
                    episode,
                    stage_name,
                    report.error,
                )
                break

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
        stage_started = _utcnow()
        tracking_run = _start_stage_pipeline_run(session, episode, stage_name, stage_started)
        result = _run_stage(session, episode, settings, stage_name, force=force)
        report.stages.append(result)

        if result.status == "success":
            _ensure_stage_pipeline_run(
                session,
                episode,
                stage_name,
                result.duration_seconds,
                stage_started,
                tracking_run=tracking_run,
            )
        else:
            _finish_stage_pipeline_run(session, tracking_run, result)

        if result.status == "failed":
            # A stage may carry its reason in `detail` instead of `error`; report
            # whichever is there rather than a bare "None".
            failure_reason = result.error or result.detail or "unknown error"
            logger.error("  Stage %s failed: %s", stage_name, failure_reason)
            report.error = f"Stage '{stage_name}' failed: {failure_reason}"

            # Record failure on the episode
            session.refresh(episode)
            episode.error_message = report.error
            episode.retry_count += 1
            session.commit()

            # Create dead-letter entry for permanent errors
            try:
                from btcedu.services.errors import (
                    ErrorCategory,
                    is_transient,
                )

                # Parse category from error string or classify
                error_str = failure_reason
                category = ErrorCategory.UNKNOWN
                for cat in ErrorCategory:
                    if f"[{cat.value}]" in error_str:
                        category = cat
                        break

                if not is_transient(category):
                    from btcedu.models.dead_letter import DeadLetterEntry
                    from btcedu.services.errors import ERROR_SUGGESTIONS

                    dlq_entry = DeadLetterEntry(
                        episode_id=episode.episode_id,
                        stage=stage_name,
                        error_category=category.value,
                        error_message=error_str[:2000],
                        suggestion=ERROR_SUGGESTIONS.get(category, "Check logs for details."),
                        retry_count=episode.retry_count,
                    )
                    session.add(dlq_entry)
                    session.commit()
                    logger.info(
                        "  Dead-letter entry created for %s/%s (%s)",
                        episode.episode_id,
                        stage_name,
                        category.value,
                    )
            except Exception:
                logger.debug("Could not create DLQ entry", exc_info=True)

            try:
                from btcedu.services.notify_service import notify_stage_failure

                notify_stage_failure(
                    settings,
                    episode_id=episode.episode_id,
                    episode_title=episode.title or "",
                    stage=stage_name,
                    error=failure_reason,
                    retry_count=episode.retry_count,
                )
            except Exception:
                logger.debug("Could not send failure notification", exc_info=True)

            _trigger_automatic_copilot_fix(settings, episode, stage_name, report.error)

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
            "correct",
            "transcript_verify",
            "segment",
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


def _trigger_automatic_copilot_fix(
    settings: Settings,
    episode: Episode,
    stage_name: str,
    error_message: str,
) -> None:
    """Launch one best-effort Copilot repair without masking the pipeline error."""
    try:
        from btcedu.core.copilot_fix import start_copilot_fix

        launch = start_copilot_fix(
            settings,
            episode.episode_id,
            episode.title or "",
            stage_name,
            error_message,
            automatic=True,
            profile=episode.content_profile or "tagesschau_tr",
        )
        if launch.started:
            logger.info(
                "  Automatic Copilot fix started for %s/%s",
                episode.episode_id,
                stage_name,
            )
        elif launch.already_attempted:
            logger.info("  Automatic Copilot fix already attempted for this exact error")
        elif launch.already_running:
            logger.warning(
                "  Automatic Copilot fix not started because session %s is busy",
                launch.session,
            )
    except Exception:
        logger.exception(
            "Could not start automatic Copilot fix for %s/%s",
            episode.episode_id,
            stage_name,
        )


def run_episode_pipeline_coordinated(
    session: Session,
    episode: Episode,
    settings: Settings,
    force: bool = False,
    stage_callback: Callable[[str], None] | None = None,
) -> PipelineReport:
    """Run one episode behind the external failover lease when enabled."""
    from btcedu.failover.coordination import acquire_pipeline_lease_guard

    guard = acquire_pipeline_lease_guard(session, episode, settings)
    try:
        report = run_episode_pipeline(
            session,
            episode,
            settings,
            force=force,
            stage_callback=stage_callback,
            lease_guard=guard,
        )
        session.refresh(episode)
        completion_status = "failed"
        if report.success or getattr(episode, "youtube_video_id", None):
            completion_status = episode.status.value
        try:
            guard.report_completion(
                status=completion_status,
                youtube_id=getattr(episode, "youtube_video_id", None),
            )
        except Exception:
            logger.warning(
                "Could not report failover completion for %s",
                episode.episode_id,
                exc_info=True,
            )
        return report
    finally:
        guard.close()


def run_pending(
    session: Session,
    settings: Settings,
    max_episodes: int | None = None,
    since: datetime | None = None,
    profile: str | None = None,
) -> list[PipelineReport]:
    """Process all pending episodes, guarded by the exclusive pipeline lock.

    Skips (returns an empty list) when another pipeline run is already active,
    so overlapping invocations (e.g. the autostart timer firing while a long
    run is in progress) do not collide on the SQLite write lock.
    """
    from btcedu.core.runlock import PipelineBusyError, pipeline_lock

    try:
        with pipeline_lock(settings):
            _mark_orphaned_pipeline_runs_interrupted(session)
            return _run_pending_locked(
                session, settings, max_episodes=max_episodes, since=since, profile=profile
            )
    except PipelineBusyError:
        logger.warning("run_pending skipped: another pipeline run is already active.")
        return []


def _run_pending_locked(
    session: Session,
    settings: Settings,
    max_episodes: int | None = None,
    since: datetime | None = None,
    profile: str | None = None,
) -> list[PipelineReport]:
    """Process all pending episodes through the pipeline.

    Queries all resumable episode statuses, ordered by published_at ASC
    (oldest first).

    Args:
        session: DB session.
        settings: Application settings.
        max_episodes: Limit number of episodes to process.
        since: Only process episodes published after this date.
        profile: If given, only process episodes with this content_profile.

    Returns:
        List of PipelineReports.
    """
    query = (
        session.query(Episode)
        .filter(
            Episode.status.in_(_RESUMABLE_EPISODE_STATUSES),
            Episode.error_message.is_(None),
        )
        .order_by(Episode.published_at.asc())
    )

    if since is not None:
        query = query.filter(Episode.published_at >= since)

    if profile is not None:
        query = query.filter(Episode.content_profile == profile)

    if max_episodes is not None:
        query = query.limit(max_episodes)

    episodes = query.all()

    # Filter out episodes with active review tasks to avoid wasteful
    # re-processing — EXCEPT episodes on unattended profiles
    # (auto_approve_reviews), whose review gates auto-approve or auto-adjudicate
    # via an independent model. For those we must resume so the gate can decide
    # and supersede its own (possibly stale) pending review; otherwise a review
    # created before adjudication was enabled would wedge the episode forever.
    if episodes:
        from btcedu.core.reviewer import has_pending_review

        episodes = [
            ep
            for ep in episodes
            if _profile_pipeline_flags(settings, ep)[0]
            or not has_pending_review(session, ep.episode_id)
        ]

    if not episodes:
        logger.info("No pending episodes to process.")
        return []

    logger.info("Processing %d pending episode(s)...", len(episodes))

    reports = []
    for ep in episodes:
        try:
            report = run_episode_pipeline_coordinated(session, ep, settings)
        except Exception as exc:
            from btcedu.services.failover_service import FailoverExecutionRejected

            if not isinstance(exc, FailoverExecutionRejected):
                raise
            logger.info("Skipping %s: %s", ep.episode_id, exc)
            continue
        reports.append(report)

    return reports


def run_latest(
    session: Session,
    settings: Settings,
    profile: str | None = None,
    detect_all: bool = False,
) -> PipelineReport | None:
    """Detect + process the newest pending episode, guarded by the run lock.

    Raises PipelineBusyError when another pipeline run is already active so
    callers can distinguish lock contention from an empty pending queue.
    """
    from btcedu.core.runlock import pipeline_lock

    with pipeline_lock(settings):
        _mark_orphaned_pipeline_runs_interrupted(session)
        return _run_latest_locked(session, settings, profile=profile, detect_all=detect_all)


def _run_latest_locked(
    session: Session,
    settings: Settings,
    profile: str | None = None,
    detect_all: bool = False,
) -> PipelineReport | None:
    """Detect new episodes and process the newest pending one.

    Calls detect first, then finds the newest pending episode
    and runs the pipeline.

    Args:
        profile: If given, only consider episodes with this content_profile.
        detect_all: If True, detect from every active channel using each
            channel's own profile (applies per-profile title filters, e.g.
            the tagesschau 20:00 Uhr broadcast) instead of the single default
            feed.

    Returns:
        PipelineReport for the processed episode, or None if nothing to do.
    """
    # The locally recorded broadcast is ready about twenty minutes after air
    # time, one to two hours before the same broadcast reaches YouTube. Checking
    # it first is what turns this ten-minute timer into a same-evening pipeline.
    # It is a no-op for profiles without a local recorder, and any failure falls
    # through to the feed below rather than stopping the run.
    from btcedu.core.detector import (
        DetectResult,
        detect_all_active_channels,
        detect_episodes,
        detect_local_recordings,
    )

    try:
        local_result = detect_local_recordings(session, settings, profile_name=profile)
    except Exception:
        logger.exception("Local recorder detection failed; falling back to the feed")
        session.rollback()
        local_result = DetectResult()
    if local_result.new:
        logger.info("Local recorder supplied %d new episode(s)", local_result.new)

    if detect_all:
        detect_result = detect_all_active_channels(session, settings)
    else:
        detect_result = detect_episodes(session, settings)
    detect_result.found += local_result.found
    detect_result.new += local_result.new
    logger.info(
        "Detection: found=%d, new=%d, total=%d",
        detect_result.found,
        detect_result.new,
        detect_result.total,
    )

    # Find newest pending episode
    candidates_query = (
        session.query(Episode)
        .filter(
            Episode.status.in_(_RESUMABLE_EPISODE_STATUSES),
            Episode.error_message.is_(None),
        )
        .order_by(Episode.published_at.desc())
    )

    if profile is not None:
        candidates_query = candidates_query.filter(Episode.content_profile == profile)

    candidates = candidates_query.all()

    # Filter out episodes with active review tasks — except unattended profiles
    # (auto_approve_reviews), whose gates auto-approve/auto-adjudicate. Those
    # must be resumed so the gate can decide and supersede its own (possibly
    # stale) pending review instead of wedging the episode forever.
    from btcedu.core.reviewer import has_pending_review

    episode = None
    for candidate in candidates:
        if _profile_pipeline_flags(settings, candidate)[0] or not has_pending_review(
            session, candidate.episode_id
        ):
            episode = candidate
            break

    if episode is None:
        logger.info("No pending episodes after detection.")
        return None

    try:
        return run_episode_pipeline_coordinated(session, episode, settings)
    except Exception as exc:
        from btcedu.services.failover_service import FailoverExecutionRejected

        if not isinstance(exc, FailoverExecutionRejected):
            raise
        logger.info("run_latest skipped %s: %s", episode.episode_id, exc)
        return None


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

    if not episode.error_message and episode.status not in {
        EpisodeStatus.FAILED,
        EpisodeStatus.COST_LIMIT,
    }:
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

    if episode.status in {EpisodeStatus.FAILED, EpisodeStatus.COST_LIMIT}:
        failed_run = (
            session.query(PipelineRun)
            .filter(
                PipelineRun.episode_id == episode.id,
                PipelineRun.status == RunStatus.FAILED,
            )
            .order_by(PipelineRun.id.desc())
            .first()
        )
        if failed_run is None:
            raise ValueError(
                f"Episode {episode_id} has status '{episode.status.value}' "
                "but no failed pipeline run to resume."
            )
        failed_stage_name = next(
            (
                stage_name
                for stage_name, pipeline_stage in _STAGE_NAME_TO_PIPELINE_STAGE.items()
                if pipeline_stage == failed_run.stage
            ),
            None,
        )
        resume_status = next(
            (
                required_status
                for stage_name, required_status in _get_stages(settings, episode)
                if stage_name == failed_stage_name
            ),
            None,
        )
        if resume_status is None:
            raise ValueError(
                f"Episode {episode_id} cannot resume failed stage '{failed_run.stage.value}'."
            )
        episode.status = resume_status

    # Clear error to allow pipeline to proceed
    episode.error_message = None
    session.commit()

    return run_episode_pipeline_coordinated(
        session,
        episode,
        settings,
        stage_callback=stage_callback,
    )


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
