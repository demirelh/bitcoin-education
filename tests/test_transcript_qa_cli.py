"""CLI coverage for the deterministic transcript QA gate."""

from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from btcedu.cli import cli
from btcedu.config import Settings


@patch("btcedu.core.transcript_qa.load_transcript_qa")
@patch("btcedu.core.transcript_qa.evaluate_transcript_qa")
def test_transcript_qa_command_supports_force(
    mock_evaluate,
    mock_load,
    db_session,
):
    mock_evaluate.return_value = SimpleNamespace(
        skipped=False,
        reason="",
        status="green",
        blocked=False,
        finding_count=0,
        blocking_count=0,
    )
    mock_load.return_value = {
        "summary": {
            "critical_count": 0,
            "major_count": 0,
            "minor_count": 0,
            "blocking_count": 0,
        },
        "findings": [],
    }
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["transcript-qa", "--episode-id", "episode-1", "--force"],
        obj={"settings": Settings(), "session_factory": lambda: db_session},
    )

    assert result.exit_code == 0
    assert "[GREEN]" in result.output
    assert "cost=$0.0000 (deterministic, zero-cost)" in result.output
    mock_evaluate.assert_called_once()
    assert mock_evaluate.call_args.kwargs["force"] is True


@patch("btcedu.core.transcript_qa.load_transcript_qa")
@patch("btcedu.core.transcript_qa.evaluate_transcript_qa")
def test_transcript_qa_command_blocked_result_exits_2_and_lists_findings(
    mock_evaluate,
    mock_load,
    db_session,
):
    mock_evaluate.return_value = SimpleNamespace(
        skipped=False,
        reason="",
        status="red",
        blocked=True,
        finding_count=1,
        blocking_count=1,
    )
    mock_load.return_value = {
        "summary": {
            "critical_count": 1,
            "major_count": 0,
            "minor_count": 0,
            "blocking_count": 1,
        },
        "findings": [
            {
                "severity": "critical",
                "category": "unresolved_negation",
                "segment_ids": ["seg-1"],
                "start_seconds": 1.0,
                "end_seconds": 2.5,
                "message": "Negation could not be resolved.",
                "blocking": True,
            }
        ],
    }
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["transcript-qa", "--episode-id", "episode-1"],
        obj={"settings": Settings(), "session_factory": lambda: db_session},
    )

    assert result.exit_code == 2
    assert "[RED]" in result.output
    assert "BLOCKED" in result.output
    assert "Negation could not be resolved." in result.output


@patch("btcedu.core.transcript_qa.load_transcript_qa")
@patch("btcedu.core.transcript_qa.evaluate_transcript_qa")
def test_transcript_qa_command_skip_without_artifact(
    mock_evaluate,
    mock_load,
    db_session,
):
    mock_evaluate.return_value = SimpleNamespace(
        skipped=True,
        reason="transcript QA disabled by profile",
        status="green",
        blocked=False,
        finding_count=0,
        blocking_count=0,
    )
    mock_load.return_value = None
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["transcript-qa", "--episode-id", "episode-1"],
        obj={"settings": Settings(), "session_factory": lambda: db_session},
    )

    assert result.exit_code == 0
    assert "[SKIP] episode-1 -> transcript QA disabled by profile" in result.output


@patch("btcedu.core.transcript_qa.evaluate_transcript_qa")
def test_transcript_qa_command_reports_failure_without_traceback(
    mock_evaluate,
    db_session,
):
    mock_evaluate.side_effect = FileNotFoundError(
        "Structured corrected transcript not found: /tmp/x.json"
    )
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["transcript-qa", "--episode-id", "episode-1"],
        obj={"settings": Settings(), "session_factory": lambda: db_session},
    )

    assert result.exit_code == 1
    assert "[FAIL] episode-1:" in result.output
    assert "Traceback" not in result.output
    # A clean, intentional sys.exit — not an unhandled exception/traceback.
    assert isinstance(result.exception, SystemExit)


def test_transcript_qa_command_help():
    result = CliRunner().invoke(cli, ["transcript-qa", "--help"])

    assert result.exit_code == 0
    assert "--episode-id" in result.output
    assert "--force" in result.output
    assert "--dry-run" not in result.output
