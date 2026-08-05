"""Video rendering: Assemble chapter images + TTS audio + overlays into draft MP4."""

import hashlib
import json
import logging
from collections.abc import Callable
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


def _write_render_progress(render_dir: Path, payload: dict) -> None:
    """Persist render progress to ``render/progress.json`` atomically.

    Written on every progress event regardless of how render was triggered
    (pipeline autostart or web job), so the dashboard can show live progress
    even when no browser initiated the run.
    """
    try:
        render_dir.mkdir(parents=True, exist_ok=True)
        data = dict(payload)
        data["updated_at"] = _utcnow().isoformat()
        tmp = render_dir / "progress.json.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(render_dir / "progress.json")
    except Exception:
        # Progress reporting is best-effort; never let it break rendering.
        pass


# Overlay style defaults based on overlay type
OVERLAY_STYLES = {
    "lower_third": {"fontsize": 48, "fontcolor": "white", "position": "bottom_center"},
    # Used when an overlay carries both a headline and a summary line.
    "lower_third_headline": {
        "fontsize": 52,
        "fontcolor": "white",
        "position": "lower_third_headline",
    },
    "lower_third_subtext": {
        "fontsize": 34,
        "fontcolor": "#E8EEF5",
        "position": "lower_third_subtext",
    },
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
    progress_callback: "Callable[[dict], None] | None" = None,
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
    if (
        episode.status
        not in (EpisodeStatus.TTS_DONE, EpisodeStatus.ANCHOR_GENERATED, EpisodeStatus.RENDERED)
        and not force
    ):
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'tts_done' or 'rendered'. Use --force to override."
        )

    # Load profile for profile-aware rendering (accent color, feature toggles etc.)
    _render_cfg: dict = {}
    _weather_cfg: dict = {}
    _profile_name = getattr(episode, "content_profile", "bitcoin_podcast") or "bitcoin_podcast"
    try:
        from btcedu.profiles import get_registry as _get_profile_registry

        _profile = _get_profile_registry(settings).get(_profile_name)
        _render_cfg = (_profile.stage_config.get("render", {}) if _profile else {}) or {}
        _weather_cfg = (_profile.stage_config.get("weather", {}) if _profile else {}) or {}
        _accent_color = _render_cfg.get("accent_color") or "#F7931A"
    except Exception:
        _profile = None
        _render_cfg = {}
        _weather_cfg = {}
        _accent_color = "#F7931A"

    # Helper to prefer profile override, else fall back to global setting.
    def _rc(key: str, default):
        if key in _render_cfg and _render_cfg[key] is not None:
            return _render_cfg[key]
        return default

    # Resolve effective render feature flags (profile overrides globals)
    _eff_ken_burns = bool(
        _rc("ken_burns_enabled", getattr(settings, "render_ken_burns_enabled", False))
    )
    _eff_lower_thirds = bool(
        _rc("lower_thirds_animated", getattr(settings, "render_lower_thirds_animated", False))
    )
    _eff_ticker = bool(_rc("ticker_enabled", getattr(settings, "render_ticker_enabled", False)))
    _eff_intro_show_name = str(
        _rc("intro_show_name", getattr(settings, "render_intro_show_name", ""))
    )
    _eff_intro_enabled = bool(
        _rc("intro_enabled", getattr(settings, "render_intro_enabled", False))
    )
    _eff_intro_audio = str(_rc("intro_audio", "") or "")
    _eff_intro_slogan = str(_rc("intro_slogan", "") or "")
    _eff_intro_episode_title = str(_rc("intro_episode_title", "") or "")
    _eff_intro_duration = float(_rc("intro_duration", settings.render_intro_duration))
    _eff_topic_intro_enabled = bool(_rc("topic_intro_enabled", False))
    _eff_topic_intro_duration = float(_rc("topic_intro_duration", 2.4))
    _eff_topic_intro_label = str(_rc("topic_intro_label", "GÜNDEM") or "GÜNDEM")
    _eff_topic_intro_audio = str(_rc("topic_intro_audio", _eff_intro_audio) or "")
    # The closing card uses the same sting as the intro unless a profile names
    # its own, so the programme never ends on a silent picture.
    _eff_outro_audio = str(_rc("outro_audio", _eff_intro_audio) or "")
    _eff_outro_text = str(_rc("outro_text", getattr(settings, "render_outro_text", "")))
    _eff_font = str(_rc("font", getattr(settings, "render_font", "")))
    _eff_music_bed = str(_rc("music_bed", getattr(settings, "render_music_bed", "")))

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
    _apply_branding_guard(settings, episode_id, _profile_name, chapters_doc, _render_cfg)
    image_manifest = _load_image_manifest(image_manifest_path)
    tts_manifest = _load_tts_manifest(tts_manifest_path)

    # Load anchor manifest (optional — only if anchor generation was enabled)
    anchor_manifest_path = Path(settings.outputs_dir) / episode_id / "anchor" / "manifest.json"
    anchor_manifest: dict = {}
    if anchor_manifest_path.exists():
        try:
            anchor_manifest = json.loads(anchor_manifest_path.read_text(encoding="utf-8"))
            logger.info(
                "Loaded anchor manifest with %d segments",
                len(anchor_manifest.get("segments", [])),
            )
        except (json.JSONDecodeError, KeyError):
            logger.warning("Could not load anchor manifest, using images for all chapters")

    # Compute content hash (for idempotency) — includes enhancement flags
    _enh_hash_data = {}
    _intro_audio_bytes: bytes | None = None
    if _eff_intro_audio and Path(_eff_intro_audio).exists():
        _intro_audio_bytes = Path(_eff_intro_audio).read_bytes()
    _topic_intro_audio_bytes: bytes | None = None
    if _eff_topic_intro_audio and Path(_eff_topic_intro_audio).exists():
        _topic_intro_audio_bytes = Path(_eff_topic_intro_audio).read_bytes()
    _outro_audio_bytes: bytes | None = None
    if _eff_outro_audio and Path(_eff_outro_audio).exists():
        _outro_audio_bytes = Path(_eff_outro_audio).read_bytes()
    try:
        _enh_hash_data = {
            "ken_burns": _eff_ken_burns,
            "lower_thirds_animated": _eff_lower_thirds,
            "ticker": _eff_ticker,
            "intro": _eff_intro_enabled,
            "intro_audio": _eff_intro_audio,
            "intro_audio_sha256": (
                hashlib.sha256(_intro_audio_bytes).hexdigest()
                if _intro_audio_bytes is not None
                else None
            ),
            "intro_slogan": _eff_intro_slogan,
            "intro_episode_title": _eff_intro_episode_title,
            "intro_duration": _eff_intro_duration,
            "topic_intro_enabled": _eff_topic_intro_enabled,
            "topic_intro_duration": _eff_topic_intro_duration,
            "topic_intro_label": _eff_topic_intro_label,
            "topic_intro_audio": _eff_topic_intro_audio,
            "topic_intro_audio_sha256": (
                hashlib.sha256(_topic_intro_audio_bytes).hexdigest()
                if _topic_intro_audio_bytes is not None
                else None
            ),
            "outro": bool(settings.render_outro_enabled),
            "outro_audio": _eff_outro_audio,
            "outro_audio_sha256": (
                hashlib.sha256(_outro_audio_bytes).hexdigest()
                if _outro_audio_bytes is not None
                else None
            ),
            "color_correction": bool(settings.render_color_correction_enabled),
            "font": _eff_font,
            "music_bed": _eff_music_bed,
            "intro_show_name": _eff_intro_show_name,
            "outro_text": _eff_outro_text,
            "accent_color": _accent_color,
            "resolution": settings.render_resolution,
            "fps": settings.render_fps,
            "crf": settings.render_crf,
            "preset": settings.render_preset,
            "audio_bitrate": settings.render_audio_bitrate,
            "transition_duration": settings.render_transition_duration,
            "ken_burns_zoom_ratio": settings.render_ken_burns_zoom_ratio,
            "lower_third_slide_duration": settings.render_lower_thirds_slide_duration,
            "ticker_speed": settings.render_ticker_speed,
            "ticker_height": settings.render_ticker_height,
            "ticker_fontsize": settings.render_ticker_fontsize,
            "intro_bg_color": settings.render_intro_bg_color,
            "outro_duration": settings.render_outro_duration,
            "outro_bg_color": settings.render_outro_bg_color,
            "color_saturation": settings.render_color_saturation,
            "color_brightness": settings.render_color_brightness,
            "color_blue_shift": settings.render_color_blue_shift,
            "episode_title": episode.title,
            "episode_published_at": str(episode.published_at),
            "weather": _weather_cfg,
        }
    except (AttributeError, TypeError):
        pass  # settings may lack these attrs (backward compat / mocks)
    content_hash = _compute_render_content_hash(
        chapters_doc,
        image_manifest,
        tts_manifest,
        _enh_hash_data if _enh_hash_data else None,
    )

    # Idempotency check
    if not force:
        if _is_render_current(manifest_path, provenance_path, draft_path, content_hash):
            logger.info("Render is current for %s (use --force to re-render)", episode_id)
            # Still advance episode status so pipeline can proceed
            if episode.status in (EpisodeStatus.TTS_DONE, EpisodeStatus.ANCHOR_GENERATED):
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
            create_topic_intro_segment,
            create_video_segment,
            get_ffmpeg_version,
            probe_media,
        )

        ffmpeg_version = get_ffmpeg_version()
        logger.info("Using %s", ffmpeg_version)

        # Create render directories
        segments_dir.mkdir(parents=True, exist_ok=True)
        _intro_audio_snapshot: Path | None = None
        if _intro_audio_bytes is not None:
            inputs_dir = render_dir / "inputs"
            inputs_dir.mkdir(parents=True, exist_ok=True)
            _intro_audio_snapshot = inputs_dir / "intro.mp3"
            _intro_audio_snapshot.write_bytes(_intro_audio_bytes)
        _topic_intro_audio_snapshot: Path | None = None
        if _topic_intro_audio_bytes is not None:
            inputs_dir = render_dir / "inputs"
            inputs_dir.mkdir(parents=True, exist_ok=True)
            _topic_intro_audio_snapshot = inputs_dir / "topic_intro.mp3"
            _topic_intro_audio_snapshot.write_bytes(_topic_intro_audio_bytes)

        # Render each chapter segment
        segment_entries: list[RenderSegmentEntry] = []
        total_duration = 0.0
        total_size = 0

        base_dir = Path(settings.outputs_dir) / episode_id

        # Build ticker text if enabled
        _ticker_text = None
        if _eff_ticker:
            _ticker_items = [ch.title for ch in chapters_doc.chapters]
            _ticker_text = "  |||  ".join(_ticker_items)

        # Enhancement kwargs shared between photo and video segments
        def _enhancement_kwargs(
            asset_type: str,
            chapter_idx: int,
        ) -> dict:
            kwargs: dict = {}
            # Color correction (both photo and video)
            if getattr(settings, "render_color_correction_enabled", False) is True:
                kwargs["color_correction"] = True
                kwargs["color_saturation"] = settings.render_color_saturation
                kwargs["color_brightness"] = settings.render_color_brightness
                kwargs["color_blue_shift"] = settings.render_color_blue_shift
            # Animated lower thirds (both)
            if _eff_lower_thirds:
                kwargs["animated_lower_thirds"] = True
                kwargs["lower_third_slide_duration"] = settings.render_lower_thirds_slide_duration
                kwargs["lower_third_accent_color"] = _accent_color
            # Ticker (both)
            if _ticker_text:
                kwargs["ticker_text"] = _ticker_text
                kwargs["ticker_speed"] = settings.render_ticker_speed
                kwargs["ticker_height"] = settings.render_ticker_height
                kwargs["ticker_fontsize"] = settings.render_ticker_fontsize
            # Ken Burns (photo only)
            if _eff_ken_burns and asset_type == "photo":
                pattern = KEN_BURNS_PATTERNS[chapter_idx % len(KEN_BURNS_PATTERNS)]
                kwargs["ken_burns_pattern"] = pattern
                kwargs["ken_burns_zoom_ratio"] = settings.render_ken_burns_zoom_ratio
            return kwargs

        _chapters_total = len(chapters_doc.chapters)

        def _emit(evt: dict) -> None:
            _write_render_progress(render_dir, evt)
            if progress_callback is not None:
                try:
                    progress_callback(evt)
                except Exception:
                    pass

        for _ch_idx, chapter in enumerate(chapters_doc.chapters, start=1):
            # Notify progress at start of each chapter
            _emit(
                {
                    "stage": "segment_start",
                    "current": _ch_idx,
                    "total": _chapters_total,
                    "chapter_id": chapter.chapter_id,
                    "chapter_title": getattr(chapter, "title", "") or "",
                    "progress_pct": int(100 * (_ch_idx - 1) / max(_chapters_total, 1)),
                }
            )
            # Resolve media files for this chapter
            try:
                media_path, audio_path, duration, asset_type = _resolve_chapter_media(
                    chapter.chapter_id,
                    image_manifest,
                    tts_manifest,
                    base_dir,
                    anchor_manifest=anchor_manifest,
                )
            except ValueError as e:
                raise ValueError(
                    f"Cannot render complete episode: chapter {chapter.chapter_id} "
                    f"has unresolved media: {e}"
                ) from e

            image_entry = next(
                (
                    entry
                    for entry in image_manifest.get("images", [])
                    if entry.get("chapter_id") == chapter.chapter_id
                ),
                None,
            )
            if (
                image_entry
                and (image_entry.get("metadata") or {}).get("category") == "weather"
                and (_weather_cfg.get("rendering", {}) or {}).get("animation_level", "subtle")
                != "none"
            ):
                from btcedu.core.weather.models import WeatherData
                from btcedu.core.weather.renderer import render_weather_scene_video
                from btcedu.core.weather.scene_planner import plan_weather_scenes

                weather_data_path = base_dir / (image_entry.get("metadata") or {}).get(
                    "weather_data_path",
                    f"images/{chapter.chapter_id}_weather.json",
                )
                if weather_data_path.exists():
                    weather_data = WeatherData.model_validate_json(
                        weather_data_path.read_text(encoding="utf-8")
                    )
                    actual_scene_plan = plan_weather_scenes(
                        weather_data,
                        duration,
                        story_id=chapter.chapter_id,
                    )
                    actual_scene_plan_path = (
                        base_dir / "images" / f"{chapter.chapter_id}_weather_scenes.json"
                    )
                    actual_scene_plan_path.write_text(
                        actual_scene_plan.model_dump_json(indent=2),
                        encoding="utf-8",
                    )
                    weather_rendering = _weather_cfg.get("rendering", {}) or {}
                    weather_resolution = weather_rendering.get("resolution", {}) or {}
                    weather_branding = _weather_cfg.get("branding", {}) or {}
                    weather_video_path = base_dir / "images" / f"{chapter.chapter_id}_weather.mp4"
                    weather_video_result = render_weather_scene_video(
                        weather_data,
                        actual_scene_plan,
                        weather_video_path,
                        accent_color=weather_branding.get("accent_color") or _accent_color,
                        profile=json.dumps(
                            {"name": _profile_name, "weather": _weather_cfg},
                            sort_keys=True,
                            ensure_ascii=False,
                        ),
                        width=int(weather_resolution.get("width", 1920)),
                        height=int(weather_resolution.get("height", 1080)),
                        fps=int(weather_rendering.get("fps", settings.render_fps)),
                        title=weather_branding.get("title") or "Hava Durumu",
                    )
                    if weather_video_result.success and weather_video_path.exists():
                        media_path = weather_video_path
                        asset_type = "video"
                    else:
                        logger.warning(
                            "Timed weather video failed for %s; using static weather card",
                            chapter.chapter_id,
                        )

            # Convert overlays to OverlaySpec (profile-aware accent color + font)
            overlay_specs = _chapter_to_overlay_specs(
                chapter,
                _eff_font or settings.render_font,
                accent_color=_accent_color,
                animated_lower_thirds=_eff_lower_thirds,
            )

            # Compute fade durations based on transition types (Sprint 10)
            fade_in_dur = 0.0
            fade_out_dur = 0.0
            if chapter.transitions.in_transition.value in ("fade", "dissolve"):
                fade_in_dur = settings.render_transition_duration
            if chapter.transitions.out_transition.value in ("fade", "dissolve"):
                fade_out_dur = settings.render_transition_duration

            # Render segment. A full render can take an hour on the Pi, so the
            # directory is re-asserted per segment instead of only once up front.
            segments_dir.mkdir(parents=True, exist_ok=True)
            segment_filename = f"{chapter.chapter_id}.mp4"
            segment_path = segments_dir / segment_filename
            segment_rel_path = f"render/segments/{segment_filename}"

            # Skip segment if it already exists, is valid, AND is newer than its
            # inputs (image/video + audio). Merely existing is not enough: when
            # images or TTS are regenerated, the old segment is stale and MUST be
            # re-rendered, otherwise the final video keeps the previous content.
            if segment_path.exists() and segment_path.stat().st_size > 0:
                seg_mtime = segment_path.stat().st_mtime
                input_mtimes = []
                for _inp in (media_path, audio_path):
                    try:
                        if _inp and Path(_inp).exists():
                            input_mtimes.append(Path(_inp).stat().st_mtime)
                    except OSError:
                        pass
                segment_is_fresh = not input_mtimes or seg_mtime >= max(input_mtimes)
            else:
                segment_is_fresh = False

            if segment_is_fresh:
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

            beats = (chapter.metadata or {}).get("visual_beats") or []
            beat_image_paths = _beat_images(chapter.chapter_id, image_manifest) if beats else []
            use_beats = (
                asset_type != "video"
                and len(beats) >= 2
                and len(beat_image_paths) >= 2
                and not settings.dry_run
            )

            # Phase 4: Branch on asset_type for video vs image segments
            if use_beats:
                segment_result = _render_beat_chapter(
                    chapter=chapter,
                    beats=beats,
                    beat_image_paths=[str(base_dir / rel) for rel in beat_image_paths],
                    parts=_speaker_parts(chapter.chapter_id, tts_manifest),
                    audio_path=audio_path,
                    output_path=segment_path,
                    duration=duration,
                    overlays=overlay_specs,
                    fade_in_duration=fade_in_dur,
                    fade_out_duration=fade_out_dur,
                    settings=settings,
                    font=_eff_font or settings.render_font,
                    enhancement_kwargs=_enhancement_kwargs,
                )
            elif asset_type == "video":
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

            # Notify progress at completion of each chapter
            _emit(
                {
                    "stage": "segment_done",
                    "current": _ch_idx,
                    "total": _chapters_total,
                    "chapter_id": chapter.chapter_id,
                    "chapter_title": getattr(chapter, "title", "") or "",
                    "size_bytes": segment_result.size_bytes,
                    "progress_pct": int(100 * _ch_idx / max(_chapters_total, 1)),
                }
            )

        # Concatenate segments
        if not segment_entries:
            raise RuntimeError("No segments were rendered")

        segment_abs_paths = [
            str((base_dir / entry.segment_path).absolute()) for entry in segment_entries
        ]

        if _eff_topic_intro_enabled:
            topic_chapter_ids = {
                chapter.chapter_id
                for chapter in chapters_doc.chapters
                if (chapter.story_type or "").lower() not in {"intro", "outro"}
            }
            topic_total = len(topic_chapter_ids)
            topic_index = 0
            chapter_by_id = {chapter.chapter_id: chapter for chapter in chapters_doc.chapters}
            paths_with_topic_intros: list[str] = []
            for entry, segment_abs_path in zip(segment_entries, segment_abs_paths, strict=True):
                chapter = chapter_by_id[entry.chapter_id]
                if chapter.chapter_id in topic_chapter_ids:
                    topic_index += 1
                    segments_dir.mkdir(parents=True, exist_ok=True)
                    topic_path = segments_dir / f"topic_{chapter.chapter_id}.mp4"
                    create_topic_intro_segment(
                        output_path=str(topic_path),
                        topic_title=chapter.title,
                        topic_index=topic_index,
                        total_topics=topic_total,
                        channel_name=_eff_intro_show_name or settings.render_intro_show_name,
                        section_label=_eff_topic_intro_label,
                        audio_path=(
                            str(_topic_intro_audio_snapshot)
                            if _topic_intro_audio_snapshot
                            else None
                        ),
                        duration=_eff_topic_intro_duration,
                        resolution=settings.render_resolution,
                        fps=settings.render_fps,
                        bg_color=settings.render_intro_bg_color,
                        accent_color=_accent_color,
                        font=_eff_font or settings.render_font,
                        crf=settings.render_crf,
                        preset=settings.render_preset,
                        timeout_seconds=settings.render_timeout_segment,
                        dry_run=settings.dry_run,
                    )
                    paths_with_topic_intros.append(str(topic_path.absolute()))
                    total_duration += _eff_topic_intro_duration
                paths_with_topic_intros.append(segment_abs_path)
            segment_abs_paths = paths_with_topic_intros
            logger.info("Created %d topic intro segments", topic_index)

        # Prepend intro if enabled
        if _eff_intro_enabled:
            segments_dir.mkdir(parents=True, exist_ok=True)
            intro_path = segments_dir / "intro.mp4"
            _ep_date = ""
            if hasattr(episode, "published_at") and episode.published_at:
                _ep_date = episode.published_at.strftime("%d.%m.%Y")
            _ep_title = _eff_intro_episode_title or getattr(episode, "title", "") or episode_id
            create_intro_segment(
                output_path=str(intro_path),
                show_name=_eff_intro_show_name or settings.render_intro_show_name,
                episode_title=_ep_title,
                episode_date=_ep_date,
                slogan=_eff_intro_slogan,
                audio_path=str(_intro_audio_snapshot) if _intro_audio_snapshot else None,
                duration=_eff_intro_duration,
                resolution=settings.render_resolution,
                fps=settings.render_fps,
                bg_color=settings.render_intro_bg_color,
                accent_color=_accent_color,
                font=_eff_font or settings.render_font,
                crf=settings.render_crf,
                preset=settings.render_preset,
                timeout_seconds=settings.render_timeout_segment,
                dry_run=settings.dry_run,
            )
            segment_abs_paths.insert(0, str(intro_path.absolute()))
            total_duration += _eff_intro_duration
            logger.info("Intro segment created (%.1fs)", _eff_intro_duration)

        # Append outro if enabled
        if getattr(settings, "render_outro_enabled", False) is True:
            segments_dir.mkdir(parents=True, exist_ok=True)
            outro_path = segments_dir / "outro.mp4"
            create_outro_segment(
                output_path=str(outro_path),
                source_text=_eff_outro_text or settings.render_outro_text,
                audio_path=_eff_outro_audio or None,
                duration=settings.render_outro_duration,
                resolution=settings.render_resolution,
                fps=settings.render_fps,
                bg_color=settings.render_outro_bg_color,
                accent_color=_accent_color,
                font=_eff_font or settings.render_font,
                crf=settings.render_crf,
                preset=settings.render_preset,
                timeout_seconds=settings.render_timeout_segment,
                dry_run=settings.dry_run,
            )
            segment_abs_paths.append(str(outro_path.absolute()))
            total_duration += settings.render_outro_duration
            logger.info("Outro segment created (%.1fs)", settings.render_outro_duration)

        logger.info("Concatenating %d segments into draft video", len(segment_abs_paths))

        _emit(
            {
                "stage": "concat",
                "current": _chapters_total,
                "total": _chapters_total,
                "chapter_id": "concat",
                "chapter_title": f"Concat {len(segment_abs_paths)} segments",
                "progress_pct": 99,
            }
        )

        concat_result = concatenate_segments(
            segment_paths=segment_abs_paths,
            output_path=str(draft_path),
            timeout_seconds=settings.render_timeout_concat,
            dry_run=settings.dry_run,
        )

        total_size += concat_result.size_bytes

        # Draft is freshly rendered: clear any .stale marker from cascade invalidation
        stale_marker = draft_path.with_suffix(".mp4.stale")
        if stale_marker.exists():
            try:
                stale_marker.unlink()
            except OSError as e:
                logger.warning("Could not remove stale marker %s: %s", stale_marker, e)

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

        _emit(
            {
                "stage": "done",
                "current": _chapters_total,
                "total": _chapters_total,
                "chapter_id": "done",
                "chapter_title": "Render complete",
                "progress_pct": 100,
            }
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
        _write_render_progress(
            render_dir,
            {"stage": "failed", "error": str(e), "chapter_title": "Render failed"},
        )
        logger.error("Render failed for %s: %s", episode_id, e)
        raise


def _apply_branding_guard(
    settings,
    episode_id: str,
    profile_name: str,
    chapters_doc: ChapterDocument,
    render_cfg: dict,
) -> None:
    """Strip legacy attribution overlays, then fail closed on any remaining leak.

    Episodes chapterized before the independent-branding change carry a
    mandatory source-attribution lower third. Those overlays are dropped
    in-memory so an already-approved episode still renders; anything else that
    would become visible (titles, narration, render config) aborts the stage.
    """
    from btcedu.core import branding_guard

    branding = branding_guard.branding_config(settings, profile_name)
    if not branding or branding.get("visible_source_attribution", True):
        return
    removed = branding_guard.sanitize_overlays(chapters_doc, branding)
    if removed:
        logger.info(
            "Branding guard removed %d legacy attribution overlay(s) for %s", removed, episode_id
        )
    branding_guard.assert_no_forbidden_visible_text(
        settings,
        episode_id,
        profile_name,
        chapters_doc,
        render_config=render_cfg,
        stage="render",
    )


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
                "title": ch.title,
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
                "content_hash": img.get("content_hash"),
                "metadata": img.get("metadata"),
            }
            for img in image_manifest.get("images", [])
        ],
        "tts": [
            {
                "chapter_id": seg["chapter_id"],
                "file_path": seg["file_path"],
                "duration_seconds": seg["duration_seconds"],
                "text_hash": seg.get("text_hash"),
                "voice_id": seg.get("voice_id"),
                "model": seg.get("model"),
            }
            for seg in tts_manifest.get("segments", [])
        ],
    }
    if enhancement_settings:
        relevant_data["enhancements"] = enhancement_settings
    content_str = json.dumps(relevant_data, sort_keys=True, ensure_ascii=False, default=str)
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


