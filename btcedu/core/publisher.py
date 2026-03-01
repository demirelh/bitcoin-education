"""YouTube publishing: safety checks, metadata, upload, provenance."""

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func
from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, RunStatus
from btcedu.models.publish_job import PublishJob, PublishJobStatus
from btcedu.models.review import ReviewStatus, ReviewTask

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------


@dataclass
class SafetyCheck:
    """Result of one pre-publish safety check."""

    name: str
    passed: bool
    message: str


@dataclass
class PublishResult:
    """Summary of publish operation for one episode."""

    episode_id: str
    youtube_video_id: str | None = None
    youtube_url: str | None = None
    publish_job_id: int | None = None
    safety_checks: dict[str, str] = field(default_factory=dict)  # name → message
    skipped: bool = False
    dry_run: bool = False
    error: str | None = None


# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------


def _compute_hash_over_paths(paths: list[str]) -> str:
    """SHA-256 over sorted file contents (mirrors reviewer._compute_artifact_hash)."""
    h = hashlib.sha256()
    for path_str in sorted(paths):
        p = Path(path_str)
        if p.exists():
            h.update(p.read_bytes())
    return h.hexdigest()


def _get_approved_render_task(session: Session, episode_id: str) -> ReviewTask | None:
    """Return the latest approved render ReviewTask, or None."""
    return (
        session.query(ReviewTask)
        .filter(
            ReviewTask.episode_id == episode_id,
            ReviewTask.stage == "render",
            ReviewTask.status == ReviewStatus.APPROVED.value,
        )
        .order_by(ReviewTask.created_at.desc())
        .first()
    )


def _check_approval_gate(session: Session, episode: Episode) -> SafetyCheck:
    """Check 1: Episode is APPROVED and an approved render review exists."""
    if episode.status != EpisodeStatus.APPROVED:
        return SafetyCheck(
            name="approval_gate",
            passed=False,
            message=f"Episode status is '{episode.status.value}', expected 'approved'",
        )
    task = _get_approved_render_task(session, episode.episode_id)
    if not task:
        return SafetyCheck(
            name="approval_gate",
            passed=False,
            message="No approved render ReviewTask found",
        )
    return SafetyCheck(
        name="approval_gate",
        passed=True,
        message=f"Approved render review task #{task.id} found",
    )


def _check_artifact_integrity(
    session: Session, episode: Episode, settings: Settings
) -> SafetyCheck:
    """Check 2: SHA-256 of artifact paths matches hash recorded at RG3 approval."""
    task = _get_approved_render_task(session, episode.episode_id)
    if not task or not task.artifact_hash:
        return SafetyCheck(
            name="artifact_integrity",
            passed=False,
            message="No artifact_hash found on approved render ReviewTask",
        )

    if not task.artifact_paths:
        return SafetyCheck(
            name="artifact_integrity",
            passed=False,
            message="No artifact_paths stored on review task",
        )

    try:
        paths = json.loads(task.artifact_paths)
    except (json.JSONDecodeError, TypeError):
        return SafetyCheck(
            name="artifact_integrity",
            passed=False,
            message="Could not parse artifact_paths from review task",
        )

    current_hash = _compute_hash_over_paths(paths)
    if current_hash != task.artifact_hash:
        return SafetyCheck(
            name="artifact_integrity",
            passed=False,
            message=(
                f"Artifact integrity mismatch: "
                f"approved={task.artifact_hash[:16]}... "
                f"current={current_hash[:16]}..."
            ),
        )
    return SafetyCheck(
        name="artifact_integrity",
        passed=True,
        message="SHA-256 hash matches approved artifact",
    )


def _check_metadata_completeness(
    title: str, description: str, tags: list[str]
) -> SafetyCheck:
    """Check 3: Title, description, and tags are non-empty."""
    missing = []
    if not title or not title.strip():
        missing.append("title")
    if not description or not description.strip():
        missing.append("description")
    if not tags:
        missing.append("tags")
    if missing:
        return SafetyCheck(
            name="metadata_completeness",
            passed=False,
            message=f"Missing metadata: {', '.join(missing)}",
        )
    return SafetyCheck(
        name="metadata_completeness",
        passed=True,
        message="Title, description, and tags all present",
    )


