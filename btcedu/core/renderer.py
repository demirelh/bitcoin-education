"""Video rendering: Assemble chapter images + TTS audio + overlays into draft MP4."""

import hashlib
import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError
from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.models.chapter_schema import ChapterDocument
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, RunStatus
from btcedu.models.media_asset import MediaAsset, MediaAssetType

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(UTC)


# Overlay style defaults based on overlay type
OVERLAY_STYLES = {
    "lower_third": {"fontsize": 48, "fontcolor": "white", "position": "bottom_center"},
    "title": {"fontsize": 72, "fontcolor": "white", "position": "center"},
    "quote": {"fontsize": 42, "fontcolor": "white", "position": "center"},
    "statistic": {"fontsize": 56, "fontcolor": "#F7931A", "position": "center"},
}


@dataclass
class RenderSegmentEntry:
    """Metadata for a single rendered segment."""

    chapter_id: str
    image: str  # Relative path from outputs dir (image or video)
    audio: str  # Relative path from outputs dir
    duration_seconds: float
    segment_path: str  # Relative path from outputs dir
    overlays: list[dict]
    transition_in: str
    transition_out: str
    size_bytes: int
    asset_type: str = "photo"  # "photo" or "video" (Phase 4)


@dataclass
class RenderResult:
    """Summary of render operation for one episode."""

    episode_id: str
    render_path: Path
    manifest_path: Path
    provenance_path: Path
    draft_path: Path
    segment_count: int = 0
    total_duration_seconds: float = 0.0
    total_size_bytes: int = 0
    skipped: bool = False


