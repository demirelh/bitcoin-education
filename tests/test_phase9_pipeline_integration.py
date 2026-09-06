"""Phase 9 end-to-end pipeline compatibility tests without external API calls."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from btcedu.config import Settings
from btcedu.core.pipeline import StageResult, run_episode_pipeline
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage
from btcedu.models.review import ReviewTask
from btcedu.models.transcript_schema import (
    TranscriptDocument,
    TranscriptSegment,
    TranscriptUsage,
)
from btcedu.services.claude_service import ClaudeResponse

_STAGE_STATUS = {
    "download": EpisodeStatus.DOWNLOADED,
    "transcribe": EpisodeStatus.TRANSCRIBED,
    "correct": EpisodeStatus.CORRECTED,
    "segment": EpisodeStatus.SEGMENTED,
    "translate": EpisodeStatus.TRANSLATED,
    "adapt": EpisodeStatus.ADAPTED,
    "chapterize": EpisodeStatus.CHAPTERIZED,
    "frameextract": EpisodeStatus.FRAMES_EXTRACTED,
    "imagegen": EpisodeStatus.IMAGES_GENERATED,
    "tts": EpisodeStatus.TTS_DONE,
    "sceneplan": EpisodeStatus.SCENE_PLANNED,
    "anchorgen": EpisodeStatus.ANCHOR_GENERATED,
    "render": EpisodeStatus.RENDERED,
    "review_gate_3": EpisodeStatus.APPROVED,
    "publish": EpisodeStatus.PUBLISHED,
}


def _settings(tmp_path) -> Settings:
    return Settings(
        outputs_dir=str(tmp_path / "outputs"),
        transcripts_dir=str(tmp_path / "transcripts"),
        raw_data_dir=str(tmp_path / "raw"),
        reports_dir=str(tmp_path / "reports"),
        database_url=f"sqlite:///{tmp_path / 'phase9.db'}",
        pipeline_version=2,
        dry_run=True,
    )


def _dry_run_stage(session, episode, settings, stage_name, force=False):
    assert settings.dry_run is True
    next_status = _STAGE_STATUS.get(stage_name)
    if next_status is not None:
        episode.status = next_status
        session.commit()
    return StageResult(stage_name, "success", 0.0, detail="dry-run ($0.0000)")


@pytest.mark.parametrize("pipeline_version", [1, 2])
def test_complete_pipeline_dry_run_is_legacy_safe(
    db_session,
    tmp_path,
    pipeline_version,
):
    settings = _settings(tmp_path)
    episode = Episode(
        episode_id=f"ep-phase9-v{pipeline_version}",
        source="youtube_rss",
        title=f"Phase 9 v{pipeline_version}",
        url=f"https://example.com/v{pipeline_version}",
        status=EpisodeStatus.NEW,
        pipeline_version=pipeline_version,
        content_profile="bitcoin_podcast",
    )
    db_session.add(episode)
    db_session.commit()

    with patch("btcedu.core.pipeline._run_stage", side_effect=_dry_run_stage):
        report = run_episode_pipeline(db_session, episode, settings)

    db_session.refresh(episode)
    assert report.success is True
    assert episode.status == EpisodeStatus.PUBLISHED
    assert report.total_cost_usd == 0.0
    stage_names = {stage.stage for stage in report.stages}
    if pipeline_version == 1:
        assert not {
            "transcript_analyze",
            "transcript_verify",
            "transcript_qa",
            "review_gate_transcript_qa",
        }.intersection(stage_names)
    else:
        assert {
            "transcript_analyze",
            "transcript_verify",
            "transcript_qa",
            "review_gate_transcript_qa",
        }.issubset(stage_names)


def test_real_v2_transcript_chain_blocks_at_transcript_qa_gate(db_session, tmp_path):
    settings = _settings(tmp_path)
    settings.dry_run = False
    episode_id = "ep-phase9-real-v2"
    transcript_dir = Path(settings.transcripts_dir) / episode_id
    transcript_dir.mkdir(parents=True)
    text = "Die Bundesregierung bestätigte den Bericht."
    clean_path = transcript_dir / "transcript.clean.de.txt"
    clean_path.write_text(text, encoding="utf-8")
    document = TranscriptDocument(
        episode_id=episode_id,
        provider="openai",
        model="whisper-1",
        language="de",
        text=text,
        segments=[
            TranscriptSegment(
                segment_id="seg-0001",
                start_seconds=0,
                end_seconds=3,
                text=text,
                confidence=0.99,
            )
        ],
        usage=TranscriptUsage(audio_seconds=3, cost_usd=0.0),
    )
    (transcript_dir / "transcript.structured.de.json").write_text(
        document.model_dump_json(indent=2),
        encoding="utf-8",
    )
    episode = Episode(
        episode_id=episode_id,
        source="youtube_rss",
        title="Real v2 transcript chain",
        url="https://example.com/real-v2",
        status=EpisodeStatus.TRANSCRIBED,
        pipeline_version=2,
        content_profile="tagesschau_tr",
        transcript_path=str(clean_path),
        audio_path=str(tmp_path / "audio.m4a"),
    )
    db_session.add(episode)
    db_session.commit()

    correction_response = ClaudeResponse(
        text=json.dumps(
            {
                "segments": [
                    {
                        "segment_id": "seg-0001",
                        "corrected_text": text,
                        "status": "unresolved",
                        "severity": "critical",
                        "flags": ["incomplete_sentence"],
                        "reason": "Satz bleibt ungeklärt.",
                        "verification_ids": [],
                    }
                ]
            }
        ),
        input_tokens=10,
        output_tokens=10,
        cost_usd=0.001,
        model="test-model",
    )

    from btcedu.core.qa_reviewer import GateAdjudicationResult

    with (
        patch("btcedu.core.corrector.call_claude", return_value=correction_response),
        patch(
            "btcedu.core.qa_reviewer.adjudicate_transcript_qa_gate",
            return_value=GateAdjudicationResult(performed=False),
        ),
    ):
        report = run_episode_pipeline(db_session, episode, settings)

    executed = [stage.stage for stage in report.stages if stage.status != "skipped"]
    assert executed == [
        "transcript_analyze",
        "transcript_verify",
        "correct",
        "transcript_qa",
        "review_gate_transcript_qa",
    ]
    assert report.stages[-1].status == "review_pending"
    db_session.refresh(episode)
    assert episode.status == EpisodeStatus.CORRECTED
    assert (
        db_session.query(ReviewTask)
        .filter(
            ReviewTask.episode_id == episode_id,
            ReviewTask.stage == "transcript_qa",
        )
        .count()
        == 1
    )
    assert {
        run.stage
        for run in db_session.query(PipelineRun).filter(PipelineRun.episode_id == episode.id).all()
    }.issuperset(
        {
            PipelineStage.TRANSCRIPT_ANALYZE,
            PipelineStage.TRANSCRIPT_VERIFY,
            PipelineStage.CORRECT,
            PipelineStage.TRANSCRIPT_QA,
        }
    )
