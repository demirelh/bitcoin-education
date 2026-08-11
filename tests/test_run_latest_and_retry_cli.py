"""CLI coverage for run-latest and retry (resume) commands.

`retry` is the established resume/restart command for failed episodes
(pipeline.retry_episode resumes from the episode's current/last-successful
status). These tests intentionally do not introduce a separate
resume/restart CLI command/alias — see cli.py docstring for `retry`.
"""

from unittest.mock import patch

from click.testing import CliRunner

from btcedu.cli import cli
from btcedu.config import Settings
from btcedu.core.pipeline import PipelineReport, StageResult
from btcedu.core.runlock import PipelineBusyError
from btcedu.models.episode import Episode, EpisodeStatus


@patch("btcedu.core.pipeline.run_latest")
def test_run_latest_command_no_pending_episodes(mock_run_latest, db_session, tmp_path):
    mock_run_latest.return_value = None
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["run-latest"],
        obj={
            "settings": Settings(reports_dir=str(tmp_path / "reports")),
            "session_factory": lambda: db_session,
        },
    )

    assert result.exit_code == 0
    assert "No pending episodes to process." in result.output


@patch("btcedu.core.pipeline.run_latest")
def test_run_latest_command_pipeline_busy_exits_nonzero(mock_run_latest, db_session, tmp_path):
    mock_run_latest.side_effect = PipelineBusyError("lock held")
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["run-latest"],
        obj={
            "settings": Settings(reports_dir=str(tmp_path / "reports")),
            "session_factory": lambda: db_session,
        },
    )

    assert result.exit_code == 1
    assert "Pipeline busy" in result.output
    assert "No pending episodes" not in result.output


@patch("btcedu.core.runlock.pipeline_lock")
def test_render_command_pipeline_busy_exits_nonzero(mock_pipeline_lock, db_session, tmp_path):
    mock_pipeline_lock.side_effect = PipelineBusyError("lock held")
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["render", "--episode-id", "ep-1"],
        obj={
            "settings": Settings(reports_dir=str(tmp_path / "reports")),
            "session_factory": lambda: db_session,
        },
    )

    assert result.exit_code == 1
    assert "Pipeline busy" in result.output


@patch("btcedu.core.pipeline.run_latest")
def test_run_latest_command_success(mock_run_latest, db_session, tmp_path):
    mock_run_latest.return_value = PipelineReport(
        episode_id="ep-1",
        title="Latest Episode",
        stages=[StageResult("download", "success", 1.2, detail="downloaded")],
        total_cost_usd=0.5,
        success=True,
    )
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["run-latest", "--profile", "tagesschau_tr", "--all-channels"],
        obj={
            "settings": Settings(reports_dir=str(tmp_path / "reports")),
            "session_factory": lambda: db_session,
        },
    )

    assert result.exit_code == 0
    assert "Episode: ep-1 (Latest Episode)" in result.output
    assert "-> OK ($0.5000)" in result.output
    assert mock_run_latest.call_args.kwargs["profile"] == "tagesschau_tr"
    assert mock_run_latest.call_args.kwargs["detect_all"] is True


@patch("btcedu.core.pipeline.run_latest")
def test_run_latest_command_failure_exits_nonzero(mock_run_latest, db_session, tmp_path):
    mock_run_latest.return_value = PipelineReport(
        episode_id="ep-1",
        title="Latest Episode",
        stages=[StageResult("transcribe", "failed", 0.4, error="Whisper API timeout")],
        total_cost_usd=0.1,
        success=False,
        error="Whisper API timeout",
    )
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["run-latest"],
        obj={
            "settings": Settings(reports_dir=str(tmp_path / "reports")),
            "session_factory": lambda: db_session,
        },
    )

    assert result.exit_code == 1
    assert "-> FAILED: Whisper API timeout" in result.output
    assert "Traceback" not in result.output
    # A clean, intentional sys.exit — not an unhandled exception/traceback.
    assert isinstance(result.exception, SystemExit)


@patch("btcedu.core.pipeline.retry_episode")
def test_retry_command_resumes_from_last_successful_stage(mock_retry, db_session, tmp_path):
    mock_retry.return_value = PipelineReport(
        episode_id="ep-1",
        title="Resumed Episode",
        stages=[StageResult("render", "success", 3.0, detail="rendered")],
        total_cost_usd=1.25,
        success=True,
    )
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["retry", "--episode-id", "ep-1"],
        obj={
            "settings": Settings(reports_dir=str(tmp_path / "reports")),
            "session_factory": lambda: db_session,
        },
    )

    assert result.exit_code == 0
    assert "[OK] ep-1: retry succeeded ($1.2500)" in result.output
    mock_retry.assert_called_once()
    assert mock_retry.call_args.args[1] == "ep-1"


