"""Frame extraction stage: keyframes from source video with anchor removal & styling."""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.models.chapter_schema import ChapterDocument
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, RunStatus

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------


@dataclass
class FrameExtractionResult:
    """Summary of frame extraction for one episode."""

    episode_id: str
    frames_dir: Path
    manifest_path: Path
    provenance_path: Path
    total_frames: int = 0
    anchor_frames: int = 0
    cropped_frames: int = 0
    styled_frames: int = 0
    assigned_frames: int = 0
    cost_usd: float = 0.0
    skipped: bool = False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _apply_dalle_edit_style(
    input_path: str,
    output_path: str,
    settings: Settings,
) -> str:
    """Apply style via DALL-E Edit API (dall-e-2 inpainting).

    Args:
        input_path: Path to the source frame image
        output_path: Path to write the styled image
        settings: Application settings (needs openai_api_key)

    Returns:
        output_path on success
    """
    from btcedu.services.image_gen_service import DallE3ImageService, ImageEditRequest

    service = DallE3ImageService(api_key=settings.openai_api_key)
    request = ImageEditRequest(
        image_path=Path(input_path),
        prompt=(
            "Transform this video frame into a professional sketch-style illustration. "
            "Maintain the composition and key visual elements. "
            "Clean, modern line art with subtle color accents."
        ),
        size="1024x1024",
    )
    response = service.edit_image(request)
    # Download the edited image
    DallE3ImageService.download_image(response.image_url, Path(output_path))
    return output_path


