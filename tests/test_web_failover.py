from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.config import Settings
from btcedu.failover.types import (
    FailoverMode,
    FailoverStatusSnapshot,
    LeaseInfo,
    NodeRole,
    NodeStatusSnapshot,
)


@pytest.fixture
def app(tmp_path: Path):
    from btcedu.web.app import create_app

    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'web.db'}",
        raw_data_dir=str(tmp_path / "raw"),
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        logs_dir=str(tmp_path / "logs"),
        failover_enabled=True,
        failover_control_plane_url="http://127.0.0.1:8092",
        failover_node_id="btcedu-primary",
        failover_node_role="primary",
        failover_token="node-secret-token",
        failover_operator_token="operator-secret-token",
    )
    application = create_app(settings=settings)
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    return app.test_client()


def test_failover_status_proxy_endpoint(client):
    payload = {
        "enabled": True,
        "operator_configured": True,
        "mode": "automatic",
        "effective_owner_role": "primary",
        "effective_owner_node": "btcedu-primary",
        "nodes": [],
        "active_leases": [],
    }
    with patch("btcedu.services.failover_service.proxy_failover_status", return_value=payload):
        response = client.get("/api/failover/status")

    assert response.status_code == 200
    assert response.get_json()["effective_owner_node"] == "btcedu-primary"


def test_failover_mode_proxy_endpoint(client):
    snapshot = FailoverStatusSnapshot(
        mode=FailoverMode.FORCE_SECONDARY,
        effective_owner_role=None,
        effective_owner_node=None,
        nodes=[
            NodeStatusSnapshot(
                node_id="btcedu-secondary",
                role=NodeRole.SECONDARY,
                boot_id="boot-2",
                git_commit="abc123",
                healthy=True,
                eligible=True,
                last_heartbeat_at=None,
                healthy_since=None,
                unhealthy_since=None,
                health={"ok": True},
            )
        ],
        active_leases=[
            LeaseInfo(
                lease_token="lease-1",
                fencing_token=7,
                expires_at=datetime(2026, 8, 22, 10, 5, tzinfo=UTC),
                resource="publish:tagesschau_tr/2000/2026-08-22",
                node_id="btcedu-secondary",
                role=NodeRole.SECONDARY,
            )
        ],
    )

    fake_client = MagicMock()
    fake_client.set_mode.return_value = snapshot
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
        response = client.post("/api/failover/mode", json={"mode": "force_secondary"})

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["mode"] == "force_secondary"
    assert payload["nodes"][0]["node_id"] == "btcedu-secondary"
    assert payload["active_leases"][0]["resource"] == "publish:tagesschau_tr/2000/2026-08-22"
    assert "lease_token" not in payload["active_leases"][0]


def test_failover_mode_proxy_rejects_disabled_control_plane(app):
    app.config["settings"].failover_enabled = False
    client = app.test_client()

    response = client.post("/api/failover/mode", json={"mode": "paused"})

    assert response.status_code == 400
    assert "disabled" in response.get_json()["error"]