@patch("btcedu.core.pipeline.retry_episode")
def test_retry_command_reports_pipeline_failure_and_exits_nonzero(mock_retry, db_session, tmp_path):
    mock_retry.return_value = PipelineReport(
        episode_id="ep-1",
        title="Resumed Episode",
        stages=[StageResult("render", "failed", 3.0, error="ffmpeg crashed")],
        total_cost_usd=0.75,
        success=False,
        error="ffmpeg crashed",
    )
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["retry", "--episode-id", "ep-1"],
        obj={
            "settings": Settings(reports_dir=str(tmp_path / "reports")),
            "session_factory": lambda: db_session,
        },
    )

    assert result.exit_code == 1
    assert "[FAIL] ep-1: ffmpeg crashed" in result.output


@patch("btcedu.core.pipeline.retry_episode")
def test_retry_command_rejects_episode_not_in_failed_state_without_traceback(
    mock_retry, db_session, tmp_path
):
    mock_retry.side_effect = ValueError(
        "Episode ep-1 is not in a failed state (status='downloaded', no error_message). "
        "Use 'run' instead."
    )
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["retry", "--episode-id", "ep-1"],
        obj={
            "settings": Settings(reports_dir=str(tmp_path / "reports")),
            "session_factory": lambda: db_session,
        },
    )

    assert result.exit_code == 1
    assert "[FAIL] ep-1: Episode ep-1 is not in a failed state" in result.output
    assert "Traceback" not in result.output
    # A clean, intentional sys.exit — not an unhandled exception/traceback.
    assert isinstance(result.exception, SystemExit)


@patch("btcedu.core.pipeline.retry_episode")
def test_retry_command_multiple_episodes_one_failure_still_reports_all(
    mock_retry, db_session, tmp_path
):
    def side_effect(session, episode_id, settings):
        if episode_id == "ep-bad":
            raise ValueError("Episode not found: ep-bad")
        return PipelineReport(
            episode_id=episode_id,
            title="Resumed",
            stages=[],
            total_cost_usd=0.1,
            success=True,
        )

    mock_retry.side_effect = side_effect
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["retry", "--episode-id", "ep-bad", "--episode-id", "ep-good"],
        obj={
            "settings": Settings(reports_dir=str(tmp_path / "reports")),
            "session_factory": lambda: db_session,
        },
    )

    assert result.exit_code == 1
    assert "[FAIL] ep-bad: Episode not found: ep-bad" in result.output
    assert "[OK] ep-good: retry succeeded ($0.1000)" in result.output


def test_retry_command_help_documents_resume_semantics():
    result = CliRunner().invoke(cli, ["retry", "--help"])

    assert result.exit_code == 0
    assert "Retry failed episodes from their last successful stage" in result.output


@patch("btcedu.core.runlock.pipeline_lock")
def test_run_command_pipeline_busy_exits_nonzero(mock_pipeline_lock, db_session, tmp_path):
    """``run`` must not start while another run holds the lock.

    Two concurrent runs share one SQLite database; the loser dies mid-stage
    with "database is locked" *after* it has already paid for that stage's
    API calls. A manual ``run`` collides with the autostart timer exactly as
    the batch entrypoints do, so it takes the same lock.
    """
    mock_pipeline_lock.side_effect = PipelineBusyError("lock held")
    db_session.add(
        Episode(
            episode_id="ep-1",
            title="Busy Episode",
            url="https://example.com/ep-1",
            status=EpisodeStatus.NEW,
        )
    )
    db_session.commit()
    runner = CliRunner()

    with patch("btcedu.core.pipeline.run_episode_pipeline") as mock_run:
        result = runner.invoke(
            cli,
            ["run", "--episode-id", "ep-1"],
            obj={
                "settings": Settings(reports_dir=str(tmp_path / "reports")),
                "session_factory": lambda: db_session,
            },
        )

    assert result.exit_code == 1
    assert "Pipeline busy" in result.output
    mock_run.assert_not_called()


@patch("btcedu.core.runlock.pipeline_lock")
def test_retry_command_pipeline_busy_exits_nonzero(mock_pipeline_lock, db_session, tmp_path):
    """``retry`` resumes the full pipeline and needs the same guard."""
    mock_pipeline_lock.side_effect = PipelineBusyError("lock held")
    runner = CliRunner()

    with patch("btcedu.core.pipeline.retry_episode") as mock_retry:
        result = runner.invoke(
            cli,
            ["retry", "--episode-id", "ep-1"],
            obj={
                "settings": Settings(reports_dir=str(tmp_path / "reports")),
                "session_factory": lambda: db_session,
            },
        )

    assert result.exit_code == 1
    assert "Pipeline busy" in result.output
    mock_retry.assert_not_called()
