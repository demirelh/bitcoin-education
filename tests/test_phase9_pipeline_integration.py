"""Phase 9 end-to-end pipeline compatibility tests without external API calls."""

from unittest.mock import patch

import pytest

from btcedu.config import Settings
from btcedu.core.pipeline import StageResult, run_episode_pipeline
from btcedu.models.episode import Episode, EpisodeStatus

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