def _current_render_content_hash(session, episode_id: str, settings: Settings) -> str | None:
    """Recompute the render content hash from the CURRENT inputs, or None.

    Mirrors the enhancement-flag/manifest logic in :func:`render_video` so a
    caller (e.g. the publisher) can verify the existing draft still matches the
    current chapters/images/audio without re-rendering.
    """
    base = Path(settings.outputs_dir) / episode_id
    chapters_path = base / "chapters.json"
    image_manifest_path = base / "images" / "manifest.json"
    tts_manifest_path = base / "tts" / "manifest.json"
    if not (chapters_path.exists() and image_manifest_path.exists() and tts_manifest_path.exists()):
        return None

    chapters_doc = _load_chapters(chapters_path)
    image_manifest = _load_image_manifest(image_manifest_path)
    tts_manifest = _load_tts_manifest(tts_manifest_path)

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    _render_cfg: dict = {}
    _weather_cfg: dict = {}
    try:
        from btcedu.profiles import get_registry as _get_profile_registry

        _profile_name = getattr(episode, "content_profile", "bitcoin_podcast") or "bitcoin_podcast"
        _profile = _get_profile_registry(settings).get(_profile_name)
        _render_cfg = (_profile.stage_config.get("render", {}) if _profile else {}) or {}
        _weather_cfg = (_profile.stage_config.get("weather", {}) if _profile else {}) or {}
    except Exception:  # noqa: BLE001
        _render_cfg = {}
        _weather_cfg = {}

    def _rc(key: str, default):
        if key in _render_cfg and _render_cfg[key] is not None:
            return _render_cfg[key]
        return default

    _enh_hash_data: dict = {}
    try:
        intro_audio = str(_rc("intro_audio", "") or "")
        topic_intro_audio = str(_rc("topic_intro_audio", intro_audio) or "")
        outro_audio = str(_rc("outro_audio", intro_audio) or "")
        _enh_hash_data = {
            "ken_burns": bool(
                _rc("ken_burns_enabled", getattr(settings, "render_ken_burns_enabled", False))
            ),
            "lower_thirds_animated": bool(
                _rc(
                    "lower_thirds_animated",
                    getattr(settings, "render_lower_thirds_animated", False),
                )
            ),
            "ticker": bool(
                _rc("ticker_enabled", getattr(settings, "render_ticker_enabled", False))
            ),
            "intro": bool(_rc("intro_enabled", getattr(settings, "render_intro_enabled", False))),
            "intro_audio": intro_audio,
            "intro_audio_sha256": (
                hashlib.sha256(Path(intro_audio).read_bytes()).hexdigest()
                if intro_audio and Path(intro_audio).exists()
                else None
            ),
            "intro_slogan": str(_rc("intro_slogan", "") or ""),
            "intro_episode_title": str(_rc("intro_episode_title", "") or ""),
            "intro_duration": float(_rc("intro_duration", settings.render_intro_duration)),
            "topic_intro_enabled": bool(_rc("topic_intro_enabled", False)),
            "topic_intro_duration": float(_rc("topic_intro_duration", 2.4)),
            "topic_intro_label": str(_rc("topic_intro_label", "GÜNDEM") or "GÜNDEM"),
            "topic_intro_audio": topic_intro_audio,
            "topic_intro_audio_sha256": (
                hashlib.sha256(Path(topic_intro_audio).read_bytes()).hexdigest()
                if topic_intro_audio and Path(topic_intro_audio).exists()
                else None
            ),
            "outro": bool(settings.render_outro_enabled),
            "outro_audio": outro_audio,
            "outro_audio_sha256": (
                hashlib.sha256(Path(outro_audio).read_bytes()).hexdigest()
                if outro_audio and Path(outro_audio).exists()
                else None
            ),
            "color_correction": bool(settings.render_color_correction_enabled),
            "font": str(_rc("font", getattr(settings, "render_font", ""))),
            "music_bed": str(_rc("music_bed", getattr(settings, "render_music_bed", ""))),
            "intro_show_name": str(
                _rc("intro_show_name", getattr(settings, "render_intro_show_name", ""))
            ),
            "outro_text": str(_rc("outro_text", getattr(settings, "render_outro_text", ""))),
            "accent_color": str(_rc("accent_color", "#F7931A")),
            "resolution": settings.render_resolution,
            "fps": settings.render_fps,
            "crf": settings.render_crf,
            "preset": settings.render_preset,
            "audio_bitrate": settings.render_audio_bitrate,
            "transition_duration": settings.render_transition_duration,
            "ken_burns_zoom_ratio": settings.render_ken_burns_zoom_ratio,
            "lower_third_slide_duration": settings.render_lower_thirds_slide_duration,
            "ticker_speed": settings.render_ticker_speed,
            "ticker_height": settings.render_ticker_height,
            "ticker_fontsize": settings.render_ticker_fontsize,
            "intro_bg_color": settings.render_intro_bg_color,
            "outro_duration": settings.render_outro_duration,
            "outro_bg_color": settings.render_outro_bg_color,
            "color_saturation": settings.render_color_saturation,
            "color_brightness": settings.render_color_brightness,
            "color_blue_shift": settings.render_color_blue_shift,
            "episode_title": getattr(episode, "title", None),
            "episode_published_at": str(getattr(episode, "published_at", None)),
            "weather": _weather_cfg,
        }
    except (AttributeError, TypeError):
        _enh_hash_data = {}

    return _compute_render_content_hash(
        chapters_doc,
        image_manifest,
        tts_manifest,
        _enh_hash_data if _enh_hash_data else None,
    )


