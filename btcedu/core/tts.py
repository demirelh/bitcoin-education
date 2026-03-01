"""TTS generation: Create per-chapter MP3 audio from chapter narration text."""

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


@dataclass
class AudioEntry:
    """Metadata for a single generated or placeholder audio file."""

    chapter_id: str
    chapter_title: str
    text_length: int
    text_hash: str
    duration_seconds: float
    file_path: str  # Relative path from episode outputs dir
    sample_rate: int
    model: str
    voice_id: str
    mime_type: str
    size_bytes: int
    cost_usd: float
    metadata: dict


@dataclass
class TTSResult:
    """Summary of TTS generation operation for one episode."""

    episode_id: str
    tts_path: Path
    manifest_path: Path
    provenance_path: Path
    segment_count: int = 0
    total_duration_seconds: float = 0.0
    total_characters: int = 0
    cost_usd: float = 0.0
    skipped: bool = False


def generate_tts(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
    chapter_id: str | None = None,
) -> TTSResult:
    """Generate TTS audio for all chapters (or a specific chapter) in an episode.

    Args:
        session: SQLAlchemy database session
        episode_id: Episode identifier
        settings: Application configuration
        force: If True, regenerate all audio even if current
        chapter_id: If provided, only regenerate this specific chapter

    Returns:
        TTSResult with paths, counts, duration, cost, and skip status

    Raises:
        ValueError: If episode/chapter not found or chapters.json invalid
        RuntimeError: If TTS API fails
    """
    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    # V2 pipeline only
    if episode.pipeline_version != 2:
        raise ValueError(
            f"Episode {episode_id} is v1 pipeline (pipeline_version={episode.pipeline_version}). "
            "TTS is only supported for v2 pipeline."
        )

    # Check episode status (allow IMAGES_GENERATED or TTS_DONE for idempotency)
    if (
        episode.status not in (EpisodeStatus.IMAGES_GENERATED, EpisodeStatus.TTS_DONE)
        and not force
    ):
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'images_generated' or 'tts_done'. Use --force to override."
        )

    # Resolve paths
    chapters_path = Path(settings.outputs_dir) / episode_id / "chapters.json"
    if not chapters_path.exists():
        raise FileNotFoundError(
            f"Chapters file not found for episode {episode_id}: {chapters_path}"
        )

    tts_dir = Path(settings.outputs_dir) / episode_id / "tts"
    manifest_path = tts_dir / "manifest.json"
    provenance_path = (
        Path(settings.outputs_dir) / episode_id / "provenance" / "tts_provenance.json"
    )

    # Load chapters
    chapters_doc = _load_chapters(chapters_path)
    chapters_hash = _compute_chapters_narration_hash(chapters_doc)

    # Idempotency check
    if not force and chapter_id is None:
        if _is_tts_current(manifest_path, provenance_path, chapters_hash):
            logger.info(
                "TTS is current for %s (use --force to regenerate)", episode_id
            )
            return TTSResult(
                episode_id=episode_id,
                tts_path=tts_dir,
                manifest_path=manifest_path,
                provenance_path=provenance_path,
                skipped=True,
            )

    # Create PipelineRun record
    pipeline_run = PipelineRun(
        episode_id=episode_id,
        stage="tts",
        status=RunStatus.RUNNING.value,
        started_at=_utcnow(),
    )
    session.add(pipeline_run)
    session.commit()

    try:
        # Create TTS service
        from btcedu.services.elevenlabs_service import ElevenLabsService, TTSRequest

        tts_service = ElevenLabsService(
            api_key=settings.elevenlabs_api_key,
            default_voice_id=settings.elevenlabs_voice_id,
            default_model=settings.elevenlabs_model,
        )

        # Filter chapters to process
        chapters_to_process = chapters_doc.chapters
        if chapter_id:
            chapters_to_process = [
                c for c in chapters_doc.chapters if c.chapter_id == chapter_id
            ]
            if not chapters_to_process:
                raise ValueError(f"Chapter {chapter_id} not found in chapters.json")

        # Load existing manifest for partial recovery
        existing_entries: dict[str, dict] = {}
        if manifest_path.exists():
            try:
                existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                existing_entries = {
                    entry["chapter_id"]: entry
                    for entry in existing_manifest.get("segments", [])
                }
            except (json.JSONDecodeError, KeyError):
                logger.warning("Could not load existing manifest, will regenerate all")

        # Process each chapter
        audio_entries: list[AudioEntry] = []
        total_cost = 0.0
        total_duration = 0.0
        total_characters = 0

        tts_dir.mkdir(parents=True, exist_ok=True)

        for chapter in chapters_to_process:
            narration_text = chapter.narration.text
            text_hash = _compute_narration_hash(narration_text)

            # Partial recovery: skip unchanged chapters with existing MP3
            if not force and chapter.chapter_id in existing_entries:
                existing = existing_entries[chapter.chapter_id]
                mp3_path = Path(settings.outputs_dir) / episode_id / existing.get("file_path", "")
                if existing.get("text_hash") == text_hash and mp3_path.exists():
                    logger.info("Skipping unchanged chapter %s", chapter.chapter_id)
                    entry = AudioEntry(
                        chapter_id=existing["chapter_id"],
                        chapter_title=existing.get("chapter_title", chapter.title),
                        text_length=existing.get("text_length", len(narration_text)),
                        text_hash=text_hash,
                        duration_seconds=existing.get("duration_seconds", 0.0),
                        file_path=existing["file_path"],
                        sample_rate=existing.get("sample_rate", 44100),
                        model=existing.get("model", settings.elevenlabs_model),
                        voice_id=existing.get("voice_id", settings.elevenlabs_voice_id),
                        mime_type="audio/mpeg",
                        size_bytes=existing.get("size_bytes", 0),
                        cost_usd=0.0,  # No cost for skipped
                        metadata=existing.get("metadata", {}),
                    )
                    audio_entries.append(entry)
                    total_duration += entry.duration_seconds
                    total_characters += entry.text_length
                    continue

            # Cost guard
            episode_total_cost = _get_episode_total_cost(session, episode_id)
            if episode_total_cost + total_cost > settings.max_episode_cost_usd:
                raise RuntimeError(
                    f"Episode cost limit exceeded: {episode_total_cost + total_cost:.2f} > "
                    f"{settings.max_episode_cost_usd}. Stopping TTS generation."
                )

            # Generate audio
            entry = _generate_single_audio(
                chapter, tts_service, tts_dir, settings
            )
            audio_entries.append(entry)
            total_cost += entry.cost_usd
            total_duration += entry.duration_seconds
            total_characters += entry.text_length

            # Create MediaAsset record
            _create_media_asset_record(session, episode_id, entry)

        # Write manifest
        manifest_data = {
            "episode_id": episode_id,
            "schema_version": "1.0",
            "voice_id": settings.elevenlabs_voice_id,
            "model": settings.elevenlabs_model,
            "generated_at": _utcnow().isoformat(),
            "total_duration_seconds": total_duration,
            "total_characters": total_characters,
            "total_cost_usd": total_cost,
            "segments": [asdict(entry) for entry in audio_entries],
        }
        manifest_path.write_text(
            json.dumps(manifest_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Write provenance
        provenance_data = {
            "stage": "tts",
            "episode_id": episode_id,
            "timestamp": _utcnow().isoformat(),
            "model": "elevenlabs",
            "tts_model": settings.elevenlabs_model,
            "voice_id": settings.elevenlabs_voice_id,
            "input_files": [str(chapters_path)],
            "input_content_hash": chapters_hash,
            "output_files": [str(manifest_path)]
            + [str(tts_dir / f"{entry.chapter_id}.mp3") for entry in audio_entries],
            "segment_count": len(audio_entries),
            "total_duration_seconds": total_duration,
            "total_characters": total_characters,
            "cost_usd": total_cost,
        }
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(provenance_data, indent=2, ensure_ascii=False), encoding="utf-8"
        )

        # Create ContentArtifact record
        artifact = ContentArtifact(
            episode_id=episode_id,
            artifact_type="tts_audio",
            file_path=str(manifest_path.relative_to(Path(settings.outputs_dir) / episode_id)),
            prompt_hash=chapters_hash,
            model="elevenlabs",
            created_at=_utcnow(),
        )
        session.add(artifact)

        # Mark downstream stages stale
        _mark_downstream_stale(episode_id, Path(settings.outputs_dir))

        # Update episode status
        episode.status = EpisodeStatus.TTS_DONE
        session.commit()

        # Update PipelineRun
        pipeline_run.status = RunStatus.SUCCESS.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.estimated_cost_usd = total_cost
        session.commit()

        logger.info(
            "TTS generation complete for %s: %d segments, %.1fs total, $%.3f",
            episode_id,
            len(audio_entries),
            total_duration,
            total_cost,
        )

        return TTSResult(
            episode_id=episode_id,
            tts_path=tts_dir,
            manifest_path=manifest_path,
            provenance_path=provenance_path,
            segment_count=len(audio_entries),
            total_duration_seconds=total_duration,
            total_characters=total_characters,
            cost_usd=total_cost,
            skipped=False,
        )

    except Exception as e:
        pipeline_run.status = RunStatus.FAILED.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        episode.error_message = str(e)
        session.commit()
        logger.error("TTS generation failed for %s: %s", episode_id, e)
        raise


def _load_chapters(chapters_path: Path) -> ChapterDocument:
    """Load and validate chapter JSON."""
    try:
        chapters_data = json.loads(chapters_path.read_text(encoding="utf-8"))
        return ChapterDocument(**chapters_data)
    except (json.JSONDecodeError, ValidationError) as e:
        raise ValueError(f"Invalid chapters.json at {chapters_path}: {e}") from e


def _compute_narration_hash(text: str) -> str:
    """Compute SHA-256 hash of a single narration text."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _compute_chapters_narration_hash(chapters_doc: ChapterDocument) -> str:
    """Compute SHA-256 hash of all chapter narration texts (not visual fields)."""
    relevant_data = [
        {"chapter_id": ch.chapter_id, "narration_text": ch.narration.text}
        for ch in chapters_doc.chapters
    ]
    content_str = json.dumps(relevant_data, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(content_str.encode("utf-8")).hexdigest()


def _is_tts_current(
    manifest_path: Path,
    provenance_path: Path,
    chapters_hash: str,
) -> bool:
    """Check if TTS output is current (idempotency)."""
    if not manifest_path.exists() or not provenance_path.exists():
        return False

    # Check for .stale marker
    stale_marker = manifest_path.with_suffix(".json.stale")
    if stale_marker.exists():
        logger.info("TTS manifest marked as stale")
        return False

    # Check provenance hash
    try:
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        if provenance.get("input_content_hash") != chapters_hash:
            logger.info("Chapter narration content has changed")
            return False
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Could not verify TTS provenance: %s", e)
        return False

    # Verify all MP3s exist
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        base_dir = manifest_path.parent.parent  # outputs/{ep_id}/
        for segment in manifest.get("segments", []):
            mp3_path = base_dir / segment["file_path"]
            if not mp3_path.exists():
                logger.info("MP3 file missing: %s", mp3_path)
                return False
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Could not verify TTS manifest: %s", e)
        return False

    return True


def _generate_single_audio(
    chapter,
    tts_service,
    output_dir: Path,
    settings: Settings,
) -> AudioEntry:
    """Generate audio for a single chapter.

    In dry-run mode, writes a silent MP3 placeholder.
    """
    from btcedu.services.elevenlabs_service import TTSRequest

    narration_text = chapter.narration.text
    text_hash = _compute_narration_hash(narration_text)
    mp3_filename = f"{chapter.chapter_id}.mp3"
    mp3_path = output_dir / mp3_filename

    if settings.dry_run:
        # Write a minimal silent MP3 placeholder
        silent_mp3 = _create_silent_mp3()
        mp3_path.write_bytes(silent_mp3)
        return AudioEntry(
            chapter_id=chapter.chapter_id,
            chapter_title=chapter.title,
            text_length=len(narration_text),
            text_hash=text_hash,
            duration_seconds=0.0,
            file_path=f"tts/{mp3_filename}",
            sample_rate=44100,
            model=settings.elevenlabs_model,
            voice_id=settings.elevenlabs_voice_id,
            mime_type="audio/mpeg",
            size_bytes=len(silent_mp3),
            cost_usd=0.0,
            metadata={"dry_run": True},
        )

    # Call TTS service
    request = TTSRequest(
        text=narration_text,
        voice_id=settings.elevenlabs_voice_id,
        model=settings.elevenlabs_model,
        stability=settings.elevenlabs_stability,
        similarity_boost=settings.elevenlabs_similarity_boost,
        style=settings.elevenlabs_style,
        use_speaker_boost=settings.elevenlabs_use_speaker_boost,
    )

    response = tts_service.synthesize(request)

    # Write MP3 file
    mp3_path.write_bytes(response.audio_bytes)
    size_bytes = mp3_path.stat().st_size

    logger.info(
        "Generated TTS for chapter %s: %.1fs, %d chars, $%.3f",
        chapter.chapter_id,
        response.duration_seconds,
        response.character_count,
        response.cost_usd,
    )

    return AudioEntry(
        chapter_id=chapter.chapter_id,
        chapter_title=chapter.title,
        text_length=len(narration_text),
        text_hash=text_hash,
        duration_seconds=response.duration_seconds,
        file_path=f"tts/{mp3_filename}",
        sample_rate=response.sample_rate,
        model=response.model,
        voice_id=response.voice_id,
        mime_type="audio/mpeg",
        size_bytes=size_bytes,
        cost_usd=response.cost_usd,
        metadata={
            "generated_at": _utcnow().isoformat(),
        },
    )


def _create_silent_mp3() -> bytes:
    """Create a minimal valid MP3 file (silence placeholder for dry-run)."""
    # Minimal MP3 frame: MPEG1 Layer3, 128kbps, 44100Hz, stereo
    # This is a single valid MP3 frame of silence
    frame_header = bytes([0xFF, 0xFB, 0x90, 0x00])
    # Pad to frame size (417 bytes for 128kbps at 44100Hz)
    frame_data = bytes(413)
    return frame_header + frame_data


def _create_media_asset_record(
    session: Session,
    episode_id: str,
    entry: AudioEntry,
) -> None:
    """Create MediaAsset database record for generated audio."""
    media_asset = MediaAsset(
        episode_id=episode_id,
        asset_type=MediaAssetType.AUDIO,
        chapter_id=entry.chapter_id,
        file_path=entry.file_path,
        mime_type=entry.mime_type,
        size_bytes=entry.size_bytes,
        duration_seconds=entry.duration_seconds,
        meta=json.dumps(entry.metadata, ensure_ascii=False),
        created_at=_utcnow(),
    )
    session.add(media_asset)


def _mark_downstream_stale(episode_id: str, outputs_dir: Path) -> None:
    """Mark RENDER stage as stale when TTS changes."""
    stale_data = {
        "invalidated_at": _utcnow().isoformat(),
        "invalidated_by": "tts",
        "reason": "audio_changed",
    }

    render_draft = outputs_dir / episode_id / "render" / "draft.mp4"
    if render_draft.exists():
        stale_marker = render_draft.with_suffix(".mp4.stale")
        stale_marker.write_text(json.dumps(stale_data, ensure_ascii=False))
        logger.info("Marked render draft as stale: %s", stale_marker)


def _get_episode_total_cost(session: Session, episode_id: str) -> float:
    """Get cumulative cost for all pipeline runs for this episode."""
    total = (
        session.query(PipelineRun)
        .filter(PipelineRun.episode_id == episode_id)
        .filter(PipelineRun.estimated_cost_usd.isnot(None))
    )
    return sum(run.estimated_cost_usd for run in total if run.estimated_cost_usd)
