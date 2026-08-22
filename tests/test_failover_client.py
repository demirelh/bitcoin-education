from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

from btcedu.config import Settings
from btcedu.failover.types import FailoverMode, NodeRole
from btcedu.services.failover_service import (
    FailoverControlPlaneClient,
    FailoverRequestError,
    load_failover_client_config,
)


def _settings(tmp_path: Path, **overrides) -> Settings:
    token_file = tmp_path / "failover.token"
    token_file.write_text("node-secret-token", encoding="utf-8")
    values = {
        "failover_enabled": True,
        "failover_node_id": "btcedu-primary",
        "failover_node_role": "primary",
        "failover_control_plane_url": "http://127.0.0.1:8092",
        "failover_token_file": str(token_file),
        "failover_request_timeout_seconds": 5,
    }
    values.update(overrides)
    return Settings(**values)


def _response(status_code: int, payload: dict):
    response = MagicMock()
    response.ok = 200 <= status_code < 300
    response.status_code = status_code
    response.json.return_value = payload
    return response


def test_load_failover_config_reads_token_file(tmp_path):
    settings = _settings(tmp_path)

    config = load_failover_client_config(settings)

    assert config is not None
    assert config.enabled is True
    assert config.node_id == "btcedu-primary"
    assert config.node_role == NodeRole.PRIMARY
    assert config.token == "node-secret-token"


def test_status_parses_typed_payload(tmp_path):
    settings = _settings(tmp_path)
    client = FailoverControlPlaneClient.from_settings(settings)

    with patch("btcedu.services.failover_service.requests.request") as request:
        request.return_value = _response(
            200,
            {
                "mode": "automatic",
                "effective_owner_role": "primary",
                "effective_owner_node": "btcedu-primary",
                "nodes": [
                    {
                        "node_id": "btcedu-primary",
                        "role": "primary",
                        "boot_id": "boot-1",
                        "git_commit": "abc123",
                        "healthy": True,
                        "eligible": True,
                        "last_heartbeat_at": "2026-08-22T10:00:00+00:00",
                        "healthy_since": "2026-08-22T10:00:00+00:00",
                        "unhealthy_since": None,
                        "health": {"ok": True},
                    }
                ],
                "active_leases": [
                    {
                        "resource": "pipeline:tagesschau_tr/2000/2026-08-22",
                        "node_id": "btcedu-primary",
                        "role": "primary",
                        "fencing_token": 5,
                        "expires_at": "2026-08-22T10:10:00+00:00",
                    }
                ],
            },
        )

        status = client.status()

    assert status.mode.value == "automatic"
    assert status.effective_owner_role == NodeRole.PRIMARY
    assert status.nodes[0].node_id == "btcedu-primary"
    assert status.nodes[0].eligible is True
    assert status.active_leases[0].resource == "pipeline:tagesschau_tr/2000/2026-08-22"
    assert status.active_leases[0].lease_token is None


def test_acquire_lease_returns_conflict_without_raising(tmp_path):
    settings = _settings(tmp_path)
    client = FailoverControlPlaneClient.from_settings(settings)

    with patch("btcedu.services.failover_service.requests.request") as request:
        request.return_value = _response(
            409,
            {
                "error": "resource already has an active lease",
                "acquired": False,
                "lease": {
                    "resource": "pipeline:broadcast",
                    "node_id": "btcedu-primary",
                    "role": "primary",
                    "fencing_token": 4,
                    "expires_at": "2026-08-22T10:10:00+00:00",
                },
            },
        )

        lease = client.acquire_lease(resource="pipeline:broadcast", ttl_seconds=540)

    assert lease.acquired is False
    assert lease.error == "resource already has an active lease"
    assert lease.lease is not None
    assert lease.lease.fencing_token == 4
    assert lease.lease.lease_token is None


def test_timeout_raises_explicit_error(tmp_path):
    settings = _settings(tmp_path)
    client = FailoverControlPlaneClient.from_settings(settings)

    with patch(
        "btcedu.services.failover_service.requests.request",
        side_effect=requests.Timeout(),
    ):
        with pytest.raises(FailoverRequestError, match="timed out"):
            client.status()


def test_set_mode_serializes_enum_value(tmp_path):
    settings = _settings(tmp_path)
    client = FailoverControlPlaneClient.from_settings(settings)

    with patch("btcedu.services.failover_service.requests.request") as request:
        request.return_value = _response(
            200,
            {
                "mode": "force_secondary",
                "effective_owner_role": "secondary",
                "effective_owner_node": "btcedu-secondary",
                "nodes": [],
                "active_leases": [],
            },
        )

        client.set_mode(FailoverMode.FORCE_SECONDARY)

    assert request.call_args.kwargs["json"] == {"mode": "force_secondary"}
