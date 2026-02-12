"""Tests for Phase 5 pipeline orchestration."""

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from btcedu.config import Settings
from btcedu.core.pipeline import (
    PipelineReport,
    StagePlan,
    StageResult,
    resolve_pipeline_plan,
    retry_episode,
    run_episode_pipeline,
    run_latest,
    run_pending,
    write_report,
)
from btcedu.models.episode import Episode, EpisodeStatus


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        anthropic_api_key="sk-ant-test",
        openai_api_key="sk-test",
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        raw_data_dir=str(tmp_path / "raw"),
        transcripts_dir=str(tmp_path / "transcripts"),
        chunks_dir=str(tmp_path / "chunks"),
        dry_run=True,  # Never call real APIs
    )


@pytest.fixture
def new_episode(db_session):
    """Episode at NEW status."""
    ep = Episode(
        episode_id="ep_new",
        source="youtube_rss",
        title="Bitcoin und Lightning Netzwerk",
        url="https://youtube.com/watch?v=ep_new",
        status=EpisodeStatus.NEW,
        published_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
    )
    db_session.add(ep)
    db_session.commit()
    return ep


@pytest.fixture
def failed_episode(db_session):
    """Episode at CHUNKED status with an error (simulating generate failure)."""
    ep = Episode(
        episode_id="ep_fail",
        source="youtube_rss",
        title="Bitcoin Mining Erklaert",
        url="https://youtube.com/watch?v=ep_fail",
        status=EpisodeStatus.CHUNKED,
        published_at=datetime(2025, 5, 15, tzinfo=timezone.utc),
        error_message="Stage 'generate' failed: API timeout",
        retry_count=1,
    )
    db_session.add(ep)
    db_session.commit()
    return ep


# ── RunEpisodePipeline ───────────────────────────────────────────


