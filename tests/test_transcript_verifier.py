"""Tests for selective secondary transcript verification."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from btcedu.config import Settings
from btcedu.core.transcript_analyzer import analyze_transcript
from btcedu.core.transcript_verifier import (
    build_verification_regions,
    compare_transcripts,
    verify_transcript,
)
from btcedu.models.episode import (
    Episode,
    EpisodeStatus,
    PipelineRun,
    PipelineStage,
    RunStatus,
)
from btcedu.models.transcript_schema import (
    SuspiciousTranscriptSegment,
    TranscriptAnalysisDocument,
    TranscriptAnalysisSummary,
    TranscriptDocument,
    TranscriptSegment,
    TranscriptUsage,
    TranscriptVerificationDocument,
)
from btcedu.profiles import reset_registry
from btcedu.services.errors import PipelineError
from btcedu.services.transcription_service import ProviderTranscript


@pytest.fixture(autouse=True)
def _reset_profiles():
    reset_registry()
    yield
    reset_registry()


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "transcripts_dir": str(tmp_path / "transcripts"),
        "outputs_dir": str(tmp_path / "outputs"),
        "profiles_dir": str(Path(__file__).parent.parent / "btcedu" / "profiles"),
        "openai_api_key": "test-key",
        "max_episode_cost_usd": 10.0,
    }
    values.update(overrides)
    return Settings(**values)


def _segment(segment_id: str, start: float, end: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=segment_id,
        start_seconds=start,
        end_seconds=end,
        text=text,
        confidence=0.8,
    )


def _document(episode_id: str = "episode-1") -> TranscriptDocument:
    segments = [
        _segment("seg-0001", 5, 10, "Am 16. Juli wurden 20 Menschen verletzt."),
        _segment("seg-0002", 12, 18, "Die Behörden bestätigten den Bericht nicht."),
        _segment("seg-0003", 70, 75, "Ein weiterer vollständiger Satz."),
    ]
    return TranscriptDocument(
        episode_id=episode_id,
        provider="openai",
        model="whisper-1",
        language="de",
        text=" ".join(segment.text for segment in segments),
        segments=segments,
        usage=TranscriptUsage(audio_seconds=100, cost_usd=0.01),
    )


def _analysis(
    document: TranscriptDocument,
    *segment_ids: str,
) -> TranscriptAnalysisDocument:
    by_id = {segment.segment_id: segment for segment in document.segments}
    findings = [
        SuspiciousTranscriptSegment(
            segment_id=segment_id,
            start_seconds=by_id[segment_id].start_seconds,
            end_seconds=by_id[segment_id].end_seconds,
            severity="major",
            reasons=["low_confidence"],
        )
        for segment_id in segment_ids
    ]
    return TranscriptAnalysisDocument(
        episode_id=document.episode_id,
        suspicious_segments=findings,
        summary=TranscriptAnalysisSummary(
            segment_count=len(document.segments),
            suspicious_count=len(findings),
            critical_count=0,
        ),
    )


def _seed(
    db_session,
    tmp_path: Path,
    document: TranscriptDocument,
    analysis: TranscriptAnalysisDocument,
    *,
    profile: str = "tagesschau_tr",
) -> Episode:
    transcript_dir = tmp_path / "transcripts" / document.episode_id
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "transcript.structured.de.json").write_text(
        json.dumps(document.model_dump(mode="json")),
        encoding="utf-8",
    )
    clean_path = transcript_dir / "transcript.clean.de.txt"
    clean_path.write_text(document.text, encoding="utf-8")
    analysis_dir = tmp_path / "outputs" / document.episode_id / "transcript"
    analysis_dir.mkdir(parents=True)
    (analysis_dir / "transcript_analysis.json").write_text(
        json.dumps(analysis.model_dump(mode="json")),
        encoding="utf-8",
    )
    audio_path = tmp_path / f"{document.episode_id}.m4a"
    audio_path.write_bytes(b"audio")
    episode = Episode(
        episode_id=document.episode_id,
        source="youtube_rss",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TRANSCRIBED,
        pipeline_version=2,
        content_profile=profile,
        transcript_path=str(clean_path),
        audio_path=str(audio_path),
    )
    db_session.add(episode)
    db_session.commit()
    return episode


def _provider(text: str | None = None, *, cost: float = 0.002):
    response_text = text or "Am 16. Juli wurden 20 Menschen verletzt."
    return SimpleNamespace(
        name="openai",
        transcribe=MagicMock(
            return_value=ProviderTranscript(
                text=response_text,
                segments=[],
                audio_seconds=20,
                cost_usd=cost,
            )
        ),
    )


def _media(duration: float = 100):
    return SimpleNamespace(duration_seconds=duration)


def test_disabled_mode_skips_without_external_calls(db_session, tmp_path):
    settings = _settings(
        tmp_path,
        transcription_secondary_enabled=False,
        transcription_secondary_mode="disabled",
    )
    document = _document()
    _seed(db_session, tmp_path, document, _analysis(document), profile="bitcoin_podcast")

    with (
        patch("btcedu.services.ffmpeg_service.probe_media") as probe,
        patch("btcedu.services.transcription_service.get_transcription_provider") as provider,
    ):
        result = verify_transcript(db_session, document.episode_id, settings)

    assert result.skipped is True
    assert result.reason == "secondary transcription disabled"
    probe.assert_not_called()
    provider.assert_not_called()


def test_full_mode_verifies_entire_audio(db_session, tmp_path):
    settings = _settings(
        tmp_path,
        transcription_secondary_enabled=True,
        transcription_secondary_mode="full",
    )
    document = _document()
    _seed(db_session, tmp_path, document, _analysis(document), profile="bitcoin_podcast")
    provider = _provider(document.text)

    with (
        patch("btcedu.services.ffmpeg_service.probe_media", return_value=_media()),
        patch("btcedu.services.ffmpeg_service.extract_audio_clip") as extract,
        patch(
            "btcedu.services.transcription_service.get_transcription_provider",
            return_value=provider,
        ),
    ):
        result = verify_transcript(db_session, document.episode_id, settings)

    assert result.regions_checked == 1
    assert provider.transcribe.call_count == 1
    assert extract.call_args.kwargs["start_seconds"] == 0
    assert extract.call_args.kwargs["end_seconds"] == 100


def test_suspicious_regions_are_clamped_and_merged():
    document = _document()
    plans = build_verification_regions(
        document,
        _analysis(document, "seg-0001", "seg-0002"),
        mode="suspicious_segments_only",
        audio_duration_seconds=20,
        context_seconds=10,
    )

    assert len(plans) == 1
    assert plans[0].source_segment_ids == ("seg-0001", "seg-0002")
    assert plans[0].clip_start_seconds == 0
    assert plans[0].clip_end_seconds == 20
    assert plans[0].original_start_seconds == 5
    assert plans[0].original_end_seconds == 18


def test_no_findings_completes_without_api_call(db_session, tmp_path):
    settings = _settings(tmp_path)
    document = _document()
    _seed(db_session, tmp_path, document, _analysis(document))

    with (
        patch("btcedu.services.ffmpeg_service.probe_media") as probe,
        patch("btcedu.services.transcription_service.get_transcription_provider") as provider,
    ):
        result = verify_transcript(db_session, document.episode_id, settings)

    assert result.skipped is False
    assert result.regions_checked == 0
    probe.assert_not_called()
    provider.assert_not_called()
    artifact = TranscriptVerificationDocument.model_validate_json(
        Path(result.verification_path).read_text(encoding="utf-8")
    )
    assert artifact.verified_regions == []


def test_identical_text_ignores_case_and_punctuation():
    comparison = compare_transcripts(
        "Die Bundesregierung bestätigt den Bericht.",
        "die bundesregierung bestätigt den bericht",
    )

    assert comparison.agreement == "high"
    assert comparison.severity == "none"
    assert comparison.risk_types == ()


@pytest.mark.parametrize(
    ("primary", "secondary", "risk"),
    [
        (
            "Es wurden 20 Menschen verletzt.",
            "Es wurden 12 Menschen verletzt.",
            "number_disagreement",
        ),
        (
            "Am 16.07.2026 begann die Sitzung.",
            "Am 17.07.2026 begann die Sitzung.",
            "date_disagreement",
        ),
        (
            "Die Sitzung begann um 20:15 Uhr.",
            "Die Sitzung begann um 21:15 Uhr.",
            "time_disagreement",
        ),
        (
            "Bei dem Angriff wurden 20 Menschen getötet.",
            "Bei dem Angriff wurden 2 Menschen getötet.",
            "casualty_disagreement",
        ),
        (
            "Die Mannschaft gewann das Spiel mit 2:1.",
            "Die Mannschaft gewann das Spiel mit 1:2.",
            "score_disagreement",
        ),
        (
            "Der Bericht wurde nicht bestätigt.",
            "Der Bericht wurde bestätigt.",
            "negation_disagreement",
        ),
        (
            "Jens Spahn stellte den Bericht vor.",
            "Jens Spaan stellte den Bericht vor.",
            "possible_name_disagreement",
        ),
        (
            "Die Behörden erklärten den vollständigen Sachverhalt.",
            "Die Behörden erklärten, dass",
            "incomplete_sentence",
        ),
        (
            "Angela Merkel kritisierte Jens Spahn.",
            "Jens Spahn kritisierte Angela Merkel.",
            "semantic_role_disagreement",
        ),
    ],
)
def test_comparison_detects_critical_content_differences(primary, secondary, risk):
    comparison = compare_transcripts(primary, secondary)

    assert risk in comparison.risk_types
    assert comparison.severity in {"major", "critical"}


def test_cost_guard_blocks_before_provider_call(db_session, tmp_path):
    settings = _settings(tmp_path, max_episode_cost_usd=0.0)
    document = _document()
    episode = _seed(db_session, tmp_path, document, _analysis(document, "seg-0001"))

    with (
        patch("btcedu.services.ffmpeg_service.probe_media", return_value=_media()),
        patch("btcedu.services.transcription_service.get_transcription_provider") as provider,
    ):
        with pytest.raises(PipelineError, match="cost limit"):
            verify_transcript(db_session, document.episode_id, settings)

    provider.assert_not_called()
    db_session.refresh(episode)
    assert episode.status == EpisodeStatus.COST_LIMIT
    run = (
        db_session.query(PipelineRun)
        .filter(PipelineRun.stage == PipelineStage.TRANSCRIPT_VERIFY)
        .one()
    )
    assert run.status == RunStatus.FAILED


def test_clip_count_limit_blocks_before_provider_call(db_session, tmp_path):
    settings = _settings(tmp_path)
    document = _document()
    _seed(
        db_session,
        tmp_path,
        document,
        _analysis(document, "seg-0001", "seg-0003"),
    )

    with (
        patch(
            "btcedu.core.transcript_verifier._load_transcription_config",
            return_value={
                "secondary": {
                    "enabled": True,
                    "provider": "openai",
                    "model": "gpt-4o-mini-transcribe",
                    "mode": "suspicious_segments_only",
                },
                "suspicious_segment_context_seconds": 1,
                "max_secondary_audio_seconds": 300,
                "max_secondary_clips": 1,
            },
        ),
        patch("btcedu.services.ffmpeg_service.probe_media", return_value=_media()),
        patch("btcedu.services.transcription_service.get_transcription_provider") as provider,
    ):
        with pytest.raises(PipelineError, match="clip limit"):
            verify_transcript(db_session, document.episode_id, settings)

    provider.assert_not_called()


def test_provider_failure_fails_stage_and_records_region(db_session, tmp_path):
    settings = _settings(tmp_path)
    document = _document()
    _seed(db_session, tmp_path, document, _analysis(document, "seg-0001"))
    provider = _provider()
    provider.transcribe.side_effect = RuntimeError("provider unavailable")
    clip_paths: list[Path] = []

    def _extract(_input, output, **_kwargs):
        clip_path = Path(output)
        clip_path.write_bytes(b"clip")
        clip_paths.append(clip_path)
        return output

    with (
        patch("btcedu.services.ffmpeg_service.probe_media", return_value=_media()),
        patch(
            "btcedu.services.ffmpeg_service.extract_audio_clip",
            side_effect=_extract,
        ),
        patch(
            "btcedu.services.transcription_service.get_transcription_provider",
            return_value=provider,
        ),
    ):
        with pytest.raises(RuntimeError, match="provider unavailable"):
            verify_transcript(db_session, document.episode_id, settings)

    path = (
        tmp_path / "outputs" / document.episode_id / "transcript" / "transcript_verification.json"
    )
    artifact = TranscriptVerificationDocument.model_validate_json(path.read_text(encoding="utf-8"))
    assert artifact.verified_regions[0].status == "failed"
    assert artifact.verified_regions[0].error == "provider unavailable"
    assert clip_paths
    assert not clip_paths[0].parent.exists()
    run = (
        db_session.query(PipelineRun)
        .filter(PipelineRun.stage == PipelineStage.TRANSCRIPT_VERIFY)
        .one()
    )
    assert run.status == RunStatus.FAILED


def test_idempotency_force_and_stale_invalidation(db_session, tmp_path):
    settings = _settings(tmp_path)
    document = _document()
    document.segments[0].text = "Die Behörden ermittelten weiter und"
    document.text = " ".join(segment.text for segment in document.segments)
    _seed(db_session, tmp_path, document, _analysis(document, "seg-0001"))
    provider = _provider()

    with (
        patch("btcedu.services.ffmpeg_service.probe_media", return_value=_media()),
        patch("btcedu.services.ffmpeg_service.extract_audio_clip"),
        patch(
            "btcedu.services.transcription_service.get_transcription_provider",
            return_value=provider,
        ),
    ):
        first = verify_transcript(db_session, document.episode_id, settings)
        second = verify_transcript(db_session, document.episode_id, settings)
        forced = verify_transcript(
            db_session,
            document.episode_id,
            settings,
            force=True,
        )

        assert first.skipped is False
        assert second.reason == "already current"
        assert forced.skipped is False
        assert provider.transcribe.call_count == 2

        analyze_transcript(
            db_session,
            document.episode_id,
            settings,
            force=True,
        )
        stale_path = Path(first.verification_path + ".stale")
        assert stale_path.exists()

        rerun = verify_transcript(db_session, document.episode_id, settings)

    assert rerun.skipped is False
    assert provider.transcribe.call_count == 3
    assert not stale_path.exists()


def test_dry_run_resolves_regions_without_external_calls(db_session, tmp_path):
    settings = _settings(tmp_path)
    document = _document()
    _seed(db_session, tmp_path, document, _analysis(document, "seg-0001"))

    with (
        patch("btcedu.services.ffmpeg_service.probe_media", return_value=_media()),
        patch("btcedu.services.ffmpeg_service.extract_audio_clip") as extract,
        patch("btcedu.services.transcription_service.get_transcription_provider") as provider,
    ):
        result = verify_transcript(
            db_session,
            document.episode_id,
            settings,
            dry_run=True,
        )

    assert result.dry_run is True
    assert result.regions_checked == 1
    extract.assert_not_called()
    provider.assert_not_called()