def _check_cost_sanity(session: Session, episode: Episode, settings: Settings) -> SafetyCheck:
    """Check 4: Total episode cost is within max_episode_cost_usd."""
    total_cost = (
        session.query(func.sum(PipelineRun.estimated_cost_usd))
        .filter(PipelineRun.episode_id == episode.id)
        .scalar()
    ) or 0.0

    if total_cost > settings.max_episode_cost_usd:
        return SafetyCheck(
            name="cost_sanity",
            passed=False,
            message=(
                f"Episode cost ${total_cost:.2f} exceeds "
                f"budget ${settings.max_episode_cost_usd:.2f}"
            ),
        )
    return SafetyCheck(
        name="cost_sanity",
        passed=True,
        message=f"Cost ${total_cost:.2f} within budget ${settings.max_episode_cost_usd:.2f}",
    )


def _run_all_safety_checks(
    session: Session,
    episode: Episode,
    settings: Settings,
    title: str,
    description: str,
    tags: list[str],
) -> list[SafetyCheck]:
    """Run all 4 pre-publish safety checks. Returns list of SafetyCheck."""
    checks = [
        _check_approval_gate(session, episode),
        _check_artifact_integrity(session, episode, settings),
        _check_metadata_completeness(title, description, tags),
        _check_cost_sanity(session, episode, settings),
    ]
    for c in checks:
        status = "PASS" if c.passed else "FAIL"
        logger.info("Safety check [%s] %s: %s", status, c.name, c.message)
    return checks


# ---------------------------------------------------------------------------
# Metadata construction
# ---------------------------------------------------------------------------

# Base tags always included
_BASE_TAGS = ["Bitcoin", "Kripto", "Blockchain", "Türkçe", "Eğitim", "Cryptocurrency"]


def _format_timestamp(total_seconds: float) -> str:
    """Format seconds as M:SS or H:MM:SS for YouTube chapter timestamps."""
    total_seconds = max(0, int(total_seconds))
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    secs = total_seconds % 60
    if hours > 0:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def _load_tts_durations(episode_id: str, settings: Settings) -> dict[str, float]:
    """Load actual TTS durations from manifest. Returns {chapter_id: duration_seconds}."""
    manifest_path = Path(settings.outputs_dir) / episode_id / "tts" / "manifest.json"
    if not manifest_path.exists():
        return {}
    try:
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        segments = data.get("segments", [])
        return {s["chapter_id"]: float(s.get("duration_seconds", 0)) for s in segments}
    except (json.JSONDecodeError, KeyError, OSError):
        return {}


def _build_youtube_metadata(
    episode: Episode,
    settings: Settings,
) -> tuple[str, str, list[str]]:
    """Build YouTube title, description, and tags from chapters.json.

    Returns:
        (title, description, tags)
    """
    chapters_path = Path(settings.outputs_dir) / episode.episode_id / "chapters.json"

    title = episode.title
    chapters_list = []

    if chapters_path.exists():
        try:
            chapters_data = json.loads(chapters_path.read_text(encoding="utf-8"))
            # ChapterDocument top-level title
            if chapters_data.get("title"):
                title = chapters_data["title"]
            chapters_list = chapters_data.get("chapters", [])
        except (json.JSONDecodeError, OSError):
            pass

    # Truncate title to YouTube limit
    title = title[:100]

    # Load TTS durations for accurate timestamps
    tts_durations = _load_tts_durations(episode.episode_id, settings)

    # Build chapter timestamps
    timestamp_lines = []
    cumulative_seconds = 0.0
    for ch in sorted(chapters_list, key=lambda c: c.get("order", 0)):
        ch_id = ch.get("chapter_id", "")
        ch_title = ch.get("title", ch_id)
        timestamp_lines.append(f"{_format_timestamp(cumulative_seconds)} {ch_title}")
        # Use TTS actual duration, fall back to estimated from narration
        duration = tts_durations.get(ch_id, 0.0)
        if duration == 0.0:
            narration = ch.get("narration") or {}
            duration = float(narration.get("estimated_duration_seconds", 60))
        cumulative_seconds += duration

    # Build description
    # Intro: first chapter narration excerpt (up to 300 chars)
    intro = ""
    if chapters_list:
        first_ch = sorted(chapters_list, key=lambda c: c.get("order", 0))[0]
        narration = first_ch.get("narration") or {}
        text = narration.get("text", "")
        intro = text[:300].strip()
        if len(text) > 300:
            intro += "..."

    description_parts = []
    if intro:
        description_parts.append(intro)
        description_parts.append("")

    if timestamp_lines:
        # YouTube auto-detects chapters if first timestamp is 0:00
        description_parts.append("📋 Bölümler / Chapters:")
        description_parts.extend(timestamp_lines)
        description_parts.append("")

    description_parts.append("#Bitcoin #Kripto #Türkçe #Eğitim #Blockchain")
    description = "\n".join(description_parts)

    # Tags: base tags + chapter titles as additional tags
    tags = list(_BASE_TAGS)
    for ch in chapters_list[:5]:  # Limit extra tags from first 5 chapters
        ch_title = ch.get("title", "")
        if ch_title and len(ch_title) <= 30:
            tags.append(ch_title)

    # Enforce YouTube 500-char tag limit
    final_tags: list[str] = []
    total_chars = 0
    for tag in tags:
        if total_chars + len(tag) + 1 <= 500:
            final_tags.append(tag)
            total_chars += len(tag) + 1

    return title, description[:5000], final_tags