def render_video(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> RenderResult:
    """Render draft video from chapter images, TTS audio, and overlays.

    Args:
        session: SQLAlchemy database session
        episode_id: Episode identifier
        settings: Application configuration
        force: If True, re-render even if current

    Returns:
        RenderResult with paths, counts, duration, size, and skip status

    Raises:
        ValueError: If episode/status invalid or inputs missing
        RuntimeError: If ffmpeg fails
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    # V2 pipeline only
    if episode.pipeline_version != 2:
        raise ValueError(
            f"Episode {episode_id} is v1 pipeline (pipeline_version={episode.pipeline_version}). "
            "Render is only supported for v2 pipeline."
        )

    # Check episode status (allow TTS_DONE or RENDERED for idempotency)
    if episode.status not in (EpisodeStatus.TTS_DONE, EpisodeStatus.RENDERED) and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'tts_done' or 'rendered'. Use --force to override."
        )

    # Load profile for profile-aware rendering (accent color etc.)
    try:
        from btcedu.profiles import get_registry as _get_profile_registry

        _profile_name = getattr(episode, "content_profile", "bitcoin_podcast") or "bitcoin_podcast"
        _profile = _get_profile_registry(settings).get(_profile_name)
        _accent_color = (
            _profile.stage_config.get("render", {}).get("accent_color") or "#F7931A"
            if _profile
            else "#F7931A"
        )
    except Exception:
        _accent_color = "#F7931A"

    # Resolve paths
    chapters_path = Path(settings.outputs_dir) / episode_id / "chapters.json"
    image_manifest_path = Path(settings.outputs_dir) / episode_id / "images" / "manifest.json"
    tts_manifest_path = Path(settings.outputs_dir) / episode_id / "tts" / "manifest.json"

    if not chapters_path.exists():
        raise FileNotFoundError(f"Chapters file not found: {chapters_path}")
    if not image_manifest_path.exists():
        raise FileNotFoundError(f"Image manifest not found: {image_manifest_path}")
    if not tts_manifest_path.exists():
        raise FileNotFoundError(f"TTS manifest not found: {tts_manifest_path}")

    render_dir = Path(settings.outputs_dir) / episode_id / "render"
    segments_dir = render_dir / "segments"
    manifest_path = render_dir / "render_manifest.json"
    draft_path = render_dir / "draft.mp4"
    provenance_path = (
        Path(settings.outputs_dir) / episode_id / "provenance" / "render_provenance.json"
    )

    # Load inputs
    chapters_doc = _load_chapters(chapters_path)
    image_manifest = _load_image_manifest(image_manifest_path)
    tts_manifest = _load_tts_manifest(tts_manifest_path)

    # Compute content hash (for idempotency) — includes enhancement flags
    _enh_hash_data = {}
    try:
        _enh_hash_data = {
            "ken_burns": bool(settings.render_ken_burns_enabled),
            "lower_thirds_animated": bool(settings.render_lower_thirds_animated),
            "ticker": bool(settings.render_ticker_enabled),
            "intro": bool(settings.render_intro_enabled),
            "outro": bool(settings.render_outro_enabled),
            "color_correction": bool(settings.render_color_correction_enabled),
        }
    except (AttributeError, TypeError):
        pass  # settings may lack these attrs (backward compat / mocks)
    content_hash = _compute_render_content_hash(
        chapters_doc, image_manifest, tts_manifest,
        _enh_hash_data if _enh_hash_data else None,
    )

    # Idempotency check
    if not force:
        if _is_render_current(manifest_path, provenance_path, draft_path, content_hash):
            logger.info("Render is current for %s (use --force to re-render)", episode_id)
            # Still advance episode status so pipeline can proceed
            if episode.status == EpisodeStatus.TTS_DONE:
                episode.status = EpisodeStatus.RENDERED
                session.commit()
            existing_provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            return RenderResult(
                episode_id=episode_id,
                render_path=render_dir,
                manifest_path=manifest_path,
                provenance_path=provenance_path,
                draft_path=draft_path,
                segment_count=existing_provenance.get("segment_count", 0),
                total_duration_seconds=existing_provenance.get("total_duration_seconds", 0.0),
                total_size_bytes=existing_provenance.get("total_size_bytes", 0),
                skipped=True,
            )

    # Create PipelineRun record
    pipeline_run = PipelineRun(
        episode_id=episode_id,
        stage="render",
        status=RunStatus.RUNNING.value,
        started_at=_utcnow(),
    )
    session.add(pipeline_run)
    session.commit()

    try:
        # Import ffmpeg service (lazy to avoid issues if ffmpeg not installed)
        from btcedu.services.ffmpeg_service import (
            KEN_BURNS_PATTERNS,
            SegmentResult,
            concatenate_segments,
            create_intro_segment,
            create_outro_segment,
            create_segment,
            create_video_segment,
            get_ffmpeg_version,
            probe_media,
        )

        ffmpeg_version = get_ffmpeg_version()
        logger.info("Using %s", ffmpeg_version)

        # Create render directories
        segments_dir.mkdir(parents=True, exist_ok=True)

        # Render each chapter segment
        segment_entries: list[RenderSegmentEntry] = []
        total_duration = 0.0
        total_size = 0

        base_dir = Path(settings.outputs_dir) / episode_id

        # Build ticker text if enabled
        _ticker_text = None
        if getattr(settings, "render_ticker_enabled", False) is True:
            _ticker_items = [ch.title for ch in chapters_doc.chapters]
            _ticker_text = "  |||  ".join(_ticker_items)

        # Enhancement kwargs shared between photo and video segments
        def _enhancement_kwargs(
            asset_type: str, chapter_idx: int,
        ) -> dict:
            kwargs: dict = {}
            # Color correction (both photo and video)
            if getattr(settings, "render_color_correction_enabled", False) is True:
                kwargs["color_correction"] = True
                kwargs["color_saturation"] = settings.render_color_saturation
                kwargs["color_brightness"] = settings.render_color_brightness
                kwargs["color_blue_shift"] = settings.render_color_blue_shift
            # Animated lower thirds (both)
            if getattr(settings, "render_lower_thirds_animated", False) is True:
                kwargs["animated_lower_thirds"] = True
                kwargs["lower_third_slide_duration"] = (
                    settings.render_lower_thirds_slide_duration
                )
                kwargs["lower_third_accent_color"] = _accent_color
            # Ticker (both)
            if _ticker_text:
                kwargs["ticker_text"] = _ticker_text
                kwargs["ticker_speed"] = settings.render_ticker_speed
                kwargs["ticker_height"] = settings.render_ticker_height
                kwargs["ticker_fontsize"] = settings.render_ticker_fontsize
            # Ken Burns (photo only)
            if (
                getattr(settings, "render_ken_burns_enabled", False) is True
                and asset_type == "photo"
            ):
                pattern = KEN_BURNS_PATTERNS[
                    chapter_idx % len(KEN_BURNS_PATTERNS)
                ]
                kwargs["ken_burns_pattern"] = pattern
                kwargs["ken_burns_zoom_ratio"] = (
                    settings.render_ken_burns_zoom_ratio
                )
            return kwargs

        for chapter in chapters_doc.chapters:
            # Resolve media files for this chapter
            try:
                media_path, audio_path, duration, asset_type = _resolve_chapter_media(
                    chapter.chapter_id, image_manifest, tts_manifest, base_dir
                )
            except ValueError as e:
                logger.warning("Skipping chapter %s: %s", chapter.chapter_id, e)
                continue

            # Convert overlays to OverlaySpec (profile-aware accent color)
            overlay_specs = _chapter_to_overlay_specs(
                chapter, settings.render_font, accent_color=_accent_color
            )

            # Compute fade durations based on transition types (Sprint 10)
            fade_in_dur = 0.0
            fade_out_dur = 0.0
            if chapter.transitions.in_transition.value in ("fade", "dissolve"):
                fade_in_dur = settings.render_transition_duration
            if chapter.transitions.out_transition.value in ("fade", "dissolve"):
                fade_out_dur = settings.render_transition_duration

            # Render segment
            segment_filename = f"{chapter.chapter_id}.mp4"
            segment_path = segments_dir / segment_filename
            segment_rel_path = f"render/segments/{segment_filename}"

            # Skip segment if it already exists and is valid (idempotency guard)
            if segment_path.exists() and segment_path.stat().st_size > 0:
                try:
                    probe_media(str(segment_path))
                    logger.info(
                        "Segment %s already exists and is valid, skipping re-render",
                        chapter.chapter_id,
                    )
                    segment_result = SegmentResult(
                        segment_path=str(segment_path),
                        duration_seconds=duration,
                        size_bytes=segment_path.stat().st_size,
                        ffmpeg_command=[],
                        returncode=0,
                        stderr="[skipped-existing]",
                    )
                    segment_rel_path = f"render/segments/{segment_filename}"
                    entry = RenderSegmentEntry(
                        chapter_id=chapter.chapter_id,
                        image=_find_image_rel_path(chapter.chapter_id, image_manifest),
                        audio=_find_audio_rel_path(chapter.chapter_id, tts_manifest),
                        segment_path=segment_rel_path,
                        duration_seconds=duration,
                        overlays=[],
                        transition_in=chapter.transitions.in_transition.value,
                        transition_out=chapter.transitions.out_transition.value,
                        size_bytes=segment_result.size_bytes,
                        asset_type=asset_type,
                    )
                    segment_entries.append(entry)
                    total_duration += duration
                    total_size += segment_result.size_bytes
                    continue
                except Exception:
                    logger.warning(
                        "Segment %s exists but is invalid, re-rendering", chapter.chapter_id
                    )
                    segment_path.unlink(missing_ok=True)

            logger.info(
                "Rendering segment %s (%d/%d): %.1fs, %d overlays, "
                "fade_in=%.1fs, fade_out=%.1fs, asset_type=%s",
                chapter.chapter_id,
                chapter.order,
                chapters_doc.total_chapters,
                duration,
                len(overlay_specs),
                fade_in_dur,
                fade_out_dur,
                asset_type,
            )

            # Build enhancement kwargs for this chapter
            enh_kwargs = _enhancement_kwargs(asset_type, chapter.order - 1)

            # Phase 4: Branch on asset_type for video vs image segments
            if asset_type == "video":
                segment_result = create_video_segment(
                    video_path=str(media_path),
                    audio_path=str(audio_path),
                    output_path=str(segment_path),
                    duration=duration,
                    overlays=overlay_specs,
                    resolution=settings.render_resolution,
                    fps=settings.render_fps,
                    crf=settings.render_crf,
                    preset=settings.render_preset,
                    audio_bitrate=settings.render_audio_bitrate,
                    font=settings.render_font,
                    fade_in_duration=fade_in_dur,
                    fade_out_duration=fade_out_dur,
                    timeout_seconds=settings.render_timeout_segment,
                    dry_run=settings.dry_run,
                    **enh_kwargs,
                )
            else:
                segment_result = create_segment(
                    image_path=str(media_path),
                    audio_path=str(audio_path),
                    output_path=str(segment_path),
                    duration=duration,
                    overlays=overlay_specs,
                    resolution=settings.render_resolution,
                    fps=settings.render_fps,
                    crf=settings.render_crf,
                    preset=settings.render_preset,
                    audio_bitrate=settings.render_audio_bitrate,
                    font=settings.render_font,
                    fade_in_duration=fade_in_dur,
                    fade_out_duration=fade_out_dur,
                    timeout_seconds=settings.render_timeout_segment,
                    dry_run=settings.dry_run,
                    **enh_kwargs,
                )

            # Record segment entry
            # Find image/video and audio relative paths from manifests
            image_rel = _find_image_rel_path(chapter.chapter_id, image_manifest)
            audio_rel = _find_audio_rel_path(chapter.chapter_id, tts_manifest)

            entry = RenderSegmentEntry(
                chapter_id=chapter.chapter_id,
                image=image_rel,
                audio=audio_rel,
                duration_seconds=duration,
                segment_path=segment_rel_path,
                overlays=[
                    {
                        "type": spec.overlay_type,
                        "text": spec.text,
                        "font": spec.font,
                        "fontsize": spec.fontsize,
                        "fontcolor": spec.fontcolor,
                        "position": spec.position,
                        "start": spec.start,
                        "end": spec.end,
                    }
                    for spec in overlay_specs
                ],
                transition_in=chapter.transitions.in_transition.value,
                transition_out=chapter.transitions.out_transition.value,
                size_bytes=segment_result.size_bytes,
                asset_type=asset_type,  # Phase 4
            )
            segment_entries.append(entry)
            total_duration += duration
            total_size += segment_result.size_bytes

        # Concatenate segments
        if not segment_entries:
            raise RuntimeError("No segments were rendered")

        segment_abs_paths = [
            str((base_dir / entry.segment_path).absolute()) for entry in segment_entries
        ]

        # Prepend intro if enabled
        if getattr(settings, "render_intro_enabled", False) is True:
            intro_path = segments_dir / "intro.mp4"
            _ep_date = ""
            if hasattr(episode, "published_at") and episode.published_at:
                _ep_date = episode.published_at.strftime("%d.%m.%Y")
            _ep_title = getattr(episode, "title", "") or episode_id
            create_intro_segment(
                output_path=str(intro_path),
                show_name=settings.render_intro_show_name,
                episode_title=_ep_title,
                episode_date=_ep_date,
                duration=settings.render_intro_duration,
                resolution=settings.render_resolution,
                fps=settings.render_fps,
                bg_color=settings.render_intro_bg_color,
                accent_color=_accent_color,
                font=settings.render_font,
                crf=settings.render_crf,
                preset=settings.render_preset,
                timeout_seconds=settings.render_timeout_segment,
                dry_run=settings.dry_run,
            )
            segment_abs_paths.insert(0, str(intro_path.absolute()))
            total_duration += settings.render_intro_duration
            logger.info("Intro segment created (%.1fs)", settings.render_intro_duration)

        # Append outro if enabled
        if getattr(settings, "render_outro_enabled", False) is True:
            outro_path = segments_dir / "outro.mp4"
            create_outro_segment(
                output_path=str(outro_path),
                source_text=settings.render_outro_text,
                duration=settings.render_outro_duration,
                resolution=settings.render_resolution,
                fps=settings.render_fps,
                bg_color=settings.render_outro_bg_color,
                accent_color=_accent_color,
                font=settings.render_font,
                crf=settings.render_crf,
                preset=settings.render_preset,
                timeout_seconds=settings.render_timeout_segment,
                dry_run=settings.dry_run,
            )
            segment_abs_paths.append(str(outro_path.absolute()))
            total_duration += settings.render_outro_duration
            logger.info("Outro segment created (%.1fs)", settings.render_outro_duration)

        logger.info("Concatenating %d segments into draft video", len(segment_abs_paths))

        concat_result = concatenate_segments(
            segment_paths=segment_abs_paths,
            output_path=str(draft_path),
            timeout_seconds=settings.render_timeout_concat,
            dry_run=settings.dry_run,
        )

        total_size += concat_result.size_bytes

        # Write render manifest
        manifest_data = {
            "episode_id": episode_id,
            "schema_version": "1.0",
            "resolution": settings.render_resolution,
            "fps": settings.render_fps,
            "generated_at": _utcnow().isoformat(),
            "total_duration_seconds": total_duration,
            "total_size_bytes": total_size,
            "transition_duration": settings.render_transition_duration,  # Sprint 10
            "segments": [asdict(entry) for entry in segment_entries],
            "output_path": "render/draft.mp4",
            "ffmpeg_version": ffmpeg_version,
            "codec": {
                "video": "libx264",
                "audio": "aac",
                "preset": settings.render_preset,
                "crf": settings.render_crf,
                "audio_bitrate": settings.render_audio_bitrate,
            },
        }
        manifest_path.write_text(
            json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Write provenance
        provenance_data = {
            "stage": "render",
            "episode_id": episode_id,
            "timestamp": _utcnow().isoformat(),
            "model": "ffmpeg",
            "ffmpeg_version": ffmpeg_version,
            "input_files": [str(chapters_path), str(image_manifest_path), str(tts_manifest_path)],
            "input_content_hash": content_hash,
            "output_files": [str(manifest_path), str(draft_path)],
            "segment_count": len(segment_entries),
            "total_duration_seconds": total_duration,
            "total_size_bytes": total_size,
            "cost_usd": 0.0,  # Render is local, no API cost
        }
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(provenance_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Create ContentArtifact record
        artifact = ContentArtifact(
            episode_id=episode_id,
            artifact_type="render",
            file_path=str(manifest_path.relative_to(base_dir)),
            prompt_hash=content_hash,
            model="ffmpeg",
            created_at=_utcnow(),
        )
        session.add(artifact)

        # Create MediaAsset record for draft video
        if not settings.dry_run:
            _create_media_asset_record(session, episode_id, draft_path, total_duration, total_size)

        # Update episode status
        episode.status = EpisodeStatus.RENDERED
        episode.error_message = None
        session.commit()

        # Update PipelineRun
        pipeline_run.status = RunStatus.SUCCESS.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.estimated_cost_usd = 0.0  # No API cost
        session.commit()

        logger.info(
            "Render complete for %s: %d segments, %.1fs, %d bytes",
            episode_id,
            len(segment_entries),
            total_duration,
            total_size,
        )

        return RenderResult(
            episode_id=episode_id,
            render_path=render_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            draft_path=draft_path,
            segment_count=len(segment_entries),
            total_duration_seconds=total_duration,
            total_size_bytes=total_size,
            skipped=False,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        episode.error_message = str(e)
        session.commit()
        logger.error("Render failed for %s: %s", episode_id, e)
        raise


def _load_chapters(chapters_path: Path) -> ChapterDocument:
    """Load and validate chapter JSON."""
    try:
        chapters_data = json.loads(chapters_path.read_text(encoding="utf-8"))
        return ChapterDocument(**chapters_data)
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValueError(f"Invalid chapters.json at {chapters_path}: {e}") from e


def _load_image_manifest(manifest_path: Path) -> dict:
    """Load image manifest JSON."""
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid image manifest at {manifest_path}: {e}") from e


def _load_tts_manifest(manifest_path: Path) -> dict:
    """Load TTS manifest JSON."""
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid TTS manifest at {manifest_path}: {e}") from e


def _compute_render_content_hash(
    chapters_doc: ChapterDocument,
    image_manifest: dict,
    tts_manifest: dict,
    enhancement_settings: dict | None = None,
) -> str:
    """Compute SHA-256 hash of all render inputs.

    Includes:
    - Chapter overlay text and timing
    - Image file paths and generation methods
    - TTS file paths and durations
    - Video enhancement settings (if any enabled)
    """
    relevant_data = {
        "chapters": [
            {
                "chapter_id": ch.chapter_id,
                "overlays": [
                    {
                        "type": ov.type.value,
                        "text": ov.text,
                        "start": ov.start_offset_seconds,
                        "duration": ov.duration_seconds,
                    }
                    for ov in ch.overlays
                ],
                "transitions": {
                    "in": ch.transitions.in_transition.value,
                    "out": ch.transitions.out_transition.value,
                },
            }
            for ch in chapters_doc.chapters
        ],
        "images": [
            {
                "chapter_id": img["chapter_id"],
                "file_path": img["file_path"],
                "generation_method": img.get("generation_method", "unknown"),
                "asset_type": img.get("asset_type", "photo"),  # Phase 4
            }
            for img in image_manifest.get("images", [])
        ],
        "tts": [
            {
                "chapter_id": seg["chapter_id"],
                "file_path": seg["file_path"],
                "duration_seconds": seg["duration_seconds"],
            }
            for seg in tts_manifest.get("segments", [])
        ],
    }
    if enhancement_settings:
        relevant_data["enhancements"] = enhancement_settings
    content_str = json.dumps(relevant_data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content_str.encode("utf-8")).hexdigest()


def _is_render_current(
    manifest_path: Path,
    provenance_path: Path,
    draft_path: Path,
    content_hash: str,
) -> bool:
    """Check if render output is current (idempotency)."""
    if not manifest_path.exists() or not provenance_path.exists() or not draft_path.exists():
        return False

    # Check for .stale marker
    stale_marker = draft_path.with_suffix(".mp4.stale")
    if stale_marker.exists():
        logger.info("Draft video marked as stale")
        return False

    # Check provenance hash
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        if provenance.get("input_content_hash") != content_hash:
            logger.info("Render input content has changed")
            return False
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Could not verify render provenance: %s", e)
        return False

    return True


def _chapter_to_overlay_specs(chapter, font: str, accent_color: str = "#F7931A") -> list:
    """Convert chapter overlays to OverlaySpec list.

    Args:
        chapter: Chapter object
        font: Font name
        accent_color: Hex color for statistic overlays (profile-specific)

    Returns:
        List of OverlaySpec objects
    """
    from btcedu.services.ffmpeg_service import OverlaySpec

    overlay_specs = []
    for overlay in chapter.overlays:
        # Get style defaults
        style = dict(OVERLAY_STYLES.get(overlay.type.value, OVERLAY_STYLES["lower_third"]))

        # Apply profile-specific accent color to statistic overlays
        if overlay.type.value == "statistic":
            style = dict(style)
            style["fontcolor"] = accent_color

        spec = OverlaySpec(
            text=overlay.text,
            overlay_type=overlay.type.value,
            fontsize=style["fontsize"],
            fontcolor=style["fontcolor"],
            font=font,
            position=style["position"],
            start=overlay.start_offset_seconds,
            end=overlay.start_offset_seconds + overlay.duration_seconds,
        )
        overlay_specs.append(spec)

    return overlay_specs


def _resolve_chapter_media(
    chapter_id: str,
    image_manifest: dict,
    tts_manifest: dict,
    base_dir: Path,
) -> tuple[Path, Path, float, str]:
    """Resolve media, audio paths and duration for a chapter.

    Args:
        chapter_id: Chapter identifier
        image_manifest: Image manifest dict
        tts_manifest: TTS manifest dict
        base_dir: Episode outputs directory

    Returns:
        Tuple of (media_path, audio_path, duration_seconds, asset_type)
        asset_type is "photo" or "video" (Phase 4)

    Raises:
        ValueError: If image/video or audio not found
    """
    # Find image/video entry
    image_entry = None
    for img in image_manifest.get("images", []):
        if img["chapter_id"] == chapter_id:
            image_entry = img
            break

    if not image_entry:
        raise ValueError(f"No image found for chapter {chapter_id}")

    media_path = base_dir / image_entry["file_path"]
    if not media_path.exists():
        raise ValueError(f"Media file not found: {media_path}")

    # Phase 4: Read asset_type (default "photo" for backward compat)
    asset_type = image_entry.get("asset_type", "photo")

    # Find audio
    audio_entry = None
    for seg in tts_manifest.get("segments", []):
        if seg["chapter_id"] == chapter_id:
            audio_entry = seg
            break

    if not audio_entry:
        raise ValueError(f"No audio found for chapter {chapter_id}")

    audio_path = base_dir / audio_entry["file_path"]
    if not audio_path.exists():
        raise ValueError(f"Audio file not found: {audio_path}")

    duration = audio_entry["duration_seconds"]

    return media_path, audio_path, duration, asset_type


def _find_image_rel_path(chapter_id: str, image_manifest: dict) -> str:
    """Find relative image path for a chapter."""
    for img in image_manifest.get("images", []):
        if img["chapter_id"] == chapter_id:
            return img["file_path"]
    return f"images/{chapter_id}_missing.png"


def _find_audio_rel_path(chapter_id: str, tts_manifest: dict) -> str:
    """Find relative audio path for a chapter."""
    for seg in tts_manifest.get("segments", []):
        if seg["chapter_id"] == chapter_id:
            return seg["file_path"]
    return f"tts/{chapter_id}_missing.mp3"


def _create_media_asset_record(
    session: Session,
    episode_id: str,
    draft_path: Path,
    duration_seconds: float,
    size_bytes: int,
) -> None:
    """Create MediaAsset database record for draft video."""
    media_asset = MediaAsset(
        episode_id=episode_id,
        asset_type=MediaAssetType.VIDEO,
        file_path=str(draft_path.relative_to(Path(draft_path.parent.parent.parent))),
        mime_type="video/mp4",
        size_bytes=size_bytes,
        duration_seconds=duration_seconds,
        meta=json.dumps({"codec": "h264", "audio_codec": "aac"}, ensure_ascii=False),
        created_at=_utcnow(),
    )
    session.add(media_asset)