def _compute_video_hash(video_path: Path) -> str:
    """Fast hash proxy: file size + mtime (avoids reading huge files)."""
    stat = video_path.stat()
    raw = f"{stat.st_size}:{stat.st_mtime_ns}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _compute_chapters_hash(chapters_doc: ChapterDocument) -> str:
    blob = json.dumps(
        [
            {
                "chapter_id": ch.chapter_id,
                "order": ch.order,
                "duration": ch.narration.estimated_duration_seconds,
            }
            for ch in chapters_doc.chapters
        ],
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def _is_extraction_current(
    manifest_path: Path,
    provenance_path: Path,
    video_hash: str,
    chapters_hash: str,
) -> bool:
    """Return True when existing extraction is still valid."""
    if not manifest_path.exists() or not provenance_path.exists():
        return False

    stale_marker = manifest_path.parent / ".stale"
    if stale_marker.exists():
        return False

    try:
        prov = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False

    return prov.get("video_hash") == video_hash and prov.get("chapters_hash") == chapters_hash


def _load_chapters(chapters_path: Path) -> ChapterDocument:
    raw = json.loads(chapters_path.read_text(encoding="utf-8"))
    return ChapterDocument(**raw)


def _build_chapter_timeline(
    chapters_doc: ChapterDocument,
) -> list[tuple[str, float, float]]:
    """Return ``[(chapter_id, start_sec, end_sec), ...]``."""
    timeline: list[tuple[str, float, float]] = []
    cursor = 0.0
    for ch in chapters_doc.chapters:
        dur = float(ch.narration.estimated_duration_seconds)
        timeline.append((ch.chapter_id, cursor, cursor + dur))
        cursor += dur
    return timeline


def _assign_frame_to_chapter(
    timestamp: float,
    timeline: list[tuple[str, float, float]],
) -> str | None:
    for cid, start, end in timeline:
        if start <= timestamp < end:
            return cid
    # If beyond last chapter, assign to last
    if timeline:
        return timeline[-1][0]
    return None


def _mark_downstream_stale(episode_id: str, outputs_dir: Path) -> None:
    images_stale = outputs_dir / episode_id / "images" / ".stale"
    if images_stale.parent.exists():
        images_stale.write_text(
            json.dumps(
                {
                    "invalidated_at": _utcnow().isoformat(),
                    "invalidated_by": "frameextract",
                    "reason": "frames_changed",
                },
                indent=2,
            ),
            encoding="utf-8",
        )


# ---------------------------------------------------------------------------
# Main stage function
# ---------------------------------------------------------------------------


def extract_frames(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> FrameExtractionResult:
    """Extract, crop, and style keyframes from the episode source video.

    Stage flow
    ----------
    1. Locate source video (written during download).
    2. Extract keyframes via ffmpeg scene detection.
    3. Detect anchor presence per frame (OpenCV).
    4. Crop anchor frames to keep the background graphic.
    5. Apply style filter for copyright differentiation.
    6. Assign styled frames to chapters based on timeline.
    7. Write ``frames/manifest.json`` + provenance.

    If frame extraction is disabled
    (``settings.frame_extraction_enabled is False``) the stage advances the
    episode status and returns immediately.
    """
    from btcedu.services.ffmpeg_service import (
        apply_style_filter,
        crop_frame,
        extract_keyframes,
    )
    from btcedu.services.frame_extraction_service import (
        NullFrameAnalyzer,
        OpenCVFrameAnalyzer,
    )

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")
    if episode.pipeline_version != 2:
        raise ValueError(
            f"Episode {episode_id} is v1 pipeline (pipeline_version={episode.pipeline_version})."
        )

    allowed = (EpisodeStatus.CHAPTERIZED, EpisodeStatus.FRAMES_EXTRACTED)
    if episode.status not in allowed and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            f"expected one of {[s.value for s in allowed]}. Use --force to override."
        )

    # --- paths -------------------------------------------------------------
    outputs = Path(settings.outputs_dir)
    raw_dir = Path(settings.raw_data_dir) / episode_id
    frames_dir = outputs / episode_id / "frames"
    styled_dir = frames_dir / "styled"
    manifest_path = frames_dir / "manifest.json"
    provenance_path = outputs / episode_id / "provenance" / "frames_provenance.json"
    chapters_path = outputs / episode_id / "chapters.json"

    # --- feature flag ------------------------------------------------------
    if not settings.frame_extraction_enabled:
        logger.info("Frame extraction disabled; advancing status for %s", episode_id)
        if episode.status == EpisodeStatus.CHAPTERIZED:
            episode.status = EpisodeStatus.FRAMES_EXTRACTED
            session.commit()
        return FrameExtractionResult(
            episode_id=episode_id,
            frames_dir=frames_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            skipped=True,
        )

    # --- locate video ------------------------------------------------------
    video_meta_path = raw_dir / "video_meta.json"
    video_path: Path | None = None
    if video_meta_path.exists():
        meta = json.loads(video_meta_path.read_text(encoding="utf-8"))
        vp = Path(meta.get("video_path", ""))
        if vp.exists():
            video_path = vp

    if video_path is None:
        # Try direct lookup
        for ext in ("mp4", "mkv", "webm"):
            candidate = raw_dir / f"video.{ext}"
            if candidate.exists():
                video_path = candidate
                break

    if video_path is None:
        logger.warning("No video file for %s; writing empty manifest", episode_id)
        frames_dir.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(
            json.dumps(
                {
                    "episode_id": episode_id,
                    "schema_version": "1.0",
                    "chapter_assignments": [],
                    "error": "no_video_file",
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        if episode.status == EpisodeStatus.CHAPTERIZED:
            episode.status = EpisodeStatus.FRAMES_EXTRACTED
            episode.error_message = None
            session.commit()
        return FrameExtractionResult(
            episode_id=episode_id,
            frames_dir=frames_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
        )

    # --- idempotency -------------------------------------------------------
    video_hash = _compute_video_hash(video_path)
    chapters_doc = _load_chapters(chapters_path)
    chapters_hash = _compute_chapters_hash(chapters_doc)

    if not force and _is_extraction_current(
        manifest_path, provenance_path, video_hash, chapters_hash
    ):
        logger.info("Frame extraction current for %s (use --force)", episode_id)
        if episode.status == EpisodeStatus.CHAPTERIZED:
            episode.status = EpisodeStatus.FRAMES_EXTRACTED
            session.commit()
        return FrameExtractionResult(
            episode_id=episode_id,
            frames_dir=frames_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            skipped=True,
        )

    # --- pipeline run record -----------------------------------------------
    pipeline_run = PipelineRun(
        episode_id=episode_id,
        stage="frameextract",
        status=RunStatus.RUNNING.value,
        started_at=_utcnow(),
    )
    session.add(pipeline_run)
    session.commit()

    try:
        # --- 1. extract keyframes ------------------------------------------
        raw_frames_dir = str(frames_dir / "raw")
        keyframes = extract_keyframes(
            video_path=str(video_path),
            output_dir=raw_frames_dir,
            scene_threshold=settings.frame_extract_scene_threshold,
            min_interval_seconds=settings.frame_extract_min_interval,
            max_frames=settings.frame_extract_max_frames,
            dry_run=settings.dry_run,
        )
        total_frames = len(keyframes)
        logger.info("Extracted %d keyframes from %s", total_frames, video_path.name)

        # --- 2. anchor detection -------------------------------------------
        if settings.frame_extract_anchor_detection:
            analyzer: OpenCVFrameAnalyzer | NullFrameAnalyzer = OpenCVFrameAnalyzer()
            if not analyzer.available:
                analyzer = NullFrameAnalyzer()
        else:
            analyzer = NullFrameAnalyzer()

        anchor_count = 0
        crop_count = 0

        # --- 3. crop + style per frame -------------------------------------
        styled_dir.mkdir(parents=True, exist_ok=True)
        styled_frames: list[dict] = []

        for kf in keyframes:
            detection = analyzer.detect_anchor(kf.frame_path)
            is_anchor = detection.has_anchor
            if is_anchor:
                anchor_count += 1

            # Crop if anchor detected and cropping enabled
            input_for_style = kf.frame_path
            was_cropped = False
            if is_anchor and settings.frame_extract_crop_anchor:
                crop_region = analyzer.compute_crop_region(detection)
                if crop_region:
                    cropped_path = str(frames_dir / "cropped" / Path(kf.frame_path).name)
                    Path(cropped_path).parent.mkdir(parents=True, exist_ok=True)
                    crop_frame(
                        input_path=kf.frame_path,
                        output_path=cropped_path,
                        crop_region=crop_region,
                        dry_run=settings.dry_run,
                    )
                    input_for_style = cropped_path
                    was_cropped = True
                    crop_count += 1

            # Apply style filter
            styled_path = str(styled_dir / Path(kf.frame_path).name)
            if settings.frame_extract_style_provider == "dalle_edit":
                _apply_dalle_edit_style(input_for_style, styled_path, settings)
            else:
                apply_style_filter(
                    input_path=input_for_style,
                    output_path=styled_path,
                    filter_preset=settings.frame_extract_style_preset,
                    dry_run=settings.dry_run,
                )

            styled_frames.append(
                {
                    "frame_path": styled_path,
                    "original_path": kf.frame_path,
                    "timestamp_seconds": kf.timestamp_seconds,
                    "scene_score": kf.scene_score,
                    "has_anchor": is_anchor,
                    "was_cropped": was_cropped,
                    "style_applied": (
                        "dalle_edit:dall-e-2"
                        if settings.frame_extract_style_provider == "dalle_edit"
                        else f"ffmpeg:{settings.frame_extract_style_preset}"
                    ),
                }
            )

        # --- 4. chapter assignment -----------------------------------------
        timeline = _build_chapter_timeline(chapters_doc)
        chapter_frames: dict[str, list[dict]] = {ch.chapter_id: [] for ch in chapters_doc.chapters}

        for sf in styled_frames:
            cid = _assign_frame_to_chapter(sf["timestamp_seconds"], timeline)
            if cid and cid in chapter_frames:
                chapter_frames[cid].append(sf)

        # Pick best frame per chapter (highest scene score, prefer no anchor)
        chapter_assignments: list[dict] = []
        assigned_count = 0
        for cid, frames_list in chapter_frames.items():
            if not frames_list:
                continue
            # Sort: non-anchor first, then by scene_score desc
            ranked = sorted(
                frames_list,
                key=lambda f: (f["has_anchor"], -f["scene_score"]),
            )
            best = ranked[0]
            alternatives = [f["frame_path"] for f in ranked[1:4]]
            chapter_assignments.append(
                {
                    "chapter_id": cid,
                    "assigned_frame": best["frame_path"],
                    "timestamp_seconds": best["timestamp_seconds"],
                    "scene_score": best["scene_score"],
                    "has_anchor": best["has_anchor"],
                    "was_cropped": best["was_cropped"],
                    "alternative_frames": alternatives,
                }
            )
            assigned_count += 1

        # --- 5. write manifest + provenance --------------------------------
        manifest_data = {
            "episode_id": episode_id,
            "schema_version": "1.0",
            "source_video": str(video_path),
            "extraction_params": {
                "scene_threshold": settings.frame_extract_scene_threshold,
                "style_filter": f"ffmpeg:{settings.frame_extract_style_preset}",
                "anchor_detection": "opencv_haar"
                if settings.frame_extract_anchor_detection
                else "disabled",
            },
            "total_frames_extracted": total_frames,
            "total_frames_with_anchor": anchor_count,
            "chapter_assignments": chapter_assignments,
            "all_frames": [
                {
                    "frame_path": sf["frame_path"],
                    "timestamp_seconds": sf["timestamp_seconds"],
                    "scene_score": sf["scene_score"],
                    "has_anchor": sf["has_anchor"],
                }
                for sf in styled_frames
            ],
        }

        manifest_path.write_text(
            json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_data = {
            "stage": "frameextract",
            "episode_id": episode_id,
            "timestamp": _utcnow().isoformat(),
            "video_hash": video_hash,
            "chapters_hash": chapters_hash,
            "input_files": [str(video_path), str(chapters_path)],
            "output_files": [str(manifest_path)],
            "total_frames": total_frames,
            "anchor_frames": anchor_count,
            "assigned_frames": assigned_count,
            "cost_usd": 0.0,
        }
        provenance_path.write_text(
            json.dumps(provenance_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # --- 6. update status + cascade ------------------------------------
        _mark_downstream_stale(episode_id, outputs)

        episode.status = EpisodeStatus.FRAMES_EXTRACTED
        episode.error_message = None
        session.commit()

        pipeline_run.status = RunStatus.SUCCESS.value
        pipeline_run.completed_at = _utcnow()
        session.commit()

        return FrameExtractionResult(
            episode_id=episode_id,
            frames_dir=frames_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            total_frames=total_frames,
            anchor_frames=anchor_count,
            cropped_frames=crop_count,
            styled_frames=len(styled_frames),
            assigned_frames=assigned_count,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        episode.error_message = str(e)
        session.commit()
        logger.error("Frame extraction failed for %s: %s", episode_id, e)
        raise
