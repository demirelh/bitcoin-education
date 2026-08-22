from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from btcedu.cli import cli
from btcedu.config import Settings
from btcedu.failover.types import FailoverMode, HeartbeatResponse, NodeRole


def _settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "reports_dir": str(tmp_path / "reports"),
        "failover_enabled": True,
        "failover_node_id": "btcedu-primary",
        "failover_node_role": "primary",
        "failover_control_plane_url": "http://127.0.0.1:8092",
        "failover_token": "node-secret-token",
    }
    values.update(overrides)
    return Settings(**values)


def test_failover_heartbeat_command_noops_when_disabled(tmp_path):
    result = CliRunner().invoke(
        cli,
        ["failover-heartbeat"],
        obj={
            "settings": Settings(reports_dir=str(tmp_path / "reports"), failover_enabled=False),
            "session_factory": lambda: MagicMock(),
        },
    )

    assert result.exit_code == 0
    assert "Failover disabled." in result.output


def test_failover_heartbeat_command_prints_status(tmp_path):
    fake_client = MagicMock()
    fake_client.heartbeat.return_value = HeartbeatResponse(
        mode=FailoverMode.AUTOMATIC,
        effective_owner_role=NodeRole.PRIMARY,
        effective_owner_node="btcedu-primary",
        eligible=True,
    )

    with (
        patch(
            "btcedu.services.failover_service.FailoverControlPlaneClient.from_settings",
            return_value=fake_client,
        ),
        patch("btcedu.services.failover_service.build_node_health", return_value={"ok": True}),
        patch("btcedu.services.failover_service.failover_boot_id", return_value="boot-1"),
        patch("btcedu.version.get_git_commit", return_value="abc123"),
    ):
        result = CliRunner().invoke(
            cli,
            ["failover-heartbeat"],
            obj={
                "settings": _settings(tmp_path),
                "session_factory": lambda: MagicMock(),
            },
        )

    assert result.exit_code == 0
    assert "mode=automatic owner=primary/btcedu-primary eligible=yes" in result.output


def test_failover_reconcile_command_calls_operator_client(tmp_path):
    fake_client = MagicMock()
    fake_client.reconcile_broadcast.return_value = {
        "resource": "publish:tagesschau_tr/2000/2026-08-22",
        "status": "published",
        "fencing_token": 7,
        "youtube_id": "yt123",
    }

    with (
        patch(
            "btcedu.services.failover_service.load_failover_operator_config",
            return_value=object(),
        ),
        patch(
            "btcedu.services.failover_service.FailoverControlPlaneClient.operator_from_settings",
            return_value=fake_client,
        ),
    ):
        result = CliRunner().invoke(
            cli,
            [
                "failover-reconcile",
                "--resource",
                "publish:tagesschau_tr/2000/2026-08-22",
                "--resolution",
                "completed",
                "--status",
                "published",
                "--youtube-id",
                "yt123",
            ],
            obj={
                "settings": _settings(
                    tmp_path,
                    failover_operator_token="operator-secret-token",
                ),
                "session_factory": lambda: MagicMock(),
            },
        )

    assert result.exit_code == 0
    fake_client.reconcile_broadcast.assert_called_once_with(
        resource="publish:tagesschau_tr/2000/2026-08-22",
        resolution="completed",
        status="published",
        youtube_id="yt123",
    )
    assert "status=published" in result.output
    assert "youtube_id=yt123" in result.output


def test_failover_reconcile_help_includes_examples():
    result = CliRunner().invoke(cli, ["failover-reconcile", "--help"])

    assert result.exit_code == 0
    assert "Examples:" in result.output
    assert "publish:tagesschau_tr/2000/2026-08-22" in result.output
