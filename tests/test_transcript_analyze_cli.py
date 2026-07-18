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
        critical_count=1,
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
    mock_analyze.assert_called_once()
    assert mock_analyze.call_args.kwargs["force"] is True
