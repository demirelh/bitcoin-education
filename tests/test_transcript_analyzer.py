"""Tests for deterministic transcript analysis."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from btcedu.config import Settings
from btcedu.core.transcript_analyzer import _analyze_document, analyze_transcript
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import (
    Episode,
    EpisodeStatus,
    PipelineRun,
    PipelineStage,
)
from btcedu.models.transcript_schema import (
    TranscriptDocument,
    TranscriptSegment,
    TranscriptUsage,
)
from btcedu.profiles import reset_registry


@pytest.fixture(autouse=True)
def _reset_profiles():
    reset_registry()
    yield
    reset_registry()


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        profiles_dir=str(Path(__file__).parent.parent / "btcedu" / "profiles"),
    )


def _document(*segments: TranscriptSegment, episode_id: str = "episode-1"):
    return TranscriptDocument(
        episode_id=episode_id,
        provider="openai",
        model="whisper-1",
        language="de",
        text=" ".join(segment.text for segment in segments),
        segments=list(segments),
        usage=TranscriptUsage(audio_seconds=max((s.end_seconds for s in segments), default=0)),
    )


def _segment(
    segment_id: str,
    text: str,
    *,
    start: float = 0,
    end: float = 5,
    confidence: float | None = None,
) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=segment_id,
        start_seconds=start,
        end_seconds=end,
        text=text,
        confidence=confidence,
    )


def _seed_episode(db_session, tmp_path: Path, document: TranscriptDocument, *, version=2):
    transcript_dir = tmp_path / "transcripts" / document.episode_id
    transcript_dir.mkdir(parents=True)
    structured_path = transcript_dir / "transcript.structured.de.json"
    structured_path.write_text(
        json.dumps(document.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    clean_path = transcript_dir / "transcript.clean.de.txt"
    clean_path.write_text(document.text, encoding="utf-8")
    episode = Episode(
        episode_id=document.episode_id,
        source="youtube_rss",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TRANSCRIBED,
        pipeline_version=version,
        content_profile="tagesschau_tr",
        transcript_path=str(clean_path),
    )
    db_session.add(episode)
    db_session.commit()
    return episode


def test_marks_incomplete_sentence_conservatively():
    analysis = _analyze_document(
        _document(_segment("seg-0001", "Die Verhandlungen dauern an und")),
        {},
    )

    suspicious = analysis.suspicious_segments[0]
    assert "incomplete_sentence" in suspicious.reasons
    assert "possible_missing_words" in suspicious.reasons


def test_marks_strong_repetition():
    analysis = _analyze_document(
        _document(_segment("seg-0001", "Fehler Fehler Fehler Fehler im Transkript.")),
        {},
    )

    suspicious = analysis.suspicious_segments[0]
    assert suspicious.severity == "critical"
    assert "strong_repetition" in suspicious.reasons


def test_marks_low_provider_confidence():
    analysis = _analyze_document(
        _document(
            _segment(
                "seg-0001",
                "Die Bundesregierung stellte den neuen Bericht vor.",
                confidence=0.2,
            )
        ),
        {"confidence_threshold": 0.45},
    )

    assert analysis.suspicious_segments[0].reasons == ["low_confidence"]


def test_normal_news_sentence_is_not_flagged():
    analysis = _analyze_document(
        _document(
            _segment(
                "seg-0001",
                "Die Bundesregierung stellte heute in Berlin den neuen Bericht vor.",
                confidence=0.91,
            ),
            _segment(
                "seg-0002",
                "Die Ergebnisse sollen in der kommenden Woche beraten werden.",
                start=5,
                end=10,
                confidence=0.88,
            ),
        ),
        {},
    )

    assert analysis.suspicious_segments == []
    assert analysis.summary.suspicious_count == 0


def test_german_noun_inflections_are_not_name_variants():
    analysis = _analyze_document(
        _document(
            _segment(
                "seg-0001",
                "Mehrere Abgeordnete diskutierten die Entscheidung.",
                confidence=0.9,
            ),
            _segment(
                "seg-0002",
                "Ein Abgeordneter verteidigte die Entscheidungen.",
                start=5,
                end=10,
                confidence=0.9,
            ),
            _segment(
                "seg-0003",
                "Die Abgeordneten stimmten über eine Entscheidung ab.",
                start=10,
                end=15,
                confidence=0.9,
            ),
        ),
        {},
    )

    assert all(
        "inconsistent_proper_name_spelling" not in finding.reasons
        for finding in analysis.suspicious_segments
    )


def test_analysis_writes_artifact_provenance_and_pipeline_run(
    db_session,
    tmp_path,
):
    settings = _settings(tmp_path)
    document = _document(_segment("seg-0001", "Die Sitzung wurde beendet und", confidence=0.8))
    episode = _seed_episode(db_session, tmp_path, document)

    result = analyze_transcript(db_session, document.episode_id, settings)

    assert result.suspicious_count == 1
    assert Path(result.analysis_path).exists()
    assert Path(result.provenance_path).exists()
    db_session.refresh(episode)
    assert episode.status == EpisodeStatus.TRANSCRIBED
    run = (
        db_session.query(PipelineRun)
        .filter(PipelineRun.stage == PipelineStage.TRANSCRIPT_ANALYZE)
        .one()
    )
    assert run.estimated_cost_usd == 0
    artifact = (
        db_session.query(ContentArtifact)
        .filter(ContentArtifact.artifact_type == "transcript_analysis")
        .one()
    )
    assert artifact.file_path == result.analysis_path


def test_analysis_is_idempotent_and_force_reruns(db_session, tmp_path):
    settings = _settings(tmp_path)
    document = _document(_segment("seg-0001", "Ein vollständiger normaler Satz."))
    _seed_episode(db_session, tmp_path, document)

    first = analyze_transcript(db_session, document.episode_id, settings)
    second = analyze_transcript(db_session, document.episode_id, settings)
    forced = analyze_transcript(db_session, document.episode_id, settings, force=True)

    assert first.skipped is False
    assert second.skipped is True
    assert second.reason == "already current"
    assert forced.skipped is False
    runs = (
        db_session.query(PipelineRun)
        .filter(PipelineRun.stage == PipelineStage.TRANSCRIPT_ANALYZE)
        .all()
    )
    assert len(runs) == 2


def test_v1_episode_is_rejected_without_mutation(db_session, tmp_path):
    settings = _settings(tmp_path)
    document = _document(_segment("seg-0001", "Ein vollständiger Satz."))
    episode = _seed_episode(db_session, tmp_path, document, version=1)

    with pytest.raises(ValueError, match="requires v2"):
        analyze_transcript(db_session, document.episode_id, settings)

    db_session.refresh(episode)
    assert episode.pipeline_version == 1
    assert episode.status == EpisodeStatus.TRANSCRIBED
