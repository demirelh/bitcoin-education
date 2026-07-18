"""CLI coverage for the deterministic transcript analysis stage."""

from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from btcedu.cli import cli
from btcedu.config import Settings


@patch("btcedu.core.transcript_analyzer.analyze_transcript")
def test_transcript_analyze_command_supports_force(
    mock_analyze,
    db_session,
):
    mock_analyze.return_value = SimpleNamespace(
        skipped=False,
        suspicious_count=2,
        segment_count=10,
        critical_count=0,
    )
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["transcript-analyze", "--episode-id", "episode-1", "--force"],
        obj={
            "settings": Settings(),
            "session_factory": lambda: db_session,
        },
    )

    assert result.exit_code == 0
    assert "2/10 suspicious" in result.output
    assert "[YELLOW]" in result.output
    assert "cost=$0.0000 (deterministic, zero-cost)" in result.output
    mock_analyze.assert_called_once()
    assert mock_analyze.call_args.kwargs["force"] is True


@patch("btcedu.core.transcript_analyzer.analyze_transcript")
def test_transcript_analyze_command_clean_result_is_green(
    mock_analyze,
    db_session,
):
    mock_analyze.return_value = SimpleNamespace(
        skipped=False,
        suspicious_count=0,
        segment_count=10,
        critical_count=0,
    )
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["transcript-analyze", "--episode-id", "episode-1"],
        obj={"settings": Settings(), "session_factory": lambda: db_session},
    )

    assert result.exit_code == 0
    assert "[GREEN]" in result.output


@patch("btcedu.core.transcript_analyzer.analyze_transcript")
def test_transcript_analyze_command_critical_finding_exits_2(
    mock_analyze,
    db_session,
):
    mock_analyze.return_value = SimpleNamespace(
        skipped=False,
        suspicious_count=2,
        segment_count=10,
        critical_count=1,
    )
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["transcript-analyze", "--episode-id", "episode-1"],
        obj={"settings": Settings(), "session_factory": lambda: db_session},
    )

    assert result.exit_code == 2
    assert "[RED]" in result.output


@patch("btcedu.core.transcript_analyzer.analyze_transcript")
def test_transcript_analyze_command_reports_failure_without_traceback(
    mock_analyze,
    db_session,
):
    mock_analyze.side_effect = ValueError("Episode not found: episode-1")
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["transcript-analyze", "--episode-id", "episode-1"],
        obj={"settings": Settings(), "session_factory": lambda: db_session},
    )

    assert result.exit_code == 1
    assert "[FAIL] episode-1: Episode not found: episode-1" in result.output
    assert "Traceback" not in result.output
    # A clean, intentional sys.exit — not an unhandled exception/traceback.
    assert isinstance(result.exception, SystemExit)


@patch("btcedu.core.transcript_analyzer.analyze_transcript")
def test_transcript_analyze_command_failure_takes_priority_over_blocking(
    mock_analyze,
    db_session,
):
    def side_effect(session, episode_id, settings, force=False):
        if episode_id == "episode-1":
            raise ValueError("boom")
        return SimpleNamespace(
            skipped=False, suspicious_count=1, segment_count=5, critical_count=1
        )

    mock_analyze.side_effect = side_effect
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "transcript-analyze",
            "--episode-id",
            "episode-1",
            "--episode-id",
            "episode-2",
        ],
        obj={"settings": Settings(), "session_factory": lambda: db_session},
    )

    # An execution failure always wins over a RED/blocking result.
    assert result.exit_code == 1
    assert "[FAIL] episode-1: boom" in result.output
    assert "[RED] episode-2" in result.output
