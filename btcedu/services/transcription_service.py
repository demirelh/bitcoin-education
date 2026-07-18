"""Transcription providers and backward-compatible Whisper helpers."""

from __future__ import annotations

import logging
import math
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from openai import OpenAI

from btcedu.models.transcript_schema import (
    TranscriptDocument,
    TranscriptSegment,
    TranscriptUsage,
    make_segment_id,
)
from btcedu.services.retry import retry_on_transient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TranscriptionProviderSpec:
    """Configured provider and model for one transcription role."""

    provider: str
    model: str


@dataclass(frozen=True)
class SecondaryTranscriptionSpec:
    """Optional secondary transcription configuration for later verification."""

    enabled: bool
    provider: str
    model: str
    mode: str


@dataclass(frozen=True)
class ResolvedTranscriptionConfig:
    """Effective primary/secondary transcription configuration."""

    primary: TranscriptionProviderSpec
    secondary: SecondaryTranscriptionSpec
    suspicious_segment_context_seconds: float
    max_secondary_audio_seconds: float


@dataclass(frozen=True)
class ProviderTranscriptSegment:
    """Provider-neutral segment before stable IDs are assigned."""

    start_seconds: float
    end_seconds: float
    text: str
    confidence: float | None = None


@dataclass(frozen=True)
class ProviderTranscript:
    """Provider-neutral transcription response."""

    text: str
    segments: list[ProviderTranscriptSegment]
    audio_seconds: float
    cost_usd: float


class TranscriptionProvider(Protocol):
    """Small provider contract used by the transcription stage."""

    name: str

    def transcribe(
        self,
        audio_path: str,
        *,
        model: str,
        language: str,
    ) -> ProviderTranscript:
        """Transcribe one audio file into timestamped segments."""


class OpenAITranscriptionProvider:
    """OpenAI transcription provider using verbose JSON segment timestamps."""

    name = "openai"

    def __init__(self, api_key: str, cost_per_minute_usd: float) -> None:
        self._api_key = api_key
        self._cost_per_minute_usd = cost_per_minute_usd

    @retry_on_transient(max_retries=3, base_delay=1.0)
    def transcribe(
        self,
        audio_path: str,
        *,
        model: str,
        language: str,
    ) -> ProviderTranscript:
        client = OpenAI(api_key=self._api_key)
        with open(audio_path, "rb") as audio_file:
            response = client.audio.transcriptions.create(
                model=model,
                file=audio_file,
                language=language,
                response_format="verbose_json",
                timestamp_granularities=["segment"],
            )

        data = _response_to_dict(response)
        text = str(data.get("text") or getattr(response, "text", "") or "").strip()
        raw_segments = data.get("segments") or getattr(response, "segments", None) or []
        segments = [
            segment
            for raw_segment in raw_segments
            if (segment := _parse_provider_segment(raw_segment)) is not None
        ]

        response_duration = data.get("duration", getattr(response, "duration", None))
        audio_seconds = _coerce_non_negative_float(response_duration)
        if audio_seconds == 0 and segments:
            audio_seconds = max(segment.end_seconds for segment in segments)

        if not segments and text:
            segments = [
                ProviderTranscriptSegment(
                    start_seconds=0,
                    end_seconds=audio_seconds,
                    text=text,
                )
            ]

        cost_usd = (audio_seconds / 60) * self._cost_per_minute_usd
        return ProviderTranscript(
            text=text,
            segments=segments,
            audio_seconds=audio_seconds,
            cost_usd=cost_usd,
        )


def resolve_transcription_config(
    settings,
    profile_config: dict | None = None,
) -> ResolvedTranscriptionConfig:
    """Resolve profile transcription settings over global defaults."""
    config = profile_config or {}
    primary = config.get("primary") or {}
    secondary = config.get("secondary") or {}

    primary_provider = str(
        primary.get("provider") or getattr(settings, "transcription_primary_provider", "openai")
    )
    primary_model = str(
        primary.get("model")
        or getattr(settings, "transcription_primary_model", "")
        or getattr(settings, "whisper_model")
    )

    secondary_provider = str(
        secondary.get("provider") or getattr(settings, "transcription_secondary_provider", "openai")
    )
    secondary_model = str(
        secondary.get("model")
        or getattr(settings, "transcription_secondary_model", "")
        or primary_model
    )

    return ResolvedTranscriptionConfig(
        primary=TranscriptionProviderSpec(
            provider=primary_provider,
            model=primary_model,
        ),
        secondary=SecondaryTranscriptionSpec(
            enabled=bool(
                secondary.get(
                    "enabled",
                    getattr(settings, "transcription_secondary_enabled", False),
                )
            ),
            provider=secondary_provider,
            model=secondary_model,
            mode=str(
                secondary.get("mode")
                or getattr(
                    settings,
                    "transcription_secondary_mode",
                    "suspicious_segments_only",
                )
            ),
        ),
        suspicious_segment_context_seconds=float(
            config.get(
                "suspicious_segment_context_seconds",
                getattr(settings, "transcription_suspicious_segment_context_seconds", 15),
            )
        ),
        max_secondary_audio_seconds=float(
            config.get(
                "max_secondary_audio_seconds",
                getattr(settings, "transcription_max_secondary_audio_seconds", 300),
            )
        ),
    )


