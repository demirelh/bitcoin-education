from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from btcedu.config import Settings
from btcedu.core.pipeline import (
    PipelineReport,
    StageResult,
    run_episode_pipeline,
    run_episode_pipeline_coordinated,
    run_latest,
    run_pending,
)
from btcedu.failover.types import (
    FailoverMode,
    HeartbeatResponse,
    LeaseAcquisition,
    LeaseInfo,
    NodeRole,
)
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.services.failover_service import FailoverExecutionRejected


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "database_url": f"sqlite:///{tmp_path / 'pipeline.db'}",
        "raw_data_dir": str(tmp_path / "raw"),
        "transcripts_dir": str(tmp_path / "transcripts"),
        "outputs_dir": str(tmp_path / "outputs"),
        "reports_dir": str(tmp_path / "reports"),
        "dry_run": True,
        "pipeline_version": 2,
    }
    values.update(overrides)
    return Settings(**values)


def test_run_episode_pipeline_fails_closed_when_lease_is_lost(db_session, tmp_path):
    episode = Episode(
        episode_id="ep-failover-loss",
        source="youtube_rss",
        title="Lease Loss",
        url="https://example.com/ep-failover-loss",
        status=EpisodeStatus.NEW,
        published_at=datetime(2026, 8, 22, tzinfo=UTC),
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    guard = MagicMock()
    guard.ensure_active.side_effect = [None, RuntimeError("control plane unreachable")]

    with patch("btcedu.core.pipeline._run_stage") as run_stage:
        run_stage.return_value = StageResult("download", "success", 0.1, detail="ok")
        report = run_episode_pipeline(db_session, episode, _settings(tmp_path), lease_guard=guard)

    assert report.success is False
    assert "control plane unreachable" in (report.error or "")
    db_session.refresh(episode)
    assert "control plane unreachable" in (episode.error_message or "")


def test_run_latest_skips_when_failover_rejects_selected_episode(db_session, tmp_path):
    episode = Episode(
        episode_id="ep-owned-elsewhere",
        source="youtube_rss",
        title="Owned Elsewhere",
        url="https://example.com/owned",
        status=EpisodeStatus.NEW,
        published_at=datetime(2026, 8, 22, tzinfo=UTC),
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    settings = _settings(tmp_path)
    with (
        patch("btcedu.core.detector.detect_local_recordings") as local_detect,
        patch("btcedu.core.detector.detect_episodes") as feed_detect,
        patch(
            "btcedu.core.pipeline.run_episode_pipeline_coordinated",
            side_effect=FailoverExecutionRejected("owner primary/btcedu-primary"),
        ),
    ):
        local_detect.return_value = MagicMock(found=0, new=0, total=1)
        feed_detect.return_value = MagicMock(found=0, new=0, total=1)

        report = run_latest(db_session, settings)

    assert report is None


def test_run_pending_skips_rejected_episode_and_continues(db_session, tmp_path):
    older = Episode(
        episode_id="ep-skip",
        source="youtube_rss",
        title="Skip",
        url="https://example.com/skip",
        status=EpisodeStatus.NEW,
        published_at=datetime(2026, 8, 21, tzinfo=UTC),
        pipeline_version=2,
    )
    newer = Episode(
        episode_id="ep-run",
        source="youtube_rss",
        title="Run",
        url="https://example.com/run",
        status=EpisodeStatus.NEW,
        published_at=datetime(2026, 8, 22, tzinfo=UTC),
        pipeline_version=2,
    )
    db_session.add_all([older, newer])
    db_session.commit()

    success_report = PipelineReport(
        episode_id="ep-run",
        title="Run",
        stages=[StageResult("download", "success", 0.1, detail="ok")],
        success=True,
    )
    settings = _settings(tmp_path)

    with patch(
        "btcedu.core.pipeline.run_episode_pipeline_coordinated",
        side_effect=[FailoverExecutionRejected("owner primary"), success_report],
    ):
        reports = run_pending(db_session, settings)

    assert [report.episode_id for report in reports] == ["ep-run"]


def test_run_episode_pipeline_coordinated_reports_completion(db_session, tmp_path):
    episode = Episode(
        episode_id="ep-coordinated",
        source="youtube_rss",
        title="Coordinated",
        url="https://example.com/coordinated",
        status=EpisodeStatus.NEW,
        published_at=datetime(2026, 8, 22, tzinfo=UTC),
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    guard = MagicMock()
    guard.close.return_value = None
    guard.report_completion.return_value = None

    with (
        patch(
            "btcedu.failover.coordination.acquire_pipeline_lease_guard",
            return_value=guard,
        ),
        patch("btcedu.core.pipeline.run_episode_pipeline") as runner,
    ):
        runner.return_value = PipelineReport(
            episode_id=episode.episode_id,
            title=episode.title,
            stages=[StageResult("download", "success", 0.1, detail="ok")],
            success=True,
        )
        report = run_episode_pipeline_coordinated(db_session, episode, _settings(tmp_path))

    assert report.success is True
    guard.report_completion.assert_called_once()
    guard.close.assert_called_once()


def test_run_episode_pipeline_coordinated_records_processing_before_pipeline_execution(
    db_session, tmp_path
):
    episode = Episode(
        episode_id="ep-coordinated-processing",
        source="youtube_rss",
        title="Coordinated Processing",
        url="https://example.com/coordinated-processing",
        status=EpisodeStatus.NEW,
        published_at=datetime(2026, 8, 22, tzinfo=UTC),
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    fake_client = MagicMock()
    fake_client.heartbeat.return_value = HeartbeatResponse(
        mode=FailoverMode.AUTOMATIC,
        effective_owner_role=NodeRole.PRIMARY,
        effective_owner_node="btcedu-primary",
        eligible=True,
    )
    fake_client.acquire_lease.return_value = LeaseAcquisition(
        acquired=True,
        lease=LeaseInfo(
            fencing_token=1,
            expires_at=datetime(2026, 8, 22, 12, 10, tzinfo=UTC),
            resource="pipeline:bitcoin_podcast/0000/2026-08-22",
            lease_token="lease-1",
            node_id="btcedu-primary",
            role=NodeRole.PRIMARY,
        ),
    )
    fake_client.release_lease.return_value = True
    fake_client.complete_broadcast.return_value = {"recorded": True}

    settings = _settings(
        tmp_path,
        failover_enabled=True,
        failover_node_id="btcedu-primary",
        failover_node_role="primary",
        failover_control_plane_url="http://127.0.0.1:8092",
        failover_token="node-secret-token",
    )

    def _runner(*_args, **_kwargs):
        first_call = fake_client.complete_broadcast.call_args_list[0].kwargs
        assert first_call["status"] == "processing"
        episode.status = EpisodeStatus.CORRECTED
        db_session.commit()
        return PipelineReport(
            episode_id=episode.episode_id,
            title=episode.title,
            stages=[StageResult("download", "success", 0.1, detail="ok")],
            success=True,
        )

    with (
        patch(
            "btcedu.failover.coordination.FailoverControlPlaneClient.from_settings",
            return_value=fake_client,
        ),
        patch("btcedu.failover.coordination.build_node_health", return_value={"ok": True}),
        patch("btcedu.failover.coordination.failover_boot_id", return_value="boot-1"),
        patch("btcedu.failover.coordination.get_git_commit", return_value="abc123"),
        patch("btcedu.core.pipeline.run_episode_pipeline", side_effect=_runner),
    ):
        report = run_episode_pipeline_coordinated(db_session, episode, settings)

    assert report.success is True
    statuses = [call.kwargs["status"] for call in fake_client.complete_broadcast.call_args_list]
    assert statuses == ["processing", EpisodeStatus.CORRECTED.value]


def test_run_episode_pipeline_coordinated_preserves_uploaded_completion_status(
    db_session, tmp_path
):
    episode = Episode(
        episode_id="ep-coordinated-uploaded",
        source="youtube_rss",
        title="Uploaded",
        url="https://example.com/uploaded",
        status=EpisodeStatus.NEW,
        published_at=datetime(2026, 8, 22, tzinfo=UTC),
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    guard = MagicMock()
    guard.close.return_value = None
    guard.report_completion.return_value = None

    def _runner(*_args, **_kwargs):
        episode.status = EpisodeStatus.PUBLISHED
        episode.youtube_video_id = "YT123"
        db_session.commit()
        return PipelineReport(
            episode_id=episode.episode_id,
            title=episode.title,
            stages=[StageResult("publish", "failed", 0.1, error="coordination failed")],
            success=False,
            error="coordination failed",
        )

    with (
        patch(
            "btcedu.failover.coordination.acquire_pipeline_lease_guard",
            return_value=guard,
        ),
        patch("btcedu.core.pipeline.run_episode_pipeline", side_effect=_runner),
    ):
        report = run_episode_pipeline_coordinated(db_session, episode, _settings(tmp_path))

    assert report.success is False
    guard.report_completion.assert_called_once_with(status="published", youtube_id="YT123")
    guard.close.assert_called_once()