# ---------------------------------------------------------------------------
# Main publish function
# ---------------------------------------------------------------------------


def publish_video(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
    privacy: str | None = None,
) -> PublishResult:
    """Publish approved video to YouTube.

    Processing flow:
    1. Validate episode (v2, APPROVED)
    2. Idempotency: skip if already published (unless force)
    3. Build metadata (title, description, tags)
    4. Run 4 pre-publish safety checks
    5. Create PublishJob (pending)
    6. Upload via YouTube service (or dry-run placeholder)
    7. On success: update Episode + PublishJob, write provenance
    8. Return PublishResult

    Args:
        session: SQLAlchemy session.
        episode_id: Episode identifier.
        settings: Application settings.
        force: Skip idempotency check (re-publishes).
        privacy: Override privacy setting ("unlisted", "private", "public").
            Defaults to settings.youtube_default_privacy.

    Returns:
        PublishResult with video_id, url, and safety check results.

    Raises:
        ValueError: If episode not found, wrong pipeline version, wrong status,
            or any safety check fails.
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    if episode.pipeline_version != 2:
        raise ValueError(
            f"Episode {episode_id} is v1 pipeline (pipeline_version={episode.pipeline_version}). "
            "Publish is only supported for v2 pipeline."
        )

    # Allow APPROVED (or PUBLISHED with force) to proceed
    if episode.status != EpisodeStatus.APPROVED and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'approved'. Use --force to override."
        )

    # Idempotency: already published
    if episode.youtube_video_id and not force:
        logger.info(
            "Episode %s already published as %s (skipping)",
            episode_id,
            episode.youtube_video_id,
        )
        return PublishResult(
            episode_id=episode_id,
            youtube_video_id=episode.youtube_video_id,
            youtube_url=f"https://youtu.be/{episode.youtube_video_id}",
            skipped=True,
        )

    # Effective privacy setting
    effective_privacy = privacy or getattr(settings, "youtube_default_privacy", "unlisted")

    # Build metadata
    title, description, tags = _build_youtube_metadata(episode, settings)

    # Run all 4 safety checks
    checks = _run_all_safety_checks(session, episode, settings, title, description, tags)
    check_results = {c.name: c.message for c in checks}
    failed_checks = [c for c in checks if not c.passed]

    if failed_checks:
        msg = "Pre-publish safety checks failed:\n" + "\n".join(
            f"  ✗ {c.name}: {c.message}" for c in failed_checks
        )
        raise ValueError(msg)

    # Create PublishJob (pending)
    publish_job = PublishJob(
        episode_id=episode_id,
        status=PublishJobStatus.PENDING.value,
    )
    session.add(publish_job)
    session.commit()

    # Find thumbnail (first chapter image)
    thumbnail_path: Path | None = None
    images_dir = Path(settings.outputs_dir) / episode_id / "images"
    chapters_path_file = Path(settings.outputs_dir) / episode_id / "chapters.json"
    if images_dir.exists() and chapters_path_file.exists():
        try:
            chapters_data = json.loads(chapters_path_file.read_text(encoding="utf-8"))
            chapters_list = chapters_data.get("chapters", [])
            if chapters_list:
                first_ch = sorted(chapters_list, key=lambda c: c.get("order", 0))[0]
                first_ch_id = first_ch.get("chapter_id", "")
                candidate = images_dir / f"{first_ch_id}.png"
                if candidate.exists():
                    thumbnail_path = candidate
        except (json.JSONDecodeError, OSError, IndexError):
            pass

    draft_path = Path(settings.outputs_dir) / episode_id / "render" / "draft.mp4"

    # Build upload request
    from btcedu.services.youtube_service import (
        DryRunYouTubeService,
        YouTubeDataAPIService,
        YouTubeUploadRequest,
    )

    upload_req = YouTubeUploadRequest(
        video_path=draft_path,
        title=title,
        description=description,
        tags=tags,
        category_id=getattr(settings, "youtube_category_id", "27"),
        default_language=getattr(settings, "youtube_default_language", "tr"),
        privacy_status=effective_privacy,
        thumbnail_path=thumbnail_path,
    )

    is_dry_run = getattr(settings, "dry_run", False)

    if is_dry_run:
        youtube_svc = DryRunYouTubeService()
    else:
        credentials_path = getattr(settings, "youtube_credentials_path", "data/.youtube_credentials.json")
        youtube_svc = YouTubeDataAPIService(
            credentials_path=credentials_path,
            chunk_size_bytes=getattr(settings, "youtube_upload_chunk_size_mb", 10) * 1024 * 1024,
        )

    # Update PublishJob to uploading
    publish_job.status = PublishJobStatus.UPLOADING.value
    session.commit()

    # Metadata snapshot
    metadata_snapshot = {
        "title": title,
        "description": description,
        "tags": tags,
        "category_id": upload_req.category_id,
        "default_language": upload_req.default_language,
        "privacy_status": effective_privacy,
    }

    def _progress_cb(uploaded: int, total: int) -> None:
        pct = int(uploaded / total * 100) if total else 100
        logger.info("YouTube upload: %d%% (%d / %d bytes)", pct, uploaded, total)

    try:
        response = youtube_svc.upload_video(upload_req, progress_callback=_progress_cb)
    except Exception as exc:
        # Record failure
        publish_job.status = PublishJobStatus.FAILED.value
        publish_job.error_message = str(exc)
        publish_job.metadata_snapshot = json.dumps(metadata_snapshot)
        session.commit()
        logger.error("YouTube upload failed for %s: %s", episode_id, exc)
        raise

    now = _utcnow()

    # Update PublishJob with success
    publish_job.status = PublishJobStatus.PUBLISHED.value
    publish_job.youtube_video_id = response.video_id
    publish_job.youtube_url = response.video_url
    publish_job.published_at = now
    publish_job.metadata_snapshot = json.dumps(metadata_snapshot)
    session.commit()

    # Update Episode
    if not is_dry_run:
        episode.youtube_video_id = response.video_id
        episode.published_at_youtube = now
        episode.status = EpisodeStatus.PUBLISHED
        session.commit()

    # Record PipelineRun
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage="publish",
        status=RunStatus.SUCCESS.value,
        started_at=now,
        completed_at=now,
        estimated_cost_usd=0.0,
        input_tokens=0,
        output_tokens=0,
    )
    session.add(pipeline_run)
    session.commit()

    # Write provenance
    _write_provenance(
        settings=settings,
        episode_id=episode_id,
        video_id=response.video_id,
        video_url=response.video_url,
        privacy=effective_privacy,
        safety_checks=checks,
        metadata_snapshot=metadata_snapshot,
        dry_run=is_dry_run,
    )

    logger.info(
        "Episode %s published%s: %s",
        episode_id,
        " (dry-run)" if is_dry_run else "",
        response.video_url,
    )

    return PublishResult(
        episode_id=episode_id,
        youtube_video_id=response.video_id,
        youtube_url=response.video_url,
        publish_job_id=publish_job.id,
        safety_checks=check_results,
        dry_run=is_dry_run,
    )


def _write_provenance(
    settings: Settings,
    episode_id: str,
    video_id: str,
    video_url: str,
    privacy: str,
    safety_checks: list[SafetyCheck],
    metadata_snapshot: dict,
    dry_run: bool,
) -> None:
    """Write provenance JSON for the publish operation."""
    prov_dir = Path(settings.outputs_dir) / episode_id / "provenance"
    prov_dir.mkdir(parents=True, exist_ok=True)
    prov_path = prov_dir / "publish.json"
    prov_data = {
        "episode_id": episode_id,
        "published_at": _utcnow().isoformat(),
        "youtube_video_id": video_id,
        "youtube_url": video_url,
        "privacy_status": privacy,
        "dry_run": dry_run,
        "safety_checks": {c.name: [c.passed, c.message] for c in safety_checks},
        "metadata_snapshot": metadata_snapshot,
    }
    prov_path.write_text(json.dumps(prov_data, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Provenance written: %s", prov_path)


# ---------------------------------------------------------------------------
# Query helpers (used by web API)
# ---------------------------------------------------------------------------


def get_latest_publish_job(session: Session, episode_id: str) -> PublishJob | None:
    """Return the most recent PublishJob for an episode."""
    return (
        session.query(PublishJob)
        .filter(PublishJob.episode_id == episode_id)
        .order_by(PublishJob.created_at.desc())
        .first()
    )