def get_transcription_provider(
    provider: str,
    *,
    api_key: str,
    openai_cost_per_minute_usd: float,
) -> TranscriptionProvider:
    """Create the configured transcription provider."""
    normalized = provider.strip().lower()
    if normalized == "openai":
        return OpenAITranscriptionProvider(api_key, openai_cost_per_minute_usd)
    raise ValueError(f"Unsupported transcription provider: {provider}")


def transcribe_audio_structured(
    audio_path: str,
    *,
    episode_id: str,
    provider: TranscriptionProvider,
    model: str,
    language: str,
    max_chunk_mb: int = 24,
) -> TranscriptDocument:
    """Transcribe audio and preserve provider timestamps across local chunks."""
    file_size_mb = Path(audio_path).stat().st_size / (1024 * 1024)
    if file_size_mb <= max_chunk_mb:
        result = provider.transcribe(audio_path, model=model, language=language)
    else:
        logger.info("Audio %.1f MB > %d MB limit, splitting...", file_size_mb, max_chunk_mb)
        result = _transcribe_chunked_structured(
            audio_path,
            provider=provider,
            model=model,
            language=language,
            max_chunk_mb=max_chunk_mb,
        )

    segments = [
        TranscriptSegment(
            segment_id=make_segment_id(index),
            start_seconds=round(segment.start_seconds, 3),
            end_seconds=round(segment.end_seconds, 3),
            text=segment.text.strip(),
            confidence=segment.confidence,
        )
        for index, segment in enumerate(result.segments)
        if segment.text.strip()
    ]
    text = result.text.strip() or " ".join(segment.text for segment in segments).strip()

    return TranscriptDocument(
        episode_id=episode_id,
        provider=provider.name,
        model=model,
        language=language,
        text=text,
        segments=segments,
        usage=TranscriptUsage(
            audio_seconds=round(result.audio_seconds, 3),
            cost_usd=round(result.cost_usd, 6),
        ),
    )


def transcribe_audio(
    audio_path: str,
    api_key: str,
    model: str = "whisper-1",
    language: str = "de",
    max_chunk_mb: int = 24,
) -> str:
    """Backward-compatible text-only OpenAI Whisper transcription."""
    file_size_mb = Path(audio_path).stat().st_size / (1024 * 1024)

    if file_size_mb <= max_chunk_mb:
        return _transcribe_single(audio_path, api_key, model, language)

    logger.info("Audio %.1f MB > %d MB limit, splitting...", file_size_mb, max_chunk_mb)
    return _transcribe_chunked(audio_path, api_key, model, language, max_chunk_mb)


@retry_on_transient(max_retries=3, base_delay=1.0)
def _transcribe_single(
    audio_path: str,
    api_key: str,
    model: str,
    language: str,
) -> str:
    """Transcribe a single audio file using the legacy text response."""
    client = OpenAI(api_key=api_key)
    with open(audio_path, "rb") as audio_file:
        response = client.audio.transcriptions.create(
            model=model,
            file=audio_file,
            language=language,
            response_format="text",
        )
    return response.strip()


def _transcribe_chunked(
    audio_path: str,
    api_key: str,
    model: str,
    language: str,
    max_chunk_mb: int,
) -> str:
    """Split audio and concatenate legacy text responses."""
    from pydub import AudioSegment

    audio = AudioSegment.from_file(audio_path)
    chunks = _split_audio(audio, audio_path, max_chunk_mb)
    tmp_dir = Path(audio_path).parent / "_whisper_tmp"
    tmp_dir.mkdir(exist_ok=True)

    parts: list[str] = []
    try:
        for index, (_, _, chunk) in enumerate(chunks):
            tmp_path = tmp_dir / f"segment_{index:03d}.mp3"
            chunk.export(str(tmp_path), format="mp3")
            logger.info("Transcribing segment %d/%d...", index + 1, len(chunks))
            parts.append(_transcribe_single(str(tmp_path), api_key, model, language))
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=False)

    return "\n\n".join(parts)


