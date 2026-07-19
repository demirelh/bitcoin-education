"""Tests for structured transcription schemas and provider abstraction."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError

from btcedu.config import Settings
from btcedu.models.transcript_schema import (
    TranscriptDocument,
    TranscriptSegment,
    TranscriptUsage,
    make_segment_id,
)
from btcedu.services.transcription_service import (
    OpenAITranscriptionProvider,
    ProviderTranscript,
    ProviderTranscriptSegment,
    resolve_transcription_config,
    transcribe_audio_structured,
)


def test_transcript_document_validates_schema():
    document = TranscriptDocument(
        episode_id="episode-1",
        provider="openai",
        model="whisper-1",
        language="de",
        text="Guten Abend.",
        segments=[
            TranscriptSegment(
                segment_id="seg-0001",
                start_seconds=1.2,
                end_seconds=2.4,
                text="Guten Abend.",
                confidence=None,
            )
        ],
        usage=TranscriptUsage(audio_seconds=2.4, cost_usd=0.00024),
    )

    assert document.schema_version == 1
    assert document.segments[0].start_seconds == 1.2


def test_transcript_document_rejects_unstable_segment_ids():
    with pytest.raises(ValidationError, match="segment IDs must be"):
        TranscriptDocument(
            episode_id="episode-1",
            provider="openai",
            model="whisper-1",
            language="de",
            text="Text",
            segments=[
                TranscriptSegment(
                    segment_id="seg-0002",
                    start_seconds=0,
                    end_seconds=1,
                    text="Text",
                )
            ],
        )


def test_make_segment_id_is_stable():
    assert [make_segment_id(index) for index in range(3)] == [
        "seg-0001",
        "seg-0002",
        "seg-0003",
    ]


def test_profile_transcription_config_overrides_settings():
    settings = Settings(
        whisper_model="whisper-1",
        transcription_primary_provider="openai",
        transcription_secondary_enabled=False,
    )
    config = resolve_transcription_config(
        settings,
        {
            "primary": {"provider": "openai", "model": "primary-model"},
            "secondary": {
                "enabled": True,
                "provider": "openai",
                "model": "secondary-model",
                "mode": "suspicious_segments_only",
                "minimum_severity": "major",
            },
            "suspicious_segment_context_seconds": 12,
            "max_secondary_audio_seconds": 240,
        },
    )

    assert config.primary.model == "primary-model"
    assert config.secondary.enabled is True
    assert config.secondary.model == "secondary-model"
    assert config.secondary_minimum_severity == "major"
    assert config.suspicious_segment_context_seconds == 12
    assert config.max_secondary_audio_seconds == 240


@patch("btcedu.services.transcription_service.OpenAI")
def test_openai_provider_returns_segments_and_cost(mock_openai, tmp_path):
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"audio")
    response = MagicMock()
    response.model_dump.return_value = {
        "text": "Guten Abend.",
        "duration": 60,
        "segments": [
            {
                "start": 0,
                "end": 3,
                "text": "Guten Abend.",
                "avg_logprob": -0.1,
            }
        ],
    }
    mock_openai.return_value.audio.transcriptions.create.return_value = response
    provider = OpenAITranscriptionProvider("sk-test", cost_per_minute_usd=0.006)

    result = provider.transcribe(
        str(audio_path),
        model="whisper-1",
        language="de",
    )

    assert result.audio_seconds == 60
    assert result.cost_usd == pytest.approx(0.006)
    assert result.segments[0].confidence == pytest.approx(0.9048, rel=1e-3)
    call = mock_openai.return_value.audio.transcriptions.create.call_args.kwargs
    assert call["response_format"] == "verbose_json"
    assert call["model"] == "whisper-1"


class _FakeAudio:
    def __init__(self, duration_ms: int) -> None:
        self.duration_ms = duration_ms

    def __len__(self) -> int:
        return self.duration_ms

    def __getitem__(self, item):
        return _FakeAudio(item.stop - item.start)

    def export(self, path: str, format: str) -> None:
        assert format == "mp3"
        with open(path, "wb") as output:
            output.write(b"chunk")


@patch("btcedu.services.transcription_service.OpenAI")
def test_openai_transcribe_model_uses_json_and_local_duration(mock_openai, tmp_path):
    audio_path = tmp_path / "audio.mp3"
    audio_path.write_bytes(b"audio")
    response = MagicMock()
    response.model_dump.return_value = {"text": "Guten Abend."}
    mock_openai.return_value.audio.transcriptions.create.return_value = response
    provider = OpenAITranscriptionProvider("sk-test", cost_per_minute_usd=0.006)

    with patch("pydub.AudioSegment.from_file", return_value=_FakeAudio(30_000)):
        result = provider.transcribe(
            str(audio_path),
            model="gpt-4o-mini-transcribe",
            language="de",
        )

    call = mock_openai.return_value.audio.transcriptions.create.call_args.kwargs
    assert call["response_format"] == "json"
    assert "timestamp_granularities" not in call
    assert result.audio_seconds == 30
    assert result.cost_usd == pytest.approx(0.003)
    assert result.segments[0].end_seconds == 30


def test_chunked_structured_transcription_offsets_timestamps(tmp_path):
    audio_path = tmp_path / "large.mp3"
    audio_path.write_bytes(b"x" * (2 * 1024 * 1024 + 1))
    provider = SimpleNamespace(
        name="openai",
        transcribe=MagicMock(
            return_value=ProviderTranscript(
                text="Teil.",
                segments=[
                    ProviderTranscriptSegment(
                        start_seconds=0.1,
                        end_seconds=0.9,
                        text="Teil.",
                        confidence=0.8,
                    )
                ],
                audio_seconds=1,
                cost_usd=0.001,
            )
        ),
    )

    with patch("pydub.AudioSegment.from_file", return_value=_FakeAudio(3000)):
        document = transcribe_audio_structured(
            str(audio_path),
            episode_id="episode-1",
            provider=provider,
            model="whisper-1",
            language="de",
            max_chunk_mb=1,
        )

    assert [segment.segment_id for segment in document.segments] == [
        "seg-0001",
        "seg-0002",
        "seg-0003",
    ]
    assert [segment.start_seconds for segment in document.segments] == [0.1, 1.1, 2.1]
    assert document.usage.audio_seconds == 3
    assert document.usage.cost_usd == 0.003
