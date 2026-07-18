"""CLI coverage for the deterministic translation QA checks."""

from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from btcedu.cli import cli
from btcedu.config import Settings


@patch("btcedu.core.translation_qa.load_translation_qa")
@patch("btcedu.core.translation_qa.run_translation_qa")
def test_translation_qa_command_supports_force(
    mock_run,
    mock_load,
    db_session,
):
    mock_run.return_value = SimpleNamespace(
        skipped=False,
        reason="",
        status="green",
        finding_count=0,
        critical_count=0,
    )
    mock_load.return_value = {
        "summary": {
            "critical_count": 0,
            "major_count": 0,
            "minor_count": 0,
            "info_count": 0,
        },
        "findings": [],
    }
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["translation-qa", "--episode-id", "episode-1", "--force"],
        obj={"settings": Settings(), "session_factory": lambda: db_session},
    )

    assert result.exit_code == 0
    assert "[GREEN]" in result.output
    assert "cost=$0.0000 (deterministic, zero-cost)" in result.output
    mock_run.assert_called_once()
    assert mock_run.call_args.kwargs["force"] is True


@patch("btcedu.core.translation_qa.load_translation_qa")
@patch("btcedu.core.translation_qa.run_translation_qa")
def test_translation_qa_command_red_result_exits_2_and_lists_findings(
    mock_run,
    mock_load,
    db_session,
):
    mock_run.return_value = SimpleNamespace(
        skipped=False,
        reason="",
        status="red",
        finding_count=1,
        critical_count=1,
    )
    mock_load.return_value = {
        "summary": {
            "critical_count": 1,
            "major_count": 0,
            "minor_count": 0,
            "info_count": 0,
        },
        "findings": [
            {
                "severity": "critical",
                "category": "numeric_mismatch",
                "story_id": "story-1",
                "explanation": "12 vs 21 casualties.",
            }
        ],
    }
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["translation-qa", "--episode-id", "episode-1"],
        obj={"settings": Settings(), "session_factory": lambda: db_session},
    )

    assert result.exit_code == 2
    assert "[RED]" in result.output
    assert "12 vs 21 casualties." in result.output


@patch("btcedu.core.translation_qa.load_translation_qa")
@patch("btcedu.core.translation_qa.run_translation_qa")
def test_translation_qa_command_skip_without_artifact(
    mock_run,
    mock_load,
    db_session,
):
    mock_run.return_value = SimpleNamespace(
        skipped=True,
        reason="story-based source or target artifact missing",
        status="green",
        finding_count=0,
        critical_count=0,
    )
    mock_load.return_value = None
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["translation-qa", "--episode-id", "episode-1"],
        obj={"settings": Settings(), "session_factory": lambda: db_session},
    )

    assert result.exit_code == 1
    assert "[SKIP] episode-1 -> story-based source or target artifact missing" in result.output


@patch("btcedu.core.translation_qa.run_translation_qa")
def test_translation_qa_command_reports_failure_without_traceback(
    mock_run,
    db_session,
):
    mock_run.side_effect = ValueError("Episode not found: episode-1")
    runner = CliRunner()

    result = runner.invoke(
        cli,
        ["translation-qa", "--episode-id", "episode-1"],
        obj={"settings": Settings(), "session_factory": lambda: db_session},
    )

    assert result.exit_code == 1
    assert "[FAIL] episode-1: Episode not found: episode-1" in result.output
    assert "Traceback" not in result.output
    # A clean, intentional sys.exit — not an unhandled exception/traceback.
    assert isinstance(result.exception, SystemExit)


def test_translation_qa_command_help():
    result = CliRunner().invoke(cli, ["translation-qa", "--help"])

    assert result.exit_code == 0
    assert "--episode-id" in result.output
    assert "--force" in result.output
    assert "--dry-run" not in result.output
