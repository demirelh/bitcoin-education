"""Tests for Phase 5 pipeline orchestration."""

import json
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.config import Settings
from btcedu.core.pipeline import (
    PipelineReport,
    StagePlan,
    StageResult,
    _run_stage,
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
        dry_run=True,  # Never call real APIs
        pipeline_version=2,
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
        published_at=datetime(2025, 6, 1, tzinfo=UTC),
    )
    db_session.add(ep)
    db_session.commit()
    return ep


@pytest.fixture
def failed_episode(db_session):
    """Episode at TRANSLATED status with an error (simulating adapt failure)."""
    ep = Episode(
        episode_id="ep_fail",
        source="youtube_rss",
        title="Bitcoin Mining Erklaert",
        url="https://youtube.com/watch?v=ep_fail",
        status=EpisodeStatus.TRANSLATED,
        published_at=datetime(2025, 5, 15, tzinfo=UTC),
        pipeline_version=2,
        error_message="Stage 'adapt' failed: API timeout",
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
        # The mock does not advance status, so only the first ready stage is attempted.
        assert mock_stage.call_count >= 1
        assert report.completed_at is not None

    @patch("btcedu.core.pipeline._run_stage")
    def test_skips_completed_stages(self, mock_stage, db_session, tmp_path):
        """A TRANSLATED episode should skip earlier stages and run adapt."""
        ep = Episode(
            episode_id="ep_translated",
            source="youtube_rss",
            title="Test Translated",
            url="https://youtube.com/watch?v=ep_translated",
            status=EpisodeStatus.TRANSLATED,
            published_at=datetime(2025, 6, 1, tzinfo=UTC),
            pipeline_version=2,
        )
        db_session.add(ep)
        db_session.commit()

        mock_stage.return_value = StageResult(
            "adapt",
            "success",
            0.5,
            detail="adapted ($0.0375)",
        )
        settings = _make_settings(tmp_path)

        report = run_episode_pipeline(db_session, ep, settings)

        assert report.success is True
        # Only adapt should have been called via _run_stage
        assert mock_stage.call_count == 1
        call_args = mock_stage.call_args
        assert call_args[0][3] == "adapt"  # stage_name arg

        skipped_before_adapt = [s.stage for s in report.stages[:7]]
        assert skipped_before_adapt == [
            "download",
            "transcribe",
            "transcript_analyze",
            "transcript_verify",
            "correct",
            "review_gate_1",
            "translate",
        ]

    @patch("btcedu.core.pipeline._run_stage")
    def test_records_failure_and_increments_retry(
        self, mock_stage, db_session, new_episode, tmp_path
    ):
        mock_stage.return_value = StageResult(
            "download",
            "failed",
            0.1,
            error="Connection timeout",
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
            "download",
            "failed",
            0.1,
            error="fail",
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
            "adapt",
            "success",
            0.5,
            detail="adapted ($0.0375)",
        )
        settings = _make_settings(tmp_path)

        report = run_episode_pipeline(db_session, failed_episode, settings)

        assert report.success is True
        db_session.refresh(failed_episode)
        assert failed_episode.error_message is None


# ── RunPending ───────────────────────────────────────────────────


class TestEnsureStagePipelineRun:
    """Gap-fill PipelineRun timing for stages that don't record their own."""

    @patch("btcedu.core.pipeline._run_stage")
    def test_fills_timing_for_download(self, mock_stage, db_session, new_episode, tmp_path):
        """download has no PipelineRun of its own → gap-fill creates one."""
        from btcedu.models.episode import PipelineRun, PipelineStage, RunStatus

        mock_stage.return_value = StageResult("download", "success", 12.5, detail="ok")
        settings = _make_settings(tmp_path)

        run_episode_pipeline(db_session, new_episode, settings)

        runs = (
            db_session.query(PipelineRun)
            .filter(
                PipelineRun.episode_id == new_episode.id,
                PipelineRun.stage == PipelineStage.DOWNLOAD,
                PipelineRun.status == RunStatus.SUCCESS,
            )
            .all()
        )
        assert len(runs) == 1
        dur = (runs[0].completed_at - runs[0].started_at).total_seconds()
        assert 12.0 <= dur <= 13.0
        assert runs[0].git_commit is not None

    def test_does_not_duplicate_existing_run(self, db_session, new_episode, tmp_path):
        """If the stage already recorded its own run, no duplicate is added."""
        from btcedu.core.pipeline import _ensure_stage_pipeline_run
        from btcedu.models.episode import PipelineRun, PipelineStage, RunStatus

        own = PipelineRun(
            episode_id=new_episode.id,
            stage=PipelineStage.TRANSLATE,
            status=RunStatus.SUCCESS,
            started_at=datetime(2025, 6, 1, 10, 0, 0, tzinfo=UTC),
            completed_at=datetime(2025, 6, 1, 10, 5, 0, tzinfo=UTC),
            estimated_cost_usd=0.42,
        )
        db_session.add(own)
        db_session.commit()

        # since = well before the existing run's completed_at → detected as
        # the stage's own record, so no duplicate is added.
        since = datetime(2025, 6, 1, 9, 0, 0, tzinfo=UTC)
        _ensure_stage_pipeline_run(db_session, new_episode, "translate", 300.0, since)

        runs = (
            db_session.query(PipelineRun)
            .filter(
                PipelineRun.episode_id == new_episode.id,
                PipelineRun.stage == PipelineStage.TRANSLATE,
            )
            .all()
        )
        assert len(runs) == 1
        assert runs[0].estimated_cost_usd == 0.42

    def test_ignores_review_gates(self, db_session, new_episode):
        """Review gates have no PipelineStage → no record is created."""
        from btcedu.core.pipeline import _ensure_stage_pipeline_run
        from btcedu.core.pipeline import _utcnow as _pu
        from btcedu.models.episode import PipelineRun

        _ensure_stage_pipeline_run(db_session, new_episode, "review_gate_1", 0.1, _pu())

        assert (
            db_session.query(PipelineRun).filter(PipelineRun.episode_id == new_episode.id).count()
            == 0
        )


class TestRunPending:
    @patch("btcedu.core.pipeline.run_episode_pipeline")
    def test_processes_in_published_at_order(self, mock_run, db_session, tmp_path):
        """Episodes should be processed oldest first."""
        ep1 = Episode(
            episode_id="ep_old",
            source="youtube_rss",
            title="Old",
            url="https://youtube.com/watch?v=old",
            status=EpisodeStatus.NEW,
            published_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        ep2 = Episode(
            episode_id="ep_new",
            source="youtube_rss",
            title="New",
            url="https://youtube.com/watch?v=new",
            status=EpisodeStatus.NEW,
            published_at=datetime(2025, 6, 1, tzinfo=UTC),
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
            db_session.add(
                Episode(
                    episode_id=f"ep_{i}",
                    source="youtube_rss",
                    title=f"Ep {i}",
                    url=f"https://youtube.com/watch?v={i}",
                    status=EpisodeStatus.NEW,
                    published_at=datetime(2025, 1, i + 1, tzinfo=UTC),
                )
            )
        db_session.commit()

        mock_run.return_value = PipelineReport(episode_id="mock", title="mock", success=True)
        settings = _make_settings(tmp_path)

        reports = run_pending(db_session, settings, max_episodes=2)

        assert len(reports) == 2
        assert mock_run.call_count == 2

    @patch("btcedu.core.pipeline.run_episode_pipeline")
    def test_respects_since_filter(self, mock_run, db_session, tmp_path):
        ep_old = Episode(
            episode_id="ep_old",
            source="youtube_rss",
            title="Old",
            url="https://youtube.com/watch?v=old",
            status=EpisodeStatus.NEW,
            published_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        ep_new = Episode(
            episode_id="ep_new",
            source="youtube_rss",
            title="New",
            url="https://youtube.com/watch?v=new",
            status=EpisodeStatus.NEW,
            published_at=datetime(2025, 6, 1, tzinfo=UTC),
        )
        db_session.add_all([ep_old, ep_new])
        db_session.commit()

        mock_run.return_value = PipelineReport(episode_id="mock", title="mock", success=True)
        settings = _make_settings(tmp_path)

        since = datetime(2025, 3, 1, tzinfo=UTC)
        reports = run_pending(db_session, settings, since=since)

        assert len(reports) == 1
        assert mock_run.call_args[0][1].episode_id == "ep_new"

    @patch("btcedu.core.pipeline.run_episode_pipeline")
    def test_includes_adapted_episodes(self, mock_run, db_session, tmp_path):
        """ADAPTED episodes are pending (need review/chapterize stages)."""
        ep = Episode(
            episode_id="ep_adapted",
            source="youtube_rss",
            title="Adapted",
            url="https://youtube.com/watch?v=adapted",
            status=EpisodeStatus.ADAPTED,
            pipeline_version=2,
            published_at=datetime(2025, 6, 1, tzinfo=UTC),
        )
        db_session.add(ep)
        db_session.commit()

        mock_run.return_value = PipelineReport(
            episode_id="ep_adapted", title="Adapted", success=True
        )
        settings = _make_settings(tmp_path)
        reports = run_pending(db_session, settings)

        assert len(reports) == 1
        mock_run.assert_called_once()

    @patch("btcedu.core.pipeline.run_episode_pipeline")
    def test_skips_published_episodes(self, mock_run, db_session, tmp_path):
        ep = Episode(
            episode_id="ep_done",
            source="youtube_rss",
            title="Done",
            url="https://youtube.com/watch?v=done",
            status=EpisodeStatus.PUBLISHED,
            pipeline_version=2,
            published_at=datetime(2025, 6, 1, tzinfo=UTC),
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
            episode_id="ep_old",
            source="youtube_rss",
            title="Old",
            url="https://youtube.com/watch?v=old",
            status=EpisodeStatus.NEW,
            published_at=datetime(2025, 1, 1, tzinfo=UTC),
        )
        ep_new = Episode(
            episode_id="ep_newest",
            source="youtube_rss",
            title="Newest",
            url="https://youtube.com/watch?v=newest",
            status=EpisodeStatus.NEW,
            published_at=datetime(2025, 6, 1, tzinfo=UTC),
        )
        db_session.add_all([ep_old, ep_new])
        db_session.commit()

        mock_run.return_value = PipelineReport(
            episode_id="ep_newest",
            title="Newest",
            success=True,
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
        """Failed TRANSLATED episode should retry from adapt stage."""
        mock_stage.return_value = StageResult(
            "adapt",
            "success",
            0.5,
            detail="adapted ($0.0375)",
        )
        settings = _make_settings(tmp_path)

        report = retry_episode(db_session, "ep_fail", settings)

        assert report.success is True
        # Only adapt should run (earlier stages skipped)
        assert mock_stage.call_count == 1

        db_session.refresh(failed_episode)
        assert failed_episode.error_message is None

    def test_rejects_non_failed_episode(self, db_session, tmp_path):
        ep = Episode(
            episode_id="ep_ok",
            source="youtube_rss",
            title="OK",
            url="https://youtube.com/watch?v=ok",
            status=EpisodeStatus.NEW,
            published_at=datetime(2025, 6, 1, tzinfo=UTC),
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
                StageResult("adapt", "success", 5.0, detail="adapted ($0.038)"),
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
        required = {
            "episode_id",
            "title",
            "started_at",
            "completed_at",
            "success",
            "error",
            "total_cost_usd",
            "stages",
        }
        assert required.issubset(data.keys())
        assert data["error"] == "Stage 'download' failed: timeout"

    def test_report_dir_created(self, tmp_path):
        """Reports dir is created if it doesn't exist."""
        report = PipelineReport(
            episode_id="ep003",
            title="New Dir Test",
            success=True,
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
        assert len(plan) == 18
        assert plan[0] == StagePlan("download", "run", "status=new")
        assert plan[1] == StagePlan("transcribe", "pending", "after prior stages")
        assert plan[2] == StagePlan("transcript_analyze", "pending", "after prior stages")
        assert plan[3] == StagePlan("transcript_verify", "pending", "after prior stages")
        assert plan[4] == StagePlan("correct", "pending", "after prior stages")
        assert plan[-1] == StagePlan("publish", "pending", "after prior stages")

    def test_downloaded_skips_download(self, db_session):
        ep = Episode(
            episode_id="ep_dl",
            source="youtube_rss",
            title="Downloaded",
            url="https://youtube.com/watch?v=dl",
            status=EpisodeStatus.DOWNLOADED,
            published_at=datetime(2025, 6, 1, tzinfo=UTC),
        )
        db_session.add(ep)
        db_session.commit()

        plan = resolve_pipeline_plan(db_session, ep)
        assert plan[0] == StagePlan("download", "skip", "already completed")
        assert plan[1].decision == "run"
        assert plan[2].decision == "pending"
        assert plan[3].decision == "pending"
        assert plan[-1].decision == "pending"

    def test_translated_runs_adapt(self, db_session):
        ep = Episode(
            episode_id="ep_tr",
            source="youtube_rss",
            title="Translated",
            url="https://youtube.com/watch?v=tr",
            status=EpisodeStatus.TRANSLATED,
            pipeline_version=2,
            published_at=datetime(2025, 6, 1, tzinfo=UTC),
        )
        db_session.add(ep)
        db_session.commit()

        plan = resolve_pipeline_plan(db_session, ep)
        skipped = [p for p in plan if p.decision == "skip"]
        assert len(skipped) == 7
        assert plan[7] == StagePlan("adapt", "run", "status=translated")
        assert plan[8] == StagePlan("review_gate_2", "pending", "after prior stages")

    def test_adapted_runs_review_gate_and_chapterize(self, db_session):
        ep = Episode(
            episode_id="ep_adapted",
            source="youtube_rss",
            title="Adapted",
            url="https://youtube.com/watch?v=adapted",
            status=EpisodeStatus.ADAPTED,
            pipeline_version=2,
            published_at=datetime(2025, 6, 1, tzinfo=UTC),
        )
        db_session.add(ep)
        db_session.commit()

        plan = resolve_pipeline_plan(db_session, ep)
        skipped = [p for p in plan if p.decision == "skip"]
        assert len(skipped) == 8
        assert plan[8] == StagePlan("review_gate_2", "run", "status=adapted")
        assert plan[9] == StagePlan("chapterize", "run", "status=adapted")

    def test_published_skips_all(self, db_session):
        ep = Episode(
            episode_id="ep_ref",
            source="youtube_rss",
            title="Refined",
            url="https://youtube.com/watch?v=ref",
            status=EpisodeStatus.PUBLISHED,
            pipeline_version=2,
            published_at=datetime(2025, 6, 1, tzinfo=UTC),
        )
        db_session.add(ep)
        db_session.commit()

        plan = resolve_pipeline_plan(db_session, ep)
        assert all(p.decision == "skip" for p in plan)

    def test_force_overrides_skips(self, db_session):
        ep = Episode(
            episode_id="ep_force",
            source="youtube_rss",
            title="Force",
            url="https://youtube.com/watch?v=force",
            status=EpisodeStatus.PUBLISHED,
            pipeline_version=2,
            published_at=datetime(2025, 6, 1, tzinfo=UTC),
        )
        db_session.add(ep)
        db_session.commit()

        plan = resolve_pipeline_plan(db_session, ep, force=True)
        assert all(p.decision == "run" for p in plan)
        assert len(plan) == 18
        assert plan[0].reason == "forced"
        assert plan[-1].reason == "forced"

    def test_plan_with_error_still_resolves(self, db_session, failed_episode):
        """Pipeline plan ignores error_message — only looks at status."""
        plan = resolve_pipeline_plan(db_session, failed_episode)
        skipped = [p for p in plan if p.decision == "skip"]
        assert len(skipped) == 7
        assert plan[7].decision == "run"
        assert plan[7].stage == "adapt"
        assert plan[8].decision == "pending"
        assert plan[8].stage == "review_gate_2"

    @patch("btcedu.core.pipeline._run_stage")
    def test_stage_callback_invoked(self, mock_stage, db_session, tmp_path):
        """stage_callback is called before each stage that runs."""
        ep = Episode(
            episode_id="ep_cb",
            source="youtube_rss",
            title="Callback",
            url="https://youtube.com/watch?v=cb",
            status=EpisodeStatus.TRANSLATED,
            pipeline_version=2,
            published_at=datetime(2025, 6, 1, tzinfo=UTC),
        )
        db_session.add(ep)
        db_session.commit()

        mock_stage.return_value = StageResult(
            "adapt",
            "success",
            0.5,
            detail="adapted ($0.0375)",
        )
        settings = _make_settings(tmp_path)
        called_stages = []
        run_episode_pipeline(
            db_session,
            ep,
            settings,
            stage_callback=lambda s: called_stages.append(s),
        )
        assert called_stages == ["adapt"]


# ── V2 Pipeline End-to-End ───────────────────────────────────────


class TestV2PipelineE2E:
    """End-to-end test for the v2 pipeline including review gates.

    Simulates a full NEW → PUBLISHED flow by mocking _run_stage to
    advance episode status at each stage, with review gates pausing
    the pipeline until manually approved.
    """

    @pytest.fixture
    def v2_settings(self, tmp_path):
        """Settings for v2 pipeline tests."""
        s = _make_settings(tmp_path)
        s.pipeline_version = 2
        return s

    @pytest.fixture
    def v2_episode(self, db_session):
        ep = Episode(
            episode_id="ep_v2_e2e",
            source="youtube_rss",
            title="V2 E2E Test Episode",
            url="https://youtube.com/watch?v=v2e2e",
            status=EpisodeStatus.NEW,
            published_at=datetime(2025, 6, 1, tzinfo=UTC),
            pipeline_version=2,
        )
        db_session.add(ep)
        db_session.commit()
        return ep

    @pytest.fixture
    def v2_files(self, v2_settings, tmp_path):
        """Create required output files for review gates."""
        ep_id = "ep_v2_e2e"
        t_dir = Path(v2_settings.transcripts_dir) / ep_id
        t_dir.mkdir(parents=True, exist_ok=True)
        (t_dir / "transcript.corrected.de.txt").write_text("corrected", encoding="utf-8")

        o_dir = Path(v2_settings.outputs_dir) / ep_id
        review_dir = o_dir / "review"
        review_dir.mkdir(parents=True, exist_ok=True)
        render_dir = o_dir / "render"
        render_dir.mkdir(parents=True, exist_ok=True)

        # Diff with a real word change — should NOT auto-approve
        (review_dir / "correction_diff.json").write_text(
            json.dumps(
                {
                    "changes": [
                        {"type": "replace", "original": "Bitcon", "corrected": "Bitcoin"},
                    ],
                    "summary": {"total_changes": 1},
                }
            ),
            encoding="utf-8",
        )
        (o_dir / "script.adapted.tr.md").write_text("adapted", encoding="utf-8")
        (render_dir / "draft.mp4").write_bytes(b"video")
        (o_dir / "chapters.json").write_text(json.dumps({"chapters": []}), encoding="utf-8")

    def _make_stage_side_effect(self, db_session):
        """Return a mock side effect that advances episode status like real stages."""
        _STAGE_RESULT_STATUS = {
            "download": EpisodeStatus.DOWNLOADED,
            "transcribe": EpisodeStatus.TRANSCRIBED,
            "correct": EpisodeStatus.CORRECTED,
            "translate": EpisodeStatus.TRANSLATED,
            "adapt": EpisodeStatus.ADAPTED,
            "chapterize": EpisodeStatus.CHAPTERIZED,
            "frameextract": EpisodeStatus.FRAMES_EXTRACTED,
            "imagegen": EpisodeStatus.IMAGES_GENERATED,
            "tts": EpisodeStatus.TTS_DONE,
            "anchorgen": EpisodeStatus.ANCHOR_GENERATED,
            "render": EpisodeStatus.RENDERED,
            "publish": EpisodeStatus.PUBLISHED,
        }

        def side_effect(session, episode, settings, stage_name, force=False):
            # Review gates need real logic — delegate to actual _run_stage
            if stage_name.startswith("review_gate"):
                return _run_stage(session, episode, settings, stage_name, force=force)

            # Normal stages: advance status and return success
            new_status = _STAGE_RESULT_STATUS.get(stage_name)
            if new_status:
                episode.status = new_status
                session.commit()
            return StageResult(stage_name, "success", 0.1, detail="mock ($0.0010)")

        return side_effect

    @patch("btcedu.core.pipeline._run_stage")
    def test_pauses_at_review_gate_1(
        self, mock_stage, db_session, v2_episode, v2_settings, v2_files
    ):
        """Pipeline runs through correct, then pauses at review_gate_1."""
        mock_stage.side_effect = self._make_stage_side_effect(db_session)

        report = run_episode_pipeline(db_session, v2_episode, v2_settings)

        assert report.success is True  # review_pending is not a failure
        # Should have run: download, transcribe, correct, then paused at review_gate_1
        stage_names = [s.stage for s in report.stages if s.status != "skipped"]
        assert "download" in stage_names
        assert "transcribe" in stage_names
        assert "correct" in stage_names
        assert "review_gate_1" in stage_names

        # Episode stays at CORRECTED
        db_session.refresh(v2_episode)
        assert v2_episode.status == EpisodeStatus.CORRECTED

        # Review pending — should not have processed translate
        gate_result = next(s for s in report.stages if s.stage == "review_gate_1")
        assert gate_result.status == "review_pending"

    @patch("btcedu.core.pipeline._run_stage")
    def test_resumes_after_gate_1_approval(
        self, mock_stage, db_session, v2_episode, v2_settings, v2_files
    ):
        """After approving gate 1, pipeline continues to gate 2."""
        from btcedu.core.reviewer import approve_review
        from btcedu.models.review import ReviewTask

        mock_stage.side_effect = self._make_stage_side_effect(db_session)

        # First run: pauses at gate 1
        run_episode_pipeline(db_session, v2_episode, v2_settings)

        # Approve the review
        task = (
            db_session.query(ReviewTask)
            .filter(ReviewTask.episode_id == "ep_v2_e2e", ReviewTask.stage == "correct")
            .first()
        )
        assert task is not None
        approve_review(db_session, task.id)

        # Second run: should get past gate 1 and pause at gate 2
        db_session.refresh(v2_episode)
        report2 = run_episode_pipeline(db_session, v2_episode, v2_settings)

        assert report2.success is True
        gate1_result = next(s for s in report2.stages if s.stage == "review_gate_1")
        assert gate1_result.status == "success"

        # Should have translated + adapted, then paused at gate 2
        stage_statuses = {s.stage: s.status for s in report2.stages}
        assert stage_statuses.get("translate") == "success"
        assert stage_statuses.get("adapt") == "success"
        assert stage_statuses.get("review_gate_2") == "review_pending"

    @patch("btcedu.core.pipeline._run_stage")
    def test_full_pipeline_new_to_published(
        self, mock_stage, db_session, v2_episode, v2_settings, v2_files
    ):
        """Full pipeline: NEW → review_gate_1 → review_gate_2 → review_gate_3 → PUBLISHED."""
        from btcedu.core.reviewer import approve_review
        from btcedu.models.review import ReviewTask

        mock_stage.side_effect = self._make_stage_side_effect(db_session)

        # Run 1: pauses at gate 1
        run_episode_pipeline(db_session, v2_episode, v2_settings)
        db_session.refresh(v2_episode)
        assert v2_episode.status == EpisodeStatus.CORRECTED

        # Approve gate 1
        task1 = (
            db_session.query(ReviewTask)
            .filter(ReviewTask.episode_id == "ep_v2_e2e", ReviewTask.stage == "correct")
            .first()
        )
        approve_review(db_session, task1.id)

        # Run 2: pauses at gate 2
        db_session.refresh(v2_episode)
        run_episode_pipeline(db_session, v2_episode, v2_settings)
        db_session.refresh(v2_episode)
        assert v2_episode.status == EpisodeStatus.ADAPTED

        # Approve gate 2
        task2 = (
            db_session.query(ReviewTask)
            .filter(ReviewTask.episode_id == "ep_v2_e2e", ReviewTask.stage == "adapt")
            .first()
        )
        approve_review(db_session, task2.id)

        # Run 3: pauses at gate 3
        db_session.refresh(v2_episode)
        run_episode_pipeline(db_session, v2_episode, v2_settings)
        db_session.refresh(v2_episode)
        assert v2_episode.status == EpisodeStatus.RENDERED

        # Approve gate 3
        task3 = (
            db_session.query(ReviewTask)
            .filter(ReviewTask.episode_id == "ep_v2_e2e", ReviewTask.stage == "render")
            .first()
        )
        approve_review(db_session, task3.id)

        # Run 4: publishes
        db_session.refresh(v2_episode)
        report = run_episode_pipeline(db_session, v2_episode, v2_settings)
        db_session.refresh(v2_episode)

        assert report.success is True
        assert v2_episode.status == EpisodeStatus.PUBLISHED

        # Verify all 13 stages were reached across the 4 runs
        stage_statuses = {s.stage: s.status for s in report.stages}
        assert stage_statuses.get("review_gate_3") == "success"
        assert stage_statuses.get("publish") == "success"

    @patch("btcedu.core.pipeline._run_stage")
    def test_v2_plan_shows_all_18_stages(self, mock_stage, db_session, v2_episode, v2_settings):
        """resolve_pipeline_plan returns all 18 v2 stages."""
        plan = resolve_pipeline_plan(db_session, v2_episode, settings=v2_settings)
        assert len(plan) == 18
        stage_names = [p.stage for p in plan]
        assert stage_names == [
            "download",
            "transcribe",
            "transcript_analyze",
            "transcript_verify",
            "correct",
            "review_gate_1",
            "translate",
            "adapt",
            "review_gate_2",
            "chapterize",
            "frameextract",
            "imagegen",
            "review_gate_stock",
            "tts",
            "anchorgen",
            "render",
            "review_gate_3",
            "publish",
        ]

    def test_v1_plan_is_unchanged_by_transcript_stages(
        self,
        db_session,
        v2_settings,
    ):
        episode = Episode(
            episode_id="ep_v1_compat",
            source="youtube_rss",
            title="Legacy",
            url="https://example.com/v1",
            status=EpisodeStatus.TRANSCRIBED,
            pipeline_version=1,
        )
        db_session.add(episode)
        db_session.commit()

        plan = resolve_pipeline_plan(
            db_session,
            episode,
            settings=v2_settings,
        )

        assert "transcript_analyze" not in [item.stage for item in plan]
        assert "transcript_verify" not in [item.stage for item in plan]
        correct = next(item for item in plan if item.stage == "correct")
        assert correct.decision == "run"

    @patch("btcedu.core.pipeline._run_stage")
    def test_v2_cost_accumulation(self, mock_stage, db_session, v2_settings, tmp_path):
        """Costs from v2 stages are accumulated in PipelineReport."""
        ep = Episode(
            episode_id="ep_cost",
            source="youtube_rss",
            title="Cost Test",
            url="https://youtube.com/watch?v=cost",
            status=EpisodeStatus.TRANSLATED,
            pipeline_version=2,
        )
        db_session.add(ep)
        db_session.commit()

        # Simulate adapt returning with a cost (TRANSLATED → runs adapt)
        mock_stage.return_value = StageResult(
            "adapt", "success", 0.5, detail="3 adaptations (T1:2, T2:1, $0.0250)"
        )
        report = run_episode_pipeline(db_session, ep, v2_settings)

        assert report.total_cost_usd == pytest.approx(0.025, abs=0.001)


# ── imagegen stage dispatch (generative vs stock) ────────────────


class TestImagegenDispatch:
    """The imagegen stage routes by the profile's imagegen.provider:
    tagesschau_tr (provider: generative) → generate_images; other profiles
    fall back to the Pexels stock path."""

    def _v2_episode(self, db_session, profile):
        ep = Episode(
            episode_id=f"ep_img_{profile}",
            source="youtube_rss",
            title="Imagegen dispatch test",
            url="https://youtube.com/watch?v=img",
            status=EpisodeStatus.FRAMES_EXTRACTED,
            pipeline_version=2,
            content_profile=profile,
        )
        db_session.add(ep)
        db_session.commit()
        return ep

    def test_generative_profile_calls_generate_images(self, db_session, tmp_path):
        settings = _make_settings(tmp_path)
        settings.pipeline_version = 2
        ep = self._v2_episode(db_session, "tagesschau_tr")

        fake_result = MagicMock(
            skipped=False,
            generated_count=8,
            template_count=0,
            failed_count=0,
            cost_usd=0.12,
        )
        with (
            patch(
                "btcedu.core.image_generator.generate_images", return_value=fake_result
            ) as mock_gen,
            patch("btcedu.core.stock_images.search_stock_images") as mock_stock,
        ):
            result = _run_stage(db_session, ep, settings, "imagegen")

        mock_gen.assert_called_once()
        mock_stock.assert_not_called()
        assert result.status == "success"

    def test_default_profile_uses_stock_path(self, db_session, tmp_path):
        settings = _make_settings(tmp_path)
        settings.pipeline_version = 2
        ep = self._v2_episode(db_session, "bitcoin_podcast")

        rank_result = MagicMock(chapters_ranked=3, chapters_skipped=0, total_cost_usd=0.0)
        with (
            patch("btcedu.core.image_generator.generate_images") as mock_gen,
            patch("btcedu.core.stock_images.search_stock_images") as mock_search,
            patch(
                "btcedu.core.stock_images.rank_candidates", return_value=rank_result
            ) as mock_rank,
        ):
            result = _run_stage(db_session, ep, settings, "imagegen")

        mock_gen.assert_not_called()
        mock_search.assert_called_once()
        mock_rank.assert_called_once()
        assert result.status == "success"
