"""Core logic for the transcription pipeline stage."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import (
    Episode,
    EpisodeStatus,
    PipelineRun,
    PipelineStage,
    RunStatus,
)
from btcedu.models.transcript_schema import (
    TranscriptDocument,
    TranscriptSegment,
    TranscriptUsage,
    make_segment_id,
)

logger = logging.getLogger(__name__)

STRUCTURED_TRANSCRIPT_FILENAME = "transcript.structured.de.json"


def _utcnow() -> datetime:
    return datetime.now(UTC)


def transcribe_episode(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> str:
    """Transcribe audio while preserving existing text artifact paths."""
    from btcedu.services.transcription_service import (
        clean_transcript,
        get_transcription_provider,
        resolve_transcription_config,
        transcribe_audio_structured,
    )

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    if episode.status not in (EpisodeStatus.DOWNLOADED, EpisodeStatus.TRANSCRIBED) and not force:
        raise ValueError(
            f"Episode {episode_id} is in status '{episode.status.value}', "
            "expected 'downloaded'. Use --force to override."
        )

    transcript_dir = Path(settings.transcripts_dir) / episode_id
    raw_path = transcript_dir / "transcript.de.txt"
    clean_path = transcript_dir / "transcript.clean.de.txt"
    structured_path = transcript_dir / STRUCTURED_TRANSCRIPT_FILENAME

    if clean_path.exists() and not force:
        logger.info("Transcript exists: %s (use --force to re-transcribe)", clean_path)
        if episode.status == EpisodeStatus.DOWNLOADED:
            episode.transcript_path = str(clean_path)
            episode.status = EpisodeStatus.TRANSCRIBED
            session.commit()
        return str(clean_path)

    if not episode.audio_path:
        raise ValueError(f"No audio file for episode {episode_id}")

    profile_config = _load_transcription_profile_config(settings, episode)
    resolved = resolve_transcription_config(settings, profile_config)
    api_key = _provider_api_key(settings, resolved.primary.provider)
    provider = get_transcription_provider(
        resolved.primary.provider,
        api_key=api_key,
        openai_cost_per_minute_usd=settings.transcription_openai_cost_per_minute_usd,
    )

    pipeline_run = PipelineRun(
        episode_id=episode.id,
        stage=PipelineStage.TRANSCRIBE,
        status=RunStatus.RUNNING,
    )
    session.add(pipeline_run)
    session.flush()
    started = time.monotonic()

    try:
        document = transcribe_audio_structured(
            episode.audio_path,
            episode_id=episode_id,
            provider=provider,
            model=resolved.primary.model,
            language=settings.whisper_language,
            max_chunk_mb=settings.max_audio_chunk_mb,
        )
        cleaned = clean_transcript(document.text)

        transcript_dir.mkdir(parents=True, exist_ok=True)
        raw_path.write_text(document.text, encoding="utf-8")
        clean_path.write_text(cleaned, encoding="utf-8")
        structured_path.write_text(
            json.dumps(document.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        config_hash = _transcription_config_hash(resolved)
        provenance_path = (
            Path(settings.outputs_dir) / episode_id / "provenance" / "transcribe_provenance.json"
        )
        provenance_path.parent.mkdir(parents=True, exist_ok=True)
        provenance_path.write_text(
            json.dumps(
                {
                    "stage": "transcribe",
                    "episode_id": episode_id,
                    "timestamp": _utcnow().isoformat(),
                    "provider": document.provider,
                    "model": document.model,
                    "config_hash": config_hash,
                    "input_files": [episode.audio_path],
                    "output_files": [
                        str(raw_path),
                        str(clean_path),
                        str(structured_path),
                    ],
                    "segment_count": len(document.segments),
                    "audio_seconds": document.usage.audio_seconds,
                    "cost_usd": document.usage.cost_usd,
                    "duration_seconds": round(time.monotonic() - started, 2),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        session.add(
            ContentArtifact(
                episode_id=episode_id,
                artifact_type="transcript",
                file_path=str(structured_path),
                model=f"{document.provider}/{document.model}",
                prompt_hash=config_hash,
                retrieval_snapshot_path=None,
            )
        )

        _mark_analysis_stale(settings, episode_id)
        episode.transcript_path = str(clean_path)
        episode.status = EpisodeStatus.TRANSCRIBED
        episode.error_message = None
        pipeline_run.status = RunStatus.SUCCESS
        pipeline_run.completed_at = _utcnow()
        pipeline_run.estimated_cost_usd = document.usage.cost_usd
        session.commit()

        logger.info(
            "Structured transcript saved for %s (%d segments, $%.4f)",
            episode_id,
            len(document.segments),
            document.usage.cost_usd,
        )
        return str(clean_path)
    except Exception as exc:
        pipeline_run.status = RunStatus.FAILED
        pipeline_run.completed_at = _utcnow()
        pipeline_run.error_message = str(exc)[:1000]
        episode.error_message = str(exc)
        session.commit()
        raise


def load_transcript_document(settings: Settings, episode_id: str) -> TranscriptDocument:
    """Load structured transcript or synthesize a deterministic legacy view."""
    transcript_dir = Path(settings.transcripts_dir) / episode_id
    structured_path = transcript_dir / STRUCTURED_TRANSCRIPT_FILENAME
    if structured_path.exists():
        try:
            return TranscriptDocument.model_validate_json(
                structured_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise ValueError(
                f"Invalid structured transcript for episode {episode_id}: {structured_path}"
            ) from exc

    clean_path = transcript_dir / "transcript.clean.de.txt"
    if not clean_path.exists():
        raw_path = transcript_dir / "transcript.de.txt"
        if not raw_path.exists():
            raise FileNotFoundError(f"Transcript not found for episode {episode_id}")
        clean_path = raw_path

    text = clean_path.read_text(encoding="utf-8").strip()
    parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+|\n+", text) if part.strip()]
    segments = [
        TranscriptSegment(
            segment_id=make_segment_id(index),
            start_seconds=0,
            end_seconds=0,
            text=part,
            confidence=None,
        )
        for index, part in enumerate(parts)
    ]
    return TranscriptDocument(
        episode_id=episode_id,
        provider="legacy",
        model="legacy-text",
        language=settings.whisper_language,
        text=text,
        segments=segments,
        usage=TranscriptUsage(),
    )


def _load_transcription_profile_config(settings: Settings, episode: Episode) -> dict:
    from btcedu.profiles import get_registry

    profile = get_registry(settings).get(episode.content_profile)
    return profile.stage_config.get("transcription", {}) or {}


def _provider_api_key(settings: Settings, provider: str) -> str:
    if provider.strip().lower() == "openai":
        api_key = settings.effective_whisper_api_key
        if not api_key:
            raise ValueError(
                "No OpenAI transcription key configured. Set WHISPER_API_KEY or OPENAI_API_KEY."
            )
        return api_key
    raise ValueError(f"Unsupported transcription provider: {provider}")


def _transcription_config_hash(config) -> str:
    payload = json.dumps(
        {
            "primary": {
                "provider": config.primary.provider,
                "model": config.primary.model,
            },
            "secondary": {
                "enabled": config.secondary.enabled,
                "provider": config.secondary.provider,
                "model": config.secondary.model,
                "mode": config.secondary.mode,
            },
            "suspicious_segment_context_seconds": (config.suspicious_segment_context_seconds),
            "max_secondary_audio_seconds": config.max_secondary_audio_seconds,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mark_analysis_stale(settings: Settings, episode_id: str) -> None:
    analysis_path = (
        Path(settings.outputs_dir) / episode_id / "transcript" / "transcript_analysis.json"
    )
    if not analysis_path.exists():
        return
    stale_path = analysis_path.with_name(analysis_path.name + ".stale")
    stale_path.write_text(
        json.dumps(
            {
                "stale": True,
                "reason": "transcript changed",
                "invalidated_by": "transcribe",
                "at": _utcnow().isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