def _transcribe_chunked_structured(
    audio_path: str,
    *,
    provider: TranscriptionProvider,
    model: str,
    language: str,
    max_chunk_mb: int,
) -> ProviderTranscript:
    """Transcribe local chunks and convert relative times to absolute times."""
    from pydub import AudioSegment

    audio = AudioSegment.from_file(audio_path)
    chunks = _split_audio(audio, audio_path, max_chunk_mb)
    tmp_dir = Path(audio_path).parent / "_whisper_tmp"
    tmp_dir.mkdir(exist_ok=True)

    text_parts: list[str] = []
    merged_segments: list[ProviderTranscriptSegment] = []
    total_cost = 0.0
    try:
        for index, (start_ms, _, chunk) in enumerate(chunks):
            tmp_path = tmp_dir / f"segment_{index:03d}.mp3"
            chunk.export(str(tmp_path), format="mp3")
            logger.info("Transcribing segment %d/%d...", index + 1, len(chunks))
            result = provider.transcribe(str(tmp_path), model=model, language=language)
            offset_seconds = start_ms / 1000
            text_parts.append(result.text)
            total_cost += result.cost_usd
            merged_segments.extend(
                ProviderTranscriptSegment(
                    start_seconds=segment.start_seconds + offset_seconds,
                    end_seconds=segment.end_seconds + offset_seconds,
                    text=segment.text,
                    confidence=segment.confidence,
                )
                for segment in result.segments
            )
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=False)

    return ProviderTranscript(
        text="\n\n".join(part for part in text_parts if part).strip(),
        segments=merged_segments,
        audio_seconds=len(audio) / 1000,
        cost_usd=total_cost,
    )


def _split_audio(audio, audio_path: str, max_chunk_mb: int) -> list[tuple[int, int, object]]:
    """Return `(start_ms, end_ms, audio_chunk)` tuples."""
    duration_ms = len(audio)
    file_size_mb = Path(audio_path).stat().st_size / (1024 * 1024)
    chunk_count = max(1, math.ceil(file_size_mb / max_chunk_mb))
    chunk_ms = max(1, math.ceil(duration_ms / chunk_count))

    chunks = []
    for index in range(chunk_count):
        start_ms = index * chunk_ms
        end_ms = min((index + 1) * chunk_ms, duration_ms)
        if start_ms >= end_ms:
            break
        chunks.append((start_ms, end_ms, audio[start_ms:end_ms]))
    return chunks


def _response_to_dict(response) -> dict:
    if hasattr(response, "model_dump"):
        data = response.model_dump()
        if isinstance(data, dict):
            return data
    if isinstance(response, dict):
        return response
    return {}


def _parse_provider_segment(raw_segment) -> ProviderTranscriptSegment | None:
    data = raw_segment if isinstance(raw_segment, dict) else _response_to_dict(raw_segment)
    if not data:
        data = {
            "start": getattr(raw_segment, "start", None),
            "end": getattr(raw_segment, "end", None),
            "text": getattr(raw_segment, "text", None),
            "avg_logprob": getattr(raw_segment, "avg_logprob", None),
            "confidence": getattr(raw_segment, "confidence", None),
        }

    text = str(data.get("text") or "").strip()
    if not text:
        return None
    start_seconds = _coerce_non_negative_float(data.get("start"))
    end_seconds = _coerce_non_negative_float(data.get("end"))
    confidence = _segment_confidence(data)
    return ProviderTranscriptSegment(
        start_seconds=start_seconds,
        end_seconds=max(start_seconds, end_seconds),
        text=text,
        confidence=confidence,
    )


def _segment_confidence(data: dict) -> float | None:
    confidence = data.get("confidence")
    if isinstance(confidence, (int, float)):
        return min(1.0, max(0.0, float(confidence)))
    avg_logprob = data.get("avg_logprob")
    if isinstance(avg_logprob, (int, float)):
        return min(1.0, max(0.0, math.exp(float(avg_logprob))))
    return None


def _coerce_non_negative_float(value) -> float:
    if not isinstance(value, (int, float)):
        return 0.0
    return max(0.0, float(value))


def clean_transcript(raw_text: str) -> str:
    """Basic transcript cleanup: normalize whitespace, strip artifacts."""
    import re

    text = re.sub(r"\n{3,}", "\n\n", raw_text)
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(lines).strip()
