from unittest.mock import patch

from click.testing import CliRunner

from btcedu.cli import cli
from btcedu.config import Settings


def test_youtube_status_reports_selected_target(tmp_path):
    settings = Settings(
        youtube_test_credentials_path=str(tmp_path / "missing.json"),
        youtube_test_channel_id="UC_TEST",
    )

    result = CliRunner().invoke(
        cli,
        ["youtube-status", "--target", "test"],
        obj={"settings": settings},
    )

    assert result.exit_code == 0
    assert "Target           : test" in result.output
    assert "Expected channel : UC_TEST" in result.output
    assert "videos.insert 1 call" in result.output


def test_youtube_auth_uses_production_paths(tmp_path):
    settings = Settings(
        youtube_production_client_secrets_path=str(tmp_path / "prod-client.json"),
        youtube_production_credentials_path=str(tmp_path / "prod-token.json"),
        youtube_production_channel_id="UC_PRODUCTION",
    )

    with patch(
        "btcedu.services.youtube_service.authenticate",
        return_value={"channel_id": "UC_PRODUCTION", "channel_name": "ALMANYA24"},
    ) as authenticate:
        result = CliRunner().invoke(
            cli,
            ["youtube-auth", "--target", "production"],
            obj={"settings": settings},
        )

    assert result.exit_code == 0
    authenticate.assert_called_once_with(
        client_secrets_path=str(tmp_path / "prod-client.json"),
        credentials_path=str(tmp_path / "prod-token.json"),
        expected_channel_id="UC_PRODUCTION",
        target_name="production",
    )
    assert "YouTube target    : production" in result.output
    assert "ALMANYA24" in result.output


def test_publish_help_exposes_target_and_review_workflow():
    runner = CliRunner()

    publish_help = runner.invoke(cli, ["publish", "--help"])
    review_help = runner.invoke(cli, ["publish-review", "--help"])

    assert publish_help.exit_code == 0
    assert "--target [test|production]" in publish_help.output
    assert review_help.exit_code == 0
    assert "artifact-bound final review" in review_help.output


def test_publish_command_exits_nonzero_when_upload_fails():
    session = type("Session", (), {"close": lambda self: None})()

    with patch(
        "btcedu.core.publisher.publish_video",
        side_effect=RuntimeError("upload failed"),
    ):
        result = CliRunner().invoke(
            cli,
            ["publish", "--episode-id", "ep-1"],
            obj={"settings": Settings(), "session_factory": lambda: session},
        )

    assert result.exit_code == 1
    assert "[FAIL] ep-1: upload failed" in result.output