def render_is_current(session, episode_id: str, settings: Settings) -> tuple[bool, str]:
    """Public validation helper: is the draft render current & valid to publish?

    Returns ``(ok, reason)``. Checks the draft exists and is non-empty, the
    manifest/provenance exist, there is no stale marker, and the recomputed input
    content hash matches what was rendered. Never raises.
    """
    base = Path(settings.outputs_dir) / episode_id
    draft = base / "render" / "draft.mp4"
    manifest = base / "render" / "render_manifest.json"
    provenance = base / "provenance" / "render_provenance.json"

    if not draft.exists() or draft.stat().st_size == 0:
        return False, "draft.mp4 missing or empty"
    if not manifest.exists() or not provenance.exists():
        return False, "render manifest/provenance missing"
    if draft.with_suffix(".mp4.stale").exists():
        return False, "render marked stale (upstream change)"
    try:
        content_hash = _current_render_content_hash(session, episode_id, settings)
    except Exception as e:  # noqa: BLE001
        return False, f"could not recompute render hash: {e}"
    if content_hash is None:
        return False, "render inputs (chapters/images/tts) missing"
    if not _is_render_current(manifest, provenance, draft, content_hash):
        return False, "render inputs changed since last render"
    try:
        manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        chapters_doc = _load_chapters(base / "chapters.json")
        expected = {chapter.chapter_id for chapter in chapters_doc.chapters}
        actual = {segment.get("chapter_id") for segment in manifest_data.get("segments", [])}
        if actual != expected:
            return False, "render manifest does not cover every chapter exactly once"
    except (OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        return False, f"render manifest coverage validation failed: {exc}"
    return True, "render is current"


def _chapter_to_overlay_specs(
    chapter,
    font: str,
    accent_color: str = "#F7931A",
    animated_lower_thirds: bool = False,
) -> list:
    """Convert chapter overlays to OverlaySpec list.

    An overlay carrying a ``subtext`` becomes a two-line lower third: the short
    headline on top, a one-sentence summary below. The animated renderer draws
    both lines inside its own background bar, so a single spec is enough there;
    the static renderer needs one spec per line.

    Args:
        chapter: Chapter object
        font: Font name
        accent_color: Hex color for statistic overlays (profile-specific)
        animated_lower_thirds: Whether animated lower thirds are enabled

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

        subtext = (getattr(overlay, "subtext", None) or "").strip()
        if subtext and overlay.type.value == "lower_third" and animated_lower_thirds:
            # The animated lower third draws both lines in its own bar.
            overlay_specs.append(
                OverlaySpec(
                    text=f"{overlay.text}\\n{_shorten_overlay_text(subtext)}",
                    overlay_type="lower_third",
                    fontsize=OVERLAY_STYLES["lower_third_headline"]["fontsize"],
                    fontcolor=style["fontcolor"],
                    font=font,
                    position=style["position"],
                    start=overlay.start_offset_seconds,
                    end=overlay.start_offset_seconds + overlay.duration_seconds,
                )
            )
            continue

        if subtext and overlay.type.value == "lower_third":
            # Two-line lower third: headline on top, one-sentence summary below.
            head_style = OVERLAY_STYLES["lower_third_headline"]
            sub_style = OVERLAY_STYLES["lower_third_subtext"]
            end = overlay.start_offset_seconds + overlay.duration_seconds
            overlay_specs.append(
                OverlaySpec(
                    text=overlay.text,
                    overlay_type="lower_third",
                    fontsize=head_style["fontsize"],
                    fontcolor=head_style["fontcolor"],
                    font=font,
                    position=head_style["position"],
                    start=overlay.start_offset_seconds,
                    end=end,
                )
            )
            overlay_specs.append(
                OverlaySpec(
                    text=_shorten_overlay_text(subtext),
                    overlay_type="lower_third",
                    fontsize=sub_style["fontsize"],
                    fontcolor=sub_style["fontcolor"],
                    font=font,
                    position=sub_style["position"],
                    start=overlay.start_offset_seconds + 0.2,
                    end=end,
                )
            )
            continue

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


def _shorten_overlay_text(text: str, max_chars: int = 88) -> str:
    """Trim a summary line to one line that fits inside the title-safe area.

    ffmpeg's drawtext draws a line as-is, so an over-long summary would run past
    the frame edge. The line is cut at a word boundary and ends with an ellipsis.

    88 is measured, not guessed: at the 34 px summary size the rendered line ends
    at 1723 px of 1920, just inside the 5 % title-safe margin. 95 characters
    already reach the frame edge.
    """
    text = " ".join(text.split())
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1]
    if " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:") + "…"


def _resolve_chapter_media(
    chapter_id: str,
    image_manifest: dict,
    tts_manifest: dict,
    base_dir: Path,
    anchor_manifest: dict | None = None,
) -> tuple[Path, Path, float, str]:
    """Resolve media, audio paths and duration for a chapter.

    Priority: anchor video > image/video from manifest.

    Args:
        chapter_id: Chapter identifier
        image_manifest: Image manifest dict
        tts_manifest: TTS manifest dict
        base_dir: Episode outputs directory
        anchor_manifest: Anchor manifest dict (optional)

    Returns:
        Tuple of (media_path, audio_path, duration_seconds, asset_type)
        asset_type is "photo", "video", or "anchor_video"

    Raises:
        ValueError: If image/video or audio not found
    """
    # Check anchor manifest first (TALKING_HEAD chapters)
    if anchor_manifest:
        for seg in anchor_manifest.get("segments", []):
            if seg["chapter_id"] == chapter_id:
                anchor_path = base_dir / seg["video_path"]
                if anchor_path.exists():
                    # Anchor video contains both video and audio synced together,
                    # but we still need TTS audio for the final render mix
                    audio_entry = None
                    for s in tts_manifest.get("segments", []):
                        if s["chapter_id"] == chapter_id:
                            audio_entry = s
                            break
                    if audio_entry:
                        audio_path = base_dir / audio_entry["file_path"]
                        if audio_path.exists():
                            return (
                                anchor_path,
                                audio_path,
                                audio_entry["duration_seconds"],
                                "video",
                            )

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


_BEAT_TAIL_HEADROOM_SECONDS = 0.2


def _render_beat_chapter(
    *,
    chapter,
    beats: list[dict],
    beat_image_paths: list[str],
    parts: list[dict],
    audio_path,
    output_path,
    duration: float,
    overlays,
    fade_in_duration: float,
    fade_out_duration: float,
    settings: Settings,
    font: str,
    enhancement_kwargs,
):
    """Render one chapter as a sequence of shots, one per presenter block.

    A change of presenter is a change of scene, so the chapter is built from
    several silent shots that are joined and then given the chapter's own
    narration track. Laying the original audio over the joined picture keeps the
    sound byte-for-byte what TTS produced, including the pauses between
    speakers; only the picture cuts.

    The lower third belongs to the story, not to a speaker, so it is drawn on the
    first shot only. The fades stay at the outer edges of the chapter.
    """
    from btcedu.services.ffmpeg_service import (
        concatenate_segments,
        create_segment,
        generate_silent_audio,
        replace_audio_track,
    )

    output_path = Path(output_path)
    beats_dir = output_path.parent / "beats"
    beats_dir.mkdir(parents=True, exist_ok=True)

    usable = min(len(beats), len(beat_image_paths))
    beats = beats[:usable]
    # A few frames of headroom on the last shot: the joined picture must never be
    # shorter than the narration, because the audio is laid over it with
    # -shortest and would otherwise lose the final words.
    durations = _beat_durations(beats, parts, duration)
    durations[-1] = round(durations[-1] + _BEAT_TAIL_HEADROOM_SECONDS, 3)

    shot_paths: list[str] = []
    for index, (beat, shot_duration) in enumerate(zip(beats, durations, strict=True)):
        silent_audio = beats_dir / f"{chapter.chapter_id}_beat{index:02d}.m4a"
        generate_silent_audio(str(silent_audio), duration=shot_duration)
        shot_path = beats_dir / f"{chapter.chapter_id}_beat{index:02d}.mp4"
        create_segment(
            image_path=beat_image_paths[index],
            audio_path=str(silent_audio),
            output_path=str(shot_path),
            duration=shot_duration,
            overlays=overlays if index == 0 else [],
            resolution=settings.render_resolution,
            fps=settings.render_fps,
            crf=settings.render_crf,
            preset=settings.render_preset,
            audio_bitrate=settings.render_audio_bitrate,
            font=font,
            fade_in_duration=fade_in_duration if index == 0 else 0.0,
            fade_out_duration=fade_out_duration if index == len(beats) - 1 else 0.0,
            timeout_seconds=settings.render_timeout_segment,
            **enhancement_kwargs("photo", chapter.order - 1 + index),
        )
        shot_paths.append(str(shot_path))
        logger.info(
            "  shot %d/%d of %s: %s, %.1fs",
            index + 1,
            len(beats),
            chapter.chapter_id,
            beat.get("role", "?"),
            shot_duration,
        )

    silent_chapter = beats_dir / f"{chapter.chapter_id}_silent.mp4"
    concatenate_segments(shot_paths, str(silent_chapter))
    return replace_audio_track(
        video_path=str(silent_chapter),
        audio_path=str(audio_path),
        output_path=str(output_path),
        audio_bitrate=settings.render_audio_bitrate,
        timeout_seconds=settings.render_timeout_segment,
    )


def _beat_images(chapter_id: str, image_manifest: dict) -> list[str]:
    """Relative image paths of a chapter, ordered by presenter block."""
    entries = [img for img in image_manifest.get("images", []) if img["chapter_id"] == chapter_id]
    entries.sort(key=lambda e: int((e.get("metadata") or {}).get("beat_index") or 0))
    return [e["file_path"] for e in entries if e.get("generation_method") != "failed"]


def _beat_durations(beats: list[dict], parts: list[dict], total_duration: float) -> list[float]:
    """Split a chapter's running time across its presenter blocks.

    Weights come from the measured per-speaker audio when TTS recorded it, and
    from word counts otherwise. The weights are then scaled onto the chapter
    duration, so the blocks always add up to exactly the chapter length however
    the pauses between speakers were distributed.
    """
    weights: list[float] = []
    for beat in beats:
        indices = [int(i) for i in beat.get("segment_indices") or []]
        measured = [
            float(parts[i].get("duration_seconds") or 0.0) for i in indices if 0 <= i < len(parts)
        ]
        if measured and sum(measured) > 0:
            weights.append(sum(measured))
        else:
            weights.append(float(max(1, len(str(beat.get("text") or "").split()))))

    total_weight = sum(weights)
    if total_weight <= 0:
        share = total_duration / len(beats)
        return [share] * len(beats)

    durations = [total_duration * w / total_weight for w in weights]
    durations[-1] = round(total_duration - sum(durations[:-1]), 3)
    return durations


def _speaker_parts(chapter_id: str, tts_manifest: dict) -> list[dict]:
    """The per-speaker audio parts TTS recorded for a chapter, or []."""
    for seg in tts_manifest.get("segments", []):
        if seg["chapter_id"] == chapter_id:
            parts = (seg.get("metadata") or {}).get("speaker_parts") or []
            return [p for p in parts if isinstance(p, dict)]
    return []


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