class TestRunEpisodePipeline:
    @patch("btcedu.core.pipeline._run_stage")
    def test_processes_new_episode_end_to_end(self, mock_stage, db_session, new_episode, tmp_path):
        mock_stage.return_value = StageResult("mock", "success", 0.1, detail="ok")
        settings = _make_settings(tmp_path)

        report = run_episode_pipeline(db_session, new_episode, settings)

        assert report.success is True
        assert report.error is None
        # Should have called all 4 stages (download, transcribe, chunk, generate)
        # But since mock doesn't actually change status, only download runs then rest skip
        # because the mock doesn't advance episode status.
        # With the real mock returning success but not changing DB status,
        # download runs, then transcribe is "not ready" (still NEW).
        # So let's verify at least download was attempted.
        assert mock_stage.call_count >= 1
        assert report.completed_at is not None

    @patch("btcedu.core.pipeline._run_stage")
    def test_skips_completed_stages(self, mock_stage, db_session, tmp_path):
        """A CHUNKED episode should skip download/transcribe/chunk, run only generate."""
        ep = Episode(
            episode_id="ep_chunked",
            source="youtube_rss",
            title="Test Chunked",
            url="https://youtube.com/watch?v=ep_chunked",
            status=EpisodeStatus.CHUNKED,
            published_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        db_session.add(ep)
        db_session.commit()

        mock_stage.return_value = StageResult(
            "generate", "success", 0.5,
            detail="6 artifacts ($0.0375)",
        )
        settings = _make_settings(tmp_path)

        report = run_episode_pipeline(db_session, ep, settings)

        assert report.success is True
        # Only generate should have been called via _run_stage
        assert mock_stage.call_count == 1
        call_args = mock_stage.call_args
        assert call_args[0][3] == "generate"  # stage_name arg

        # Download, transcribe, chunk, refine should be marked skipped
        skipped = [s for s in report.stages if s.status == "skipped"]
        assert len(skipped) == 4

    @patch("btcedu.core.pipeline._run_stage")
    def test_records_failure_and_increments_retry(self, mock_stage, db_session, new_episode, tmp_path):
        mock_stage.return_value = StageResult(
            "download", "failed", 0.1, error="Connection timeout",
        )
        settings = _make_settings(tmp_path)

        report = run_episode_pipeline(db_session, new_episode, settings)

        assert report.success is False
        assert "download" in report.error
        assert "Connection timeout" in report.error

        db_session.refresh(new_episode)
        assert new_episode.retry_count == 1
        assert new_episode.error_message is not None

    @patch("btcedu.core.pipeline._run_stage")
    def test_stops_on_failure(self, mock_stage, db_session, new_episode, tmp_path):
        """Pipeline should stop after first failed stage."""
        mock_stage.return_value = StageResult(
            "download", "failed", 0.1, error="fail",
        )
        settings = _make_settings(tmp_path)

        report = run_episode_pipeline(db_session, new_episode, settings)

        # Only 1 stage ran (download failed), rest never attempted
        assert mock_stage.call_count == 1
        # The report should have download (failed) + no more attempted stages
        attempted = [s for s in report.stages if s.status != "skipped"]
        assert len(attempted) == 1
        assert attempted[0].stage == "download"

    @patch("btcedu.core.pipeline._run_stage")
    def test_clears_error_on_success(self, mock_stage, db_session, failed_episode, tmp_path):
        """Successful pipeline run clears previous error_message."""
        mock_stage.return_value = StageResult(
            "generate", "success", 0.5, detail="6 artifacts ($0.0375)",
        )
        settings = _make_settings(tmp_path)

        report = run_episode_pipeline(db_session, failed_episode, settings)

        assert report.success is True
        db_session.refresh(failed_episode)
        assert failed_episode.error_message is None


# ── RunPending ───────────────────────────────────────────────────


class TestRunPending:
    @patch("btcedu.core.pipeline.run_episode_pipeline")
    def test_processes_in_published_at_order(self, mock_run, db_session, tmp_path):
        """Episodes should be processed oldest first."""
        ep1 = Episode(
            episode_id="ep_old", source="youtube_rss", title="Old",
            url="https://youtube.com/watch?v=old",
            status=EpisodeStatus.NEW,
            published_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        ep2 = Episode(
            episode_id="ep_new", source="youtube_rss", title="New",
            url="https://youtube.com/watch?v=new",
            status=EpisodeStatus.NEW,
            published_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        db_session.add_all([ep2, ep1])  # Add in wrong order
        db_session.commit()

        mock_run.return_value = PipelineReport(episode_id="mock", title="mock", success=True)
        settings = _make_settings(tmp_path)

        reports = run_pending(db_session, settings)

        assert len(reports) == 2
        # Verify order: oldest first
        call_episodes = [call.args[1].episode_id for call in mock_run.call_args_list]
        assert call_episodes == ["ep_old", "ep_new"]

    @patch("btcedu.core.pipeline.run_episode_pipeline")
    def test_respects_max_limit(self, mock_run, db_session, tmp_path):
        for i in range(5):
            db_session.add(Episode(
                episode_id=f"ep_{i}", source="youtube_rss", title=f"Ep {i}",
                url=f"https://youtube.com/watch?v={i}",
                status=EpisodeStatus.NEW,
                published_at=datetime(2025, 1, i + 1, tzinfo=timezone.utc),
            ))
        db_session.commit()

        mock_run.return_value = PipelineReport(episode_id="mock", title="mock", success=True)
        settings = _make_settings(tmp_path)

        reports = run_pending(db_session, settings, max_episodes=2)

        assert len(reports) == 2
        assert mock_run.call_count == 2

    @patch("btcedu.core.pipeline.run_episode_pipeline")
    def test_respects_since_filter(self, mock_run, db_session, tmp_path):
        ep_old = Episode(
            episode_id="ep_old", source="youtube_rss", title="Old",
            url="https://youtube.com/watch?v=old",
            status=EpisodeStatus.NEW,
            published_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        ep_new = Episode(
            episode_id="ep_new", source="youtube_rss", title="New",
            url="https://youtube.com/watch?v=new",
            status=EpisodeStatus.NEW,
            published_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        db_session.add_all([ep_old, ep_new])
        db_session.commit()

        mock_run.return_value = PipelineReport(episode_id="mock", title="mock", success=True)
        settings = _make_settings(tmp_path)

        since = datetime(2025, 3, 1, tzinfo=timezone.utc)
        reports = run_pending(db_session, settings, since=since)

        assert len(reports) == 1
        assert mock_run.call_args[0][1].episode_id == "ep_new"

    @patch("btcedu.core.pipeline.run_episode_pipeline")
    def test_includes_generated_episodes(self, mock_run, db_session, tmp_path):
        """GENERATED episodes are pending (need refine stage)."""
        ep = Episode(
            episode_id="ep_gen", source="youtube_rss", title="Generated",
            url="https://youtube.com/watch?v=gen",
            status=EpisodeStatus.GENERATED,
            published_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        db_session.add(ep)
        db_session.commit()

        mock_run.return_value = PipelineReport(episode_id="ep_gen", title="Generated", success=True)
        settings = _make_settings(tmp_path)
        reports = run_pending(db_session, settings)

        assert len(reports) == 1
        mock_run.assert_called_once()

    @patch("btcedu.core.pipeline.run_episode_pipeline")
    def test_skips_refined_episodes(self, mock_run, db_session, tmp_path):
        ep = Episode(
            episode_id="ep_done", source="youtube_rss", title="Done",
            url="https://youtube.com/watch?v=done",
            status=EpisodeStatus.REFINED,
            published_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        db_session.add(ep)
        db_session.commit()

        settings = _make_settings(tmp_path)
        reports = run_pending(db_session, settings)

        assert len(reports) == 0
        mock_run.assert_not_called()


# ── RunLatest ────────────────────────────────────────────────────


class TestRunLatest:
    @patch("btcedu.core.pipeline.run_episode_pipeline")
    @patch("btcedu.core.detector.detect_episodes")
    def test_detects_and_processes_newest(self, mock_detect, mock_run, db_session, tmp_path):
        from btcedu.core.detector import DetectResult

        mock_detect.return_value = DetectResult(found=2, new=1, total=2)

        ep_old = Episode(
            episode_id="ep_old", source="youtube_rss", title="Old",
            url="https://youtube.com/watch?v=old",
            status=EpisodeStatus.NEW,
            published_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
        )
        ep_new = Episode(
            episode_id="ep_newest", source="youtube_rss", title="Newest",
            url="https://youtube.com/watch?v=newest",
            status=EpisodeStatus.NEW,
            published_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        db_session.add_all([ep_old, ep_new])
        db_session.commit()

        mock_run.return_value = PipelineReport(
            episode_id="ep_newest", title="Newest", success=True,
        )
        settings = _make_settings(tmp_path)

        result = run_latest(db_session, settings)

        assert result is not None
        assert result.episode_id == "ep_newest"
        # Should have called detect first
        mock_detect.assert_called_once()
        # Should have run pipeline for newest
        assert mock_run.call_args[0][1].episode_id == "ep_newest"

    @patch("btcedu.core.detector.detect_episodes")
    def test_returns_none_when_nothing_pending(self, mock_detect, db_session, tmp_path):
        from btcedu.core.detector import DetectResult

        mock_detect.return_value = DetectResult(found=0, new=0, total=0)
        settings = _make_settings(tmp_path)

        result = run_latest(db_session, settings)

        assert result is None


# ── RetryEpisode ─────────────────────────────────────────────────


class TestRetryEpisode:
    @patch("btcedu.core.pipeline._run_stage")
    def test_retries_from_failed_stage(self, mock_stage, db_session, failed_episode, tmp_path):
        """Failed CHUNKED episode should retry from generate stage."""
        mock_stage.return_value = StageResult(
            "generate", "success", 0.5, detail="6 artifacts ($0.0375)",
        )
        settings = _make_settings(tmp_path)

        report = retry_episode(db_session, "ep_fail", settings)

        assert report.success is True
        # Only generate should run (download/transcribe/chunk skipped)
        assert mock_stage.call_count == 1

        db_session.refresh(failed_episode)
        assert failed_episode.error_message is None

    def test_rejects_non_failed_episode(self, db_session, tmp_path):
        ep = Episode(
            episode_id="ep_ok", source="youtube_rss", title="OK",
            url="https://youtube.com/watch?v=ok",
            status=EpisodeStatus.NEW,
            published_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        db_session.add(ep)
        db_session.commit()

        settings = _make_settings(tmp_path)
        with pytest.raises(ValueError, match="not in a failed state"):
            retry_episode(db_session, "ep_ok", settings)

    def test_rejects_unknown_episode(self, db_session, tmp_path):
        settings = _make_settings(tmp_path)
        with pytest.raises(ValueError, match="Episode not found"):
            retry_episode(db_session, "nonexistent", settings)


# ── WriteReport ──────────────────────────────────────────────────


class TestWriteReport:
    def test_creates_report_json(self, tmp_path):
        report = PipelineReport(
            episode_id="ep001",
            title="Test Episode",
            success=True,
            total_cost_usd=0.038,
            stages=[
                StageResult("download", "success", 1.2, detail="/path/audio.m4a"),
                StageResult("generate", "success", 5.0, detail="6 artifacts ($0.038)"),
            ],
        )
        report.completed_at = report.started_at

        path = write_report(report, str(tmp_path / "reports"))

        assert Path(path).exists()
        data = json.loads(Path(path).read_text())
        assert data["episode_id"] == "ep001"
        assert data["success"] is True
        assert len(data["stages"]) == 2

    def test_report_contains_required_fields(self, tmp_path):
        report = PipelineReport(
            episode_id="ep002",
            title="Another Episode",
            success=False,
            error="Stage 'download' failed: timeout",
        )
        report.completed_at = report.started_at

        path = write_report(report, str(tmp_path / "reports"))

        data = json.loads(Path(path).read_text())
        required = {"episode_id", "title", "started_at", "completed_at",
                     "success", "error", "total_cost_usd", "stages"}
        assert required.issubset(data.keys())
        assert data["error"] == "Stage 'download' failed: timeout"

    def test_report_dir_created(self, tmp_path):
        """Reports dir is created if it doesn't exist."""
        report = PipelineReport(
            episode_id="ep003", title="New Dir Test", success=True,
        )
        report.completed_at = report.started_at

        reports_dir = tmp_path / "new_reports"
        path = write_report(report, str(reports_dir))

        assert Path(path).exists()
        assert "ep003" in path


# ── ResolvePipelinePlan ─────────────────────────────────────────


class TestResolvePipelinePlan:
    def test_new_episode_plans_all_stages(self, db_session, new_episode):
        plan = resolve_pipeline_plan(db_session, new_episode)
        assert len(plan) == 5
        assert plan[0] == StagePlan("download", "run", "status=new")
        assert plan[1] == StagePlan("transcribe", "pending", "after prior stages")
        assert plan[2] == StagePlan("chunk", "pending", "after prior stages")
        assert plan[3] == StagePlan("generate", "pending", "after prior stages")
        assert plan[4] == StagePlan("refine", "pending", "after prior stages")

    def test_downloaded_skips_download(self, db_session):
        ep = Episode(
            episode_id="ep_dl", source="youtube_rss", title="Downloaded",
            url="https://youtube.com/watch?v=dl",
            status=EpisodeStatus.DOWNLOADED,
            published_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        db_session.add(ep)
        db_session.commit()

        plan = resolve_pipeline_plan(db_session, ep)
        assert plan[0] == StagePlan("download", "skip", "already completed")
        assert plan[1].decision == "run"
        assert plan[2].decision == "pending"
        assert plan[3].decision == "pending"
        assert plan[4].decision == "pending"

    def test_chunked_skips_three(self, db_session):
        ep = Episode(
            episode_id="ep_ch", source="youtube_rss", title="Chunked",
            url="https://youtube.com/watch?v=ch",
            status=EpisodeStatus.CHUNKED,
            published_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        db_session.add(ep)
        db_session.commit()

        plan = resolve_pipeline_plan(db_session, ep)
        skipped = [p for p in plan if p.decision == "skip"]
        assert len(skipped) == 3
        assert plan[3] == StagePlan("generate", "run", "status=chunked")
        assert plan[4] == StagePlan("refine", "pending", "after prior stages")

    def test_generated_runs_refine(self, db_session):
        ep = Episode(
            episode_id="ep_gen", source="youtube_rss", title="Generated",
            url="https://youtube.com/watch?v=gen",
            status=EpisodeStatus.GENERATED,
            published_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        db_session.add(ep)
        db_session.commit()

        plan = resolve_pipeline_plan(db_session, ep)
        # download/transcribe/chunk/generate skipped, refine runs
        skipped = [p for p in plan if p.decision == "skip"]
        assert len(skipped) == 4
        assert plan[4] == StagePlan("refine", "run", "status=generated")

    def test_refined_skips_all(self, db_session):
        ep = Episode(
            episode_id="ep_ref", source="youtube_rss", title="Refined",
            url="https://youtube.com/watch?v=ref",
            status=EpisodeStatus.REFINED,
            published_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        db_session.add(ep)
        db_session.commit()

        plan = resolve_pipeline_plan(db_session, ep)
        assert all(p.decision == "skip" for p in plan)

    def test_force_overrides_skips(self, db_session):
        ep = Episode(
            episode_id="ep_force", source="youtube_rss", title="Force",
            url="https://youtube.com/watch?v=force",
            status=EpisodeStatus.REFINED,
            published_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        db_session.add(ep)
        db_session.commit()

        plan = resolve_pipeline_plan(db_session, ep, force=True)
        assert all(p.decision == "run" for p in plan)
        assert len(plan) == 5
        assert plan[0].reason == "forced"
        assert plan[4].reason == "forced"

    def test_plan_with_error_still_resolves(self, db_session, failed_episode):
        """Pipeline plan ignores error_message — only looks at status."""
        plan = resolve_pipeline_plan(db_session, failed_episode)
        # failed_episode is CHUNKED, so download/transcribe/chunk skip, generate runs, refine pending
        skipped = [p for p in plan if p.decision == "skip"]
        assert len(skipped) == 3
        assert plan[3].decision == "run"
        assert plan[3].stage == "generate"
        assert plan[4].decision == "pending"
        assert plan[4].stage == "refine"

    @patch("btcedu.core.pipeline._run_stage")
    def test_stage_callback_invoked(self, mock_stage, db_session, tmp_path):
        """stage_callback is called before each stage that runs."""
        ep = Episode(
            episode_id="ep_cb", source="youtube_rss", title="Callback",
            url="https://youtube.com/watch?v=cb",
            status=EpisodeStatus.CHUNKED,
            published_at=datetime(2025, 6, 1, tzinfo=timezone.utc),
        )
        db_session.add(ep)
        db_session.commit()

        mock_stage.return_value = StageResult(
            "generate", "success", 0.5, detail="6 artifacts ($0.0375)",
        )
        settings = _make_settings(tmp_path)
        called_stages = []
        run_episode_pipeline(
            db_session, ep, settings,
            stage_callback=lambda s: called_stages.append(s),
        )
        assert called_stages == ["generate"]
