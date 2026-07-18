"""CLI coverage for selective transcript verification."""

from types import SimpleNamespace
from unittest.mock import patch

from click.testing import CliRunner

from btcedu.cli import cli
from btcedu.config import Settings


@patch("btcedu.core.transcript_verifier.verify_transcript")
def test_transcript_verify_command_supports_force_and_dry_run(
    mock_verify,
    db_session,
):
    mock_verify.return_value = SimpleNamespace(
        skipped=True,
        reason="dry-run: would verify 2 region(s)",
        regions_checked=2,
        critical_count=0,
        cost_usd=0,
    )
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "transcript-verify",
            "--episode-id",
            "episode-1",
            "--force",
            "--dry-run",
        ],
        obj={
            "settings": Settings(),
            "session_factory": lambda: db_session,
        },
    )

    assert result.exit_code == 0
    assert "dry-run: would verify 2 region(s)" in result.output
    assert mock_verify.call_args.kwargs["force"] is True
    assert mock_verify.call_args.kwargs["dry_run"] is True
