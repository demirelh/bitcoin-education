"""TTS generation: Create per-chapter MP3 audio from chapter narration text."""

import hashlib
import json
import logging
import re
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
    if episode.status not in (EpisodeStatus.IMAGES_GENERATED, EpisodeStatus.TTS_DONE) and not force:
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
    provenance_path = Path(settings.outputs_dir) / episode_id / "provenance" / "tts_provenance.json"

    # Load chapters
    chapters_doc = _load_chapters(chapters_path)

    # Resolve profile-only voice/model/params + pronunciation lexicon up front so
    # the idempotency hash reflects them: a voice, model, voice-param or lexicon
    # change now forces regeneration (previously only narration text was hashed).
    tts_config = _resolve_tts_config(episode, settings)
    voice_sig = _voice_config_signature(tts_config)
    lexicon = tts_config["pronunciation_lexicon"]
    chapters_hash = _compute_tts_content_hash(chapters_doc, lexicon, voice_sig)

    # Idempotency check
    if not force and chapter_id is None:
        if _is_tts_current(
            manifest_path,
            provenance_path,
            chapters_hash,
            {chapter.chapter_id for chapter in chapters_doc.chapters},
        ):
            logger.info("TTS is current for %s (use --force to regenerate)", episode_id)
            # Still advance episode status so pipeline can proceed
            if episode.status == EpisodeStatus.IMAGES_GENERATED:
                episode.status = EpisodeStatus.TTS_DONE
                session.commit()
            return TTSResult(
                episode_id=episode_id,
                tts_path=tts_dir,
                manifest_path=manifest_path,
                provenance_path=provenance_path,
                skipped=True,
            )

    # Create PipelineRun record
    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage="tts",
        status=RunStatus.RUNNING.value,
        started_at=_utcnow(),
    )
    session.add(pipeline_run)
    session.commit()

    try:
        # Voice/model/params + lexicon were resolved above (profile-only).
        _voice_id = tts_config["voice_id"]
        _model = tts_config["model"]
        _stability = tts_config["stability"]
        _style = tts_config["style"]
        _speed = tts_config["speed"]
        _role_voices = tts_config.get("voices") or {}
        total_cost = 0.0

        def _before_tts_api_call(sent_chars: int, chunk_chars: int) -> None:
            from btcedu.services.elevenlabs_service import _compute_cost
            from btcedu.services.errors import ErrorCategory, PipelineError

            episode_total_cost = _get_episode_total_cost(session, episode_id)
            projected = episode_total_cost + total_cost + _compute_cost(sent_chars + chunk_chars)
            if projected > settings.max_episode_cost_usd:
                error = PipelineError(
                    f"Episode cost limit exceeded before TTS API call: "
                    f"${projected:.4f} > ${settings.max_episode_cost_usd:.4f}",
                    ErrorCategory.PERMANENT_COST_LIMIT,
                )
                error.cost_usd = _compute_cost(sent_chars)
                raise error

        # Create TTS service (profile model is honoured, not the global default)
        from btcedu.services.elevenlabs_service import ElevenLabsService

        tts_service = ElevenLabsService(
            api_key=settings.elevenlabs_api_key,
            default_voice_id=_voice_id,
            default_model=_model,
            before_api_call=_before_tts_api_call,
        )

        # Filter chapters to process
        chapters_to_process = chapters_doc.chapters
        if chapter_id:
            chapters_to_process = [c for c in chapters_doc.chapters if c.chapter_id == chapter_id]
            if not chapters_to_process:
                raise ValueError(f"Chapter {chapter_id} not found in chapters.json")

        # Load existing manifest for partial recovery
        existing_entries: dict[str, dict] = {}
        if manifest_path.exists():
            try:
                existing_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                existing_entries = {
                    entry["chapter_id"]: entry for entry in existing_manifest.get("segments", [])
                }
            except (json.JSONDecodeError, KeyError):
                logger.warning("Could not load existing manifest, will regenerate all")

        # Process each chapter
        audio_entries: list[AudioEntry] = []
        total_duration = 0.0
        total_characters = 0

        tts_dir.mkdir(parents=True, exist_ok=True)

        if chapter_id:
            for chapter in chapters_doc.chapters:
                if chapter.chapter_id == chapter_id:
                    continue
                existing = existing_entries.get(chapter.chapter_id)
                if not existing:
                    raise ValueError(
                        f"Cannot regenerate only {chapter_id}: manifest entry for "
                        f"{chapter.chapter_id} is missing; run the full TTS stage"
                    )
                mp3_path = Path(settings.outputs_dir) / episode_id / existing.get("file_path", "")
                if not mp3_path.exists():
                    raise ValueError(
                        f"Cannot regenerate only {chapter_id}: audio for "
                        f"{chapter.chapter_id} is missing; run the full TTS stage"
                    )
                synthesis_text = _apply_pronunciation_lexicon(chapter.narration.text, lexicon)
                expected_hash = _chapter_tts_hash(
                    chapter.chapter_id,
                    chapter.narration.text,
                    synthesis_text,
                    voice_sig,
                )
                if existing.get("text_hash") != expected_hash:
                    raise ValueError(
                        f"Cannot regenerate only {chapter_id}: audio for "
                        f"{chapter.chapter_id} is stale; run the full TTS stage"
                    )
                entry = AudioEntry(
                    chapter_id=existing["chapter_id"],
                    chapter_title=existing.get("chapter_title", chapter.title),
                    text_length=existing.get("text_length", len(chapter.narration.text)),
                    text_hash=expected_hash,
                    duration_seconds=existing.get("duration_seconds", 0.0),
                    file_path=existing["file_path"],
                    sample_rate=existing.get("sample_rate", 44100),
                    model=existing.get("model", _model),
                    voice_id=existing.get("voice_id", _voice_id),
                    mime_type="audio/mpeg",
                    size_bytes=existing.get("size_bytes", 0),
                    cost_usd=0.0,
                    metadata=existing.get("metadata", {}),
                )
                audio_entries.append(entry)
                total_duration += entry.duration_seconds
                total_characters += entry.text_length

        for chapter in chapters_to_process:
            narration_text = chapter.narration.text
            synthesis_text = _apply_pronunciation_lexicon(narration_text, lexicon)
            # Per-chapter idempotency hash: chapter_id + original text + synthesis
            # text + voice + model + voice params + lexicon. A change to any of
            # these regenerates just that chapter (chapter-level retry/recovery).
            segments = _speaker_segments(chapter) if _role_voices else []
            text_hash = _chapter_tts_hash(
                chapter.chapter_id,
                narration_text,
                synthesis_text,
                voice_sig,
                [s["role"] for s in segments],
            )

            # Partial recovery: skip chapters whose text+synthesis+voice are
            # unchanged and whose MP3 still exists; regenerate missing/changed.
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
                        model=existing.get("model", _model),
                        voice_id=existing.get("voice_id", _voice_id),
                        mime_type="audio/mpeg",
                        size_bytes=existing.get("size_bytes", 0),
                        cost_usd=0.0,  # No cost for skipped
                        metadata=existing.get("metadata", {}),
                    )
                    audio_entries.append(entry)
                    total_duration += entry.duration_seconds
                    total_characters += entry.text_length
                    continue

            # Cost guard — checked BEFORE every synthesis API call.
            episode_total_cost = _get_episode_total_cost(session, episode_id)
            if episode_total_cost + total_cost >= settings.max_episode_cost_usd:
                from btcedu.services.errors import ErrorCategory, PipelineError

                raise PipelineError(
                    f"Episode cost limit reached before TTS generation: "
                    f"${episode_total_cost + total_cost:.4f} >= "
                    f"${settings.max_episode_cost_usd:.4f}",
                    ErrorCategory.PERMANENT_COST_LIMIT,
                )

            # Generate audio (profile voice/model/params; synthesis text carries
            # any pronunciation-lexicon substitutions, display narration does not)
            if segments:
                entry = _generate_multi_voice_audio(
                    chapter,
                    segments,
                    tts_service,
                    tts_dir,
                    settings,
                    role_voices=_role_voices,
                    fallback={
                        "voice_id": _voice_id,
                        "model": _model,
                        "stability": _stability,
                        "similarity_boost": tts_config["similarity_boost"],
                        "style": _style,
                        "speed": _speed,
                        "use_speaker_boost": tts_config["use_speaker_boost"],
                        "voice_id_configured": bool(_voice_id),
                    },
                    lexicon=lexicon,
                    text_hash=text_hash,
                )
            else:
                entry = _generate_single_audio(
                    chapter,
                    tts_service,
                    tts_dir,
                    settings,
                    voice_id=_voice_id,
                    model=_model,
                    stability=_stability,
                    similarity_boost=tts_config["similarity_boost"],
                    style=_style,
                    speed=_speed,
                    use_speaker_boost=tts_config["use_speaker_boost"],
                    synthesis_text=synthesis_text,
                    text_hash=text_hash,
                )
            audio_entries.append(entry)
            total_cost += entry.cost_usd
            total_duration += entry.duration_seconds
            total_characters += entry.text_length

            # Create MediaAsset record
            _create_media_asset_record(session, episode_id, entry)

        order_by_id = {chapter.chapter_id: chapter.order for chapter in chapters_doc.chapters}
        audio_entries.sort(key=lambda entry: order_by_id.get(entry.chapter_id, 10**9))

        # Write manifest
        manifest_data = {
            "episode_id": episode_id,
            "schema_version": "1.0",
            "voice_id": _voice_id,
            "model": _model,
            "voice_config": voice_sig,
            "pronunciation_lexicon": lexicon,
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
            "tts_model": _model,
            "voice_id": _voice_id,
            "voice_config": voice_sig,
            "pronunciation_lexicon_size": len(lexicon),
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

        # This stage is now current: clear any stale marker from cascade
        # invalidation so a single stale flag doesn't force endless regeneration.
        _stale_marker = manifest_path.with_suffix(".json.stale")
        if _stale_marker.exists():
            try:
                _stale_marker.unlink()
            except OSError as _e:  # pragma: no cover - defensive
                logger.warning("Could not remove TTS stale marker %s: %s", _stale_marker, _e)

        # Mark downstream stages stale (RENDER only — TTS never affects images)
        _mark_downstream_stale(episode_id, Path(settings.outputs_dir))

        # Update episode status
        episode.status = EpisodeStatus.TTS_DONE
        episode.error_message = None
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
        from btcedu.services.errors import ErrorCategory, PipelineError

        pipeline_run.status = RunStatus.FAILED.value
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(e)
        pipeline_run.estimated_cost_usd = total_cost + float(getattr(e, "cost_usd", 0.0))
        if isinstance(e, PipelineError) and e.category == ErrorCategory.PERMANENT_COST_LIMIT:
            episode.status = EpisodeStatus.COST_LIMIT
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


def _resolve_tts_config(episode, settings: Settings) -> dict:
    """Resolve the profile-only TTS voice/model/params + pronunciation lexicon.

    Every synthesis parameter comes from the profile's ``stage_config.tts`` and
    falls back to ``settings``. The profile ``model`` is honoured (previously the
    global default was used). ``pronunciation_lexicon`` is an optional mapping of
    display text -> spoken substitution applied only to the synthesis text.
    """
    cfg: dict = {}
    try:
        from btcedu.profiles import get_registry as _get_profile_registry

        name = getattr(episode, "content_profile", "bitcoin_podcast") or "bitcoin_podcast"
        profile = _get_profile_registry(settings).get(name)
        cfg = (profile.stage_config.get("tts", {}) if profile else {}) or {}
    except Exception:  # noqa: BLE001 - fall back to settings defaults
        cfg = {}

    lexicon = cfg.get("pronunciation_lexicon") or {}
    if not isinstance(lexicon, dict):
        lexicon = {}
    return {
        "voice_id": cfg.get("voice_id") or settings.elevenlabs_voice_id,
        "model": cfg.get("model") or settings.elevenlabs_model,
        "stability": cfg.get("stability", settings.elevenlabs_stability),
        "similarity_boost": cfg.get("similarity_boost", settings.elevenlabs_similarity_boost),
        "style": cfg.get("style", settings.elevenlabs_style),
        "speed": cfg.get("speed", settings.elevenlabs_speed),
        "use_speaker_boost": cfg.get("use_speaker_boost", settings.elevenlabs_use_speaker_boost),
        "pronunciation_lexicon": {str(k): str(v) for k, v in lexicon.items()},
        "voices": _resolve_role_voices(cfg, settings),
    }


def _resolve_role_voices(cfg: dict, settings: Settings) -> dict:
    """Per-speaker-role voice settings, each falling back to the single voice.

    Profiles without a ``voices`` block get an empty mapping, so their episodes
    keep being synthesized with exactly one voice as before.
    """
    voices = cfg.get("voices")
    if not isinstance(voices, dict) or not voices:
        return {}
    default_voice = cfg.get("voice_id") or settings.elevenlabs_voice_id
    resolved: dict[str, dict] = {}
    for role, raw in voices.items():
        if not isinstance(raw, dict):
            continue
        resolved[str(role)] = {
            "voice_id": raw.get("voice_id") or default_voice,
            "model": raw.get("model") or cfg.get("model") or settings.elevenlabs_model,
            "stability": raw.get("stability", cfg.get("stability", settings.elevenlabs_stability)),
            "similarity_boost": raw.get(
                "similarity_boost",
                cfg.get("similarity_boost", settings.elevenlabs_similarity_boost),
            ),
            "style": raw.get("style", cfg.get("style", settings.elevenlabs_style)),
            "speed": raw.get("speed", cfg.get("speed", settings.elevenlabs_speed)),
            "use_speaker_boost": raw.get(
                "use_speaker_boost",
                cfg.get("use_speaker_boost", settings.elevenlabs_use_speaker_boost),
            ),
            "voice_id_configured": bool(raw.get("voice_id")),
        }
    return resolved


def _voice_config_signature(cfg: dict) -> dict:
    """Subset of TTS config that changes the synthesized audio (for hashing)."""
    return {
        "voice_id": cfg.get("voice_id"),
        "model": cfg.get("model"),
        "stability": cfg.get("stability"),
        "similarity_boost": cfg.get("similarity_boost"),
        "style": cfg.get("style"),
        "speed": cfg.get("speed"),
        "use_speaker_boost": cfg.get("use_speaker_boost"),
        "pronunciation_lexicon": cfg.get("pronunciation_lexicon") or {},
        "voices": cfg.get("voices") or {},
    }


def _apply_pronunciation_lexicon(text: str, lexicon: dict) -> str:
    """Deterministic whole-word substitutions applied to SYNTHESIS text only.

    The display narration (chapters.json / the QA-locked approved text) is never
    modified — only the audio the engine reads uses these substitutions so proper
    nouns and tickers are pronounced correctly. Longest keys are applied first so
    multi-word phrases win over their constituent words.
    """
    if not lexicon or not text:
        return text
    out = text
    for key in sorted(lexicon, key=len, reverse=True):
        if not key:
            continue
        replacement = lexicon[key]
        pattern = re.compile(rf"(?<!\w){re.escape(key)}(?!\w)")
        out = pattern.sub(lambda _m, r=replacement: r, out)
    return out


def _chapter_tts_hash(
    chapter_id: str,
    original_text: str,
    synthesis_text: str,
    voice_sig: dict,
    roles: list[str] | None = None,
) -> str:
    """Per-chapter idempotency hash over text + synthesis text + voice config.

    The speaker roles are part of the hash because they decide which voice a
    chapter is spoken in. Without them a chapter that changes hands between
    presenters keeps the audio of the previous speaker.
    """
    payload = json.dumps(
        {
            "chapter_id": chapter_id,
            "original_text": original_text,
            "synthesis_text": synthesis_text,
            "voice_config": voice_sig,
            "roles": roles or [],
        },
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compute_tts_content_hash(chapters_doc: ChapterDocument, lexicon: dict, voice_sig: dict) -> str:
    """Stage-level idempotency hash: narration + synthesis text + voice config.

    Changing the voice, model, voice params or lexicon changes this hash and so
    forces regeneration (previously only narration text was hashed).
    """
    relevant = {
        "voice_config": voice_sig,
        "chapters": [
            {
                "chapter_id": ch.chapter_id,
                "narration_text": ch.narration.text,
                "synthesis_text": _apply_pronunciation_lexicon(ch.narration.text, lexicon),
            }
            for ch in chapters_doc.chapters
        ],
    }
    content_str = json.dumps(relevant, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(content_str.encode("utf-8")).hexdigest()


def _is_tts_current(
    manifest_path: Path,
    provenance_path: Path,
    chapters_hash: str,
    expected_chapter_ids: set[str] | None = None,
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
        segments = manifest.get("segments", [])
        actual_ids = {segment.get("chapter_id") for segment in segments}
        if expected_chapter_ids is not None and actual_ids != expected_chapter_ids:
            logger.info("TTS manifest chapter coverage is incomplete or stale")
            return False
        for segment in segments:
            mp3_path = base_dir / segment["file_path"]
            if not mp3_path.exists():
                logger.info("MP3 file missing: %s", mp3_path)
                return False
    except (json.JSONDecodeError, KeyError) as e:
        logger.warning("Could not verify TTS manifest: %s", e)
        return False

    return True


def _speaker_segments(chapter) -> list[dict]:
    """Speaker segments of a chapter, or ``[]`` when it names no speaker.

    A chapter spoken by one presenter is returned too: it still says *which*
    presenter, and that decides the voice. Discarding it fell back to the
    profile's main voice, which gave the opening, the closing and the weather to
    the reporter although the anchor presents them.

    Only returned when the segments actually reconstruct the chapter narration,
    so a stale or hand-edited ``metadata`` block can never change what is spoken.
    """
    metadata = getattr(chapter, "metadata", None) or {}
    raw = metadata.get("speaker_segments")
    if not isinstance(raw, list) or not raw:
        return []
    segments = []
    for item in raw:
        if not isinstance(item, dict):
            return []
        text = str(item.get("text") or "").strip()
        role = str(item.get("role") or "").strip()
        if not text or not role:
            return []
        segments.append({"role": role, "purpose": str(item.get("purpose") or ""), "text": text})

    from btcedu.core.narration_lock import normalize_narration_text

    composed = normalize_narration_text(" ".join(s["text"] for s in segments))
    if composed != normalize_narration_text(chapter.narration.text):
        logger.warning(
            "Speaker segments of chapter %s do not reconstruct its narration — "
            "falling back to single-voice synthesis",
            chapter.chapter_id,
        )
        return []
    return segments


def _concat_mp3(parts: list[Path], target: Path, pause_seconds: float) -> None:
    """Join MP3 parts into one file, inserting a short pause between speakers."""
    import shutil
    import subprocess

    if len(parts) == 1:
        shutil.copyfile(parts[0], target)
        return

    inputs: list[str] = []
    for part in parts:
        inputs.extend(["-i", str(part)])
    silence = f"anullsrc=r=44100:cl=mono:d={pause_seconds}"
    filters = []
    for index in range(len(parts)):
        filters.append(f"[{index}:a]aresample=44100,aformat=channel_layouts=mono[a{index}]")
    concat_inputs = ""
    for index in range(len(parts)):
        concat_inputs += f"[a{index}]"
        if index < len(parts) - 1:
            filters.append(f"{silence}[p{index}]")
            concat_inputs += f"[p{index}]"
    segment_count = len(parts) * 2 - 1
    filters.append(f"{concat_inputs}concat=n={segment_count}:v=0:a=1[out]")

    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        *inputs,
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[out]",
        "-codec:a",
        "libmp3lame",
        "-q:a",
        "2",
        str(target),
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not target.exists():
        raise RuntimeError(f"Could not join speaker audio for {target.name}: {result.stderr[:400]}")


_NOISE_FLOOR_WARN_DB = -55.0
_NOISE_MAX_TAKES = 3
_NOISE_FLOOR_WINDOW_SAMPLES = 4410  # 100 ms at 44.1 kHz


def _noise_floor_db(path: Path) -> float | None:
    """Noise floor of an audio file: the 1st percentile of its 100 ms RMS levels.

    ElevenLabs generations are not deterministic. The same text and the same
    voice settings occasionally come back with an audible background hiss, and
    the result is bimodal: a take is either around -80 dB or around -40 dB, with
    nothing in between. Measuring the quiet passages makes that visible; the
    loudness of the speech itself is unaffected, which is why the defect passes
    every level check.

    Returns None when ffmpeg cannot analyse the file - the measurement is
    diagnostic only and must never fail a run.
    """
    import subprocess

    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(path),
                "-af",
                f"asetnsamples=n={_NOISE_FLOOR_WINDOW_SAMPLES}:p=0,"
                "astats=metadata=1:reset=1,"
                "ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        logger.debug("Could not measure the noise floor of %s: %s", path, exc)
        return None

    levels: list[float] = []
    for line in (result.stdout or "").splitlines():
        if "RMS_level=" not in line:
            continue
        try:
            value = float(line.split("=", 1)[1].strip())
        except ValueError:
            continue
        if value > -1000:  # -inf marks a digitally silent window
            levels.append(value)
    if len(levels) < 20:
        return None
    levels.sort()
    return levels[len(levels) // 100]


def _synthesize_clean_take(
    tts_service,
    request,
    target: Path,
    *,
    max_attempts: int,
    noise_floor_max_db: float,
    label: str,
):
    """Synthesize *request*, retrying while the take has an audible noise bed.

    Generation is stochastic: roughly one take in four comes back with a hiss
    that no level or duration check notices. Retrying is the only remedy, so the
    quietest of at most *max_attempts* takes is kept. Every attempt is paid for,
    which is why the number of attempts is small and configurable.

    Returns ``(response, noise_floor_db, attempts)``.
    """
    best_response = None
    best_floor: float | None = None
    attempts = 0

    for attempt in range(1, max(1, max_attempts) + 1):
        attempts = attempt
        response = tts_service.synthesize(request)
        target.write_bytes(response.audio_bytes)
        floor = _noise_floor_db(target)

        if floor is None:
            return response, None, attempts
        if best_floor is None or floor < best_floor:
            best_response, best_floor = response, floor
        if floor <= noise_floor_max_db:
            if attempt > 1:
                logger.info(
                    "%s: take %d is clean (noise floor %.1f dB)", label, attempt, floor
                )
            return response, floor, attempts

        logger.warning(
            "%s: take %d has an audible noise bed (noise floor %.1f dB, "
            "expected below %.0f dB)%s",
            label,
            attempt,
            floor,
            noise_floor_max_db,
            " — retrying" if attempt < max(1, max_attempts) else "",
        )

    if best_response is not None:
        target.write_bytes(best_response.audio_bytes)
        logger.warning(
            "%s: keeping the quietest of %d takes (noise floor %.1f dB)",
            label,
            attempts,
            best_floor,
        )
    return best_response, best_floor, attempts


def _generate_multi_voice_audio(
    chapter,
    segments: list[dict],
    tts_service,
    output_dir: Path,
    settings: Settings,
    *,
    role_voices: dict,
    fallback: dict,
    lexicon: dict,
    text_hash: str,
    pause_seconds: float = 0.35,
) -> AudioEntry:
    """Synthesize a chapter with one voice per speaker segment.

    Each segment is rendered with its role's voice and the parts are joined into
    the single chapter MP3 the renderer expects. Roles without a configured
    voice fall back to the profile's main voice, so an incomplete configuration
    degrades to today's behaviour instead of failing.
    """
    from btcedu.services.elevenlabs_service import TTSRequest

    parts_dir = output_dir / "parts"
    parts_dir.mkdir(parents=True, exist_ok=True)
    mp3_filename = f"{chapter.chapter_id}.mp3"
    mp3_path = output_dir / mp3_filename

    part_paths: list[Path] = []
    part_meta: list[dict] = []
    total_cost = 0.0
    total_duration = 0.0
    used_fallback: set[str] = set()

    for index, segment in enumerate(segments):
        role = segment["role"]
        voice = role_voices.get(role) or fallback
        if not voice.get("voice_id_configured", True):
            used_fallback.add(role)
        spoken = _apply_pronunciation_lexicon(segment["text"], lexicon)
        part_path = parts_dir / f"{chapter.chapter_id}_{index:02d}_{role}.mp3"

        if settings.dry_run:
            part_path.write_bytes(_create_silent_mp3())
            duration = 0.0
            cost = 0.0
            model = voice.get("model")
            noise_floor = None
            takes = 1
        else:
            response, noise_floor, takes = _synthesize_clean_take(
                tts_service,
                TTSRequest(
                    text=spoken,
                    voice_id=voice.get("voice_id") or settings.elevenlabs_voice_id,
                    model=voice.get("model") or settings.elevenlabs_model,
                    stability=voice.get("stability"),
                    similarity_boost=voice.get("similarity_boost"),
                    style=voice.get("style"),
                    use_speaker_boost=voice.get("use_speaker_boost"),
                    speed=voice.get("speed"),
                ),
                part_path,
                max_attempts=int(voice.get("noise_retries", _NOISE_MAX_TAKES)),
                noise_floor_max_db=float(
                    voice.get("noise_floor_max_db", _NOISE_FLOOR_WARN_DB)
                ),
                label=f"Chapter {chapter.chapter_id} part {index:02d} ({role})",
            )
            duration = response.duration_seconds
            cost = response.cost_usd * takes
            model = response.model

        part_paths.append(part_path)
        part_meta.append(
            {
                "role": role,
                "purpose": segment["purpose"],
                "voice_id": voice.get("voice_id"),
                "characters": len(segment["text"]),
                "duration_seconds": round(duration, 3),
                "file": part_path.name,
                "noise_floor_db": None if noise_floor is None else round(noise_floor, 1),
                "takes": takes,
            }
        )
        total_cost += cost
        total_duration += duration

    if settings.dry_run:
        # The placeholder frames are not a decodable stream, so joining them
        # with ffmpeg would fail. A single placeholder stands in for the chapter.
        mp3_path.write_bytes(_create_silent_mp3())
    else:
        _concat_mp3(part_paths, mp3_path, pause_seconds)
        if len(part_paths) > 1:
            total_duration += pause_seconds * (len(part_paths) - 1)

    if used_fallback:
        logger.warning(
            "Chapter %s: no voice configured for role(s) %s — used the main voice",
            chapter.chapter_id,
            ", ".join(sorted(used_fallback)),
        )

    logger.info(
        "Generated multi-voice TTS for chapter %s: %d segments, %.1fs, $%.3f",
        chapter.chapter_id,
        len(segments),
        total_duration,
        total_cost,
    )

    return AudioEntry(
        chapter_id=chapter.chapter_id,
        chapter_title=chapter.title,
        text_length=len(chapter.narration.text),
        text_hash=text_hash,
        duration_seconds=round(total_duration, 3),
        file_path=f"tts/{mp3_filename}",
        sample_rate=44100,
        model=str(model or settings.elevenlabs_model),
        voice_id=",".join(sorted({str(p["voice_id"]) for p in part_meta})),
        mime_type="audio/mpeg",
        size_bytes=mp3_path.stat().st_size if mp3_path.exists() else 0,
        cost_usd=total_cost,
        metadata={
            "generated_at": _utcnow().isoformat(),
            "multi_voice": True,
            "dry_run": settings.dry_run,
            "speaker_parts": part_meta,
            "pause_seconds": pause_seconds,
        },
    )


def _generate_single_audio(
    chapter,
    tts_service,
    output_dir: Path,
    settings: Settings,
    *,
    voice_id: str,
    model: str,
    stability: float | None = None,
    similarity_boost: float | None = None,
    style: float | None = None,
    speed: float | None = None,
    use_speaker_boost: bool | None = None,
    synthesis_text: str | None = None,
    text_hash: str | None = None,
) -> AudioEntry:
    """Generate audio for a single chapter.

    ``synthesis_text`` is what the engine actually speaks (after any
    pronunciation-lexicon substitutions); the display narration length/hash is
    derived from the original chapter narration so the manifest stays aligned
    with the locked text. In dry-run mode, writes a silent MP3 placeholder.
    """
    from btcedu.services.elevenlabs_service import TTSRequest

    narration_text = chapter.narration.text
    spoken_text = synthesis_text if synthesis_text is not None else narration_text
    if text_hash is None:
        text_hash = _compute_narration_hash(narration_text)
    lexicon_applied = spoken_text != narration_text
    mp3_filename = f"{chapter.chapter_id}.mp3"
    mp3_path = output_dir / mp3_filename

    # Use provided overrides or fall back to settings
    effective_voice_id = voice_id or settings.elevenlabs_voice_id
    effective_model = model or settings.elevenlabs_model
    effective_stability = stability if stability is not None else settings.elevenlabs_stability
    effective_similarity = (
        similarity_boost if similarity_boost is not None else settings.elevenlabs_similarity_boost
    )
    effective_style = style if style is not None else settings.elevenlabs_style
    effective_speed = speed if speed is not None else settings.elevenlabs_speed
    effective_speaker_boost = (
        use_speaker_boost
        if use_speaker_boost is not None
        else settings.elevenlabs_use_speaker_boost
    )

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
            model=effective_model,
            voice_id=effective_voice_id,
            mime_type="audio/mpeg",
            size_bytes=len(silent_mp3),
            cost_usd=0.0,
            metadata={"dry_run": True, "lexicon_applied": lexicon_applied},
        )

    # Call TTS service (speaks the synthesis text with the profile model/params)
    request = TTSRequest(
        text=spoken_text,
        voice_id=effective_voice_id,
        model=effective_model,
        stability=effective_stability,
        similarity_boost=effective_similarity,
        style=effective_style,
        use_speaker_boost=effective_speaker_boost,
        speed=effective_speed,
    )

    response, noise_floor, takes = _synthesize_clean_take(
        tts_service,
        request,
        mp3_path,
        max_attempts=_NOISE_MAX_TAKES,
        noise_floor_max_db=_NOISE_FLOOR_WARN_DB,
        label=f"Chapter {chapter.chapter_id}",
    )

    size_bytes = mp3_path.stat().st_size

    logger.info(
        "Generated TTS for chapter %s: %.1fs, %d chars, $%.3f",
        chapter.chapter_id,
        response.duration_seconds,
        response.character_count,
        response.cost_usd * takes,
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
        cost_usd=response.cost_usd * takes,
        metadata={
            "generated_at": _utcnow().isoformat(),
            "lexicon_applied": lexicon_applied,
            "noise_floor_db": None if noise_floor is None else round(noise_floor, 1),
            "takes": takes,
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
    """Get cumulative cost for all pipeline runs for this episode.

    ``PipelineRun.episode_id`` is an integer FK to ``episodes.id``; resolve the
    string ``episode_id`` to that integer so the cost guard counts every prior
    stage rather than nothing.
    """
    from sqlalchemy import func

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if episode is None:
        return 0.0
    return float(
        session.query(func.coalesce(func.sum(PipelineRun.estimated_cost_usd), 0.0))
        .filter(PipelineRun.episode_id == episode.id)
        .scalar()
        or 0.0
    )
