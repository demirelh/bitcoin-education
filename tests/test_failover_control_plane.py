import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from btcedu.failover.control_plane import ControlPlaneSettings, create_app


def _write_file(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def control_plane_env(tmp_path: Path):
    primary_token = "primary-secret-token"
    secondary_token = "secondary-secret-token"
    operator_token = "operator-secret-token"

    node_tokens = {
        "btcedu-primary": {
            "role": "primary",
            "token_file": str(_write_file(tmp_path / "primary.token", primary_token)),
        },
        "btcedu-secondary": {
            "role": "secondary",
            "token_file": str(_write_file(tmp_path / "secondary.token", secondary_token)),
        },
    }
    node_tokens_file = _write_file(tmp_path / "node_tokens.json", json.dumps(node_tokens))
    operator_token_file = _write_file(tmp_path / "operator.token", operator_token)

    settings = ControlPlaneSettings(
        database_path=str(tmp_path / "control_plane.db"),
        node_tokens_file=str(node_tokens_file),
        operator_token_file=str(operator_token_file),
        heartbeat_stale_seconds=180,
        takeover_grace_seconds=420,
        stable_primary_recovery_seconds=300,
    )
    return {
        "settings": settings,
        "primary_token": primary_token,
        "secondary_token": secondary_token,
        "operator_token": operator_token,
    }


@pytest.fixture
def client(control_plane_env):
    application = create_app(settings=control_plane_env["settings"])
    application.config["TESTING"] = True
    return application.test_client()


def _heartbeat(
    client,
    token: str,
    *,
    node_id: str,
    role: str,
    git_commit: str = "abc123def456",
    health: dict | None = None,
):
    return client.post(
        "/api/v1/heartbeat",
        headers=_headers(token),
        json={
            "node_id": node_id,
            "role": role,
            "boot_id": f"{node_id}-boot",
            "git_commit": git_commit,
            "health": health or {"ok": True},
        },
    )


def _acquire_lease(
    client,
    token: str,
    *,
    node_id: str,
    role: str,
    resource: str,
    ttl_seconds: int = 600,
):
    return client.post(
        "/api/v1/leases/acquire",
        headers=_headers(token),
        json={
            "node_id": node_id,
            "role": role,
            "resource": resource,
            "ttl_seconds": ttl_seconds,
        },
    )


def _release_lease(client, token: str, *, node_id: str, resource: str, lease_token: str):
    return client.post(
        "/api/v1/leases/release",
        headers=_headers(token),
        json={
            "node_id": node_id,
            "resource": resource,
            "lease_token": lease_token,
        },
    )


def _complete_broadcast(
    client,
    token: str,
    *,
    node_id: str,
    broadcast_id: str,
    resource: str,
    lease_token: str,
    fencing_token: int,
    status: str,
    youtube_id: str | None = None,
):
    payload = {
        "broadcast_id": broadcast_id,
        "resource": resource,
        "node_id": node_id,
        "lease_token": lease_token,
        "fencing_token": fencing_token,
        "status": status,
    }
    if youtube_id is not None:
        payload["youtube_id"] = youtube_id
    return client.post(
        "/api/v1/broadcasts/complete",
        headers=_headers(token),
        json=payload,
    )


def _reconcile_broadcast(
    client,
    token: str,
    *,
    resource: str,
    resolution: str,
    status: str | None = None,
    youtube_id: str | None = None,
):
    payload = {
        "resource": resource,
        "resolution": resolution,
    }
    if status is not None:
        payload["status"] = status
    if youtube_id is not None:
        payload["youtube_id"] = youtube_id
    return client.post(
        "/api/v1/broadcasts/reconcile",
        headers=_headers(token),
        json=payload,
    )


def test_automatic_prefers_healthy_primary(client, control_plane_env, monkeypatch):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: now)

    primary = _heartbeat(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
    )
    assert primary.status_code == 200
    assert primary.get_json()["eligible"] is True
    assert primary.get_json()["effective_owner_role"] == "primary"

    secondary = _heartbeat(
        client,
        control_plane_env["secondary_token"],
        node_id="btcedu-secondary",
        role="secondary",
    )
    assert secondary.status_code == 200
    assert secondary.get_json()["eligible"] is False
    assert secondary.get_json()["effective_owner_node"] == "btcedu-primary"

    status = client.get("/api/v1/status", headers=_headers(control_plane_env["operator_token"]))
    assert status.status_code == 200
    payload = status.get_json()
    assert payload["mode"] == "automatic"
    assert payload["effective_owner_role"] == "primary"
    assert payload["effective_owner_node"] == "btcedu-primary"
    assert len(payload["nodes"]) == 2


def test_secondary_becomes_eligible_after_primary_stale_grace(
    client, control_plane_env, monkeypatch
):
    current = {"value": datetime(2026, 8, 22, 12, 0, tzinfo=UTC)}
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: current["value"])

    _heartbeat(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
    )

    current["value"] = current["value"] + timedelta(seconds=181 + 420)
    secondary = _heartbeat(
        client,
        control_plane_env["secondary_token"],
        node_id="btcedu-secondary",
        role="secondary",
    )

    assert secondary.status_code == 200
    payload = secondary.get_json()
    assert payload["eligible"] is True
    assert payload["effective_owner_role"] == "secondary"
    assert payload["effective_owner_node"] == "btcedu-secondary"


def test_active_primary_lease_blocks_secondary_takeover(client, control_plane_env, monkeypatch):
    current = {"value": datetime(2026, 8, 22, 12, 0, tzinfo=UTC)}
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: current["value"])

    _heartbeat(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
    )
    acquire = client.post(
        "/api/v1/leases/acquire",
        headers=_headers(control_plane_env["primary_token"]),
        json={
            "node_id": "btcedu-primary",
            "role": "primary",
            "resource": "pipeline:tagesschau_tr/2000/2026-08-22",
            "ttl_seconds": 900,
        },
    )
    assert acquire.status_code == 200
    assert acquire.get_json()["acquired"] is True

    current["value"] = current["value"] + timedelta(seconds=700)
    secondary = _heartbeat(
        client,
        control_plane_env["secondary_token"],
        node_id="btcedu-secondary",
        role="secondary",
    )
    assert secondary.status_code == 200
    assert secondary.get_json()["eligible"] is False

    acquire_secondary = client.post(
        "/api/v1/leases/acquire",
        headers=_headers(control_plane_env["secondary_token"]),
        json={
            "node_id": "btcedu-secondary",
            "role": "secondary",
            "resource": "pipeline:tagesschau_tr/2000/2026-08-23",
            "ttl_seconds": 900,
        },
    )
    assert acquire_secondary.status_code == 403
    assert acquire_secondary.get_json()["acquired"] is False


def test_lease_conflict_and_fencing_tokens_are_monotonic(
    client, control_plane_env, monkeypatch
):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: now)
    _heartbeat(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
    )

    first = client.post(
        "/api/v1/leases/acquire",
        headers=_headers(control_plane_env["primary_token"]),
        json={
            "node_id": "btcedu-primary",
            "role": "primary",
            "resource": "pipeline:broadcast",
            "ttl_seconds": 600,
        },
    )
    assert first.status_code == 200
    first_lease = first.get_json()["lease"]
    assert first_lease["fencing_token"] == 1

    conflict = client.post(
        "/api/v1/leases/acquire",
        headers=_headers(control_plane_env["primary_token"]),
        json={
            "node_id": "btcedu-primary",
            "role": "primary",
            "resource": "pipeline:broadcast",
            "ttl_seconds": 600,
        },
    )
    assert conflict.status_code == 409
    assert conflict.get_json()["acquired"] is False
    assert "lease_token" not in conflict.get_json()["lease"]

    released = client.post(
        "/api/v1/leases/release",
        headers=_headers(control_plane_env["primary_token"]),
        json={
            "node_id": "btcedu-primary",
            "resource": "pipeline:broadcast",
            "lease_token": first_lease["lease_token"],
        },
    )
    assert released.status_code == 200
    assert released.get_json()["released"] is True

    second = client.post(
        "/api/v1/leases/acquire",
        headers=_headers(control_plane_env["primary_token"]),
        json={
            "node_id": "btcedu-primary",
            "role": "primary",
            "resource": "pipeline:broadcast",
            "ttl_seconds": 600,
        },
    )
    assert second.status_code == 200
    assert second.get_json()["lease"]["fencing_token"] == 2


def test_status_hides_lease_tokens_for_operator_and_node(client, control_plane_env, monkeypatch):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: now)
    _heartbeat(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
    )

    lease = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource="pipeline:tagesschau_tr/2000/2026-08-22",
    ).get_json()["lease"]
    assert lease["lease_token"]

    for token in (
        control_plane_env["operator_token"],
        control_plane_env["primary_token"],
    ):
        response = client.get("/api/v1/status", headers=_headers(token))
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["active_leases"] == [
            {
                "resource": "pipeline:tagesschau_tr/2000/2026-08-22",
                "node_id": "btcedu-primary",
                "role": "primary",
                "fencing_token": lease["fencing_token"],
                "expires_at": lease["expires_at"],
            }
        ]
        assert "lease_token" not in payload["active_leases"][0]


def test_broadcast_completion_is_idempotent_after_release(
    client, control_plane_env, monkeypatch
):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: now)
    _heartbeat(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
    )

    resource = "pipeline:tagesschau_tr/2000/2026-08-22"
    first = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=resource,
    ).get_json()["lease"]

    completed = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-22",
        resource=resource,
        lease_token=first["lease_token"],
        fencing_token=first["fencing_token"],
        status="completed",
    )
    assert completed.status_code == 200
    assert completed.get_json()["recorded"] is True
    assert completed.get_json()["resource"] == resource

    released = _release_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        resource=resource,
        lease_token=first["lease_token"],
    )
    assert released.status_code == 200
    assert released.get_json()["released"] is True

    completed_again = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-22",
        resource=resource,
        lease_token=first["lease_token"],
        fencing_token=first["fencing_token"],
        status="completed",
    )
    assert completed_again.status_code == 200
    assert completed_again.get_json() == completed.get_json()


def test_recorder_broadcast_resource_accepts_completion_and_blocks_reacquire(
    client, control_plane_env, monkeypatch
):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: now)
    _heartbeat(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
    )

    resource = "broadcast:tagesschau/2000/2026-08-22:recorder"
    broadcast_id = "tagesschau/2000/2026-08-22"
    first = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=resource,
    ).get_json()["lease"]

    completed = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id=broadcast_id,
        resource=resource,
        lease_token=first["lease_token"],
        fencing_token=first["fencing_token"],
        status="recorded",
    )
    assert completed.status_code == 200
    assert completed.get_json()["broadcast_id"] == broadcast_id

    released = _release_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        resource=resource,
        lease_token=first["lease_token"],
    )
    assert released.status_code == 200
    assert released.get_json()["released"] is True

    reacquire = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=resource,
    )
    assert reacquire.status_code == 409
    assert reacquire.get_json()["latest_status"] == "recorded"


def test_broadcast_completion_rejects_missing_or_mismatched_leases(
    client, control_plane_env, monkeypatch
):
    current = {"value": datetime(2026, 8, 22, 12, 0, tzinfo=UTC)}
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: current["value"])
    _heartbeat(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
    )
    _heartbeat(
        client,
        control_plane_env["secondary_token"],
        node_id="btcedu-secondary",
        role="secondary",
    )

    resource = "pipeline:tagesschau_tr/2000/2026-08-22"
    lease = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=resource,
        ttl_seconds=60,
    ).get_json()["lease"]

    accepted = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-22",
        resource=resource,
        lease_token=lease["lease_token"],
        fencing_token=lease["fencing_token"],
        status="completed",
    )
    assert accepted.status_code == 200

    released = _release_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        resource=resource,
        lease_token=lease["lease_token"],
    )
    assert released.status_code == 200

    wrong_node = _complete_broadcast(
        client,
        control_plane_env["secondary_token"],
        node_id="btcedu-secondary",
        broadcast_id="tagesschau_tr/2000/2026-08-22",
        resource=resource,
        lease_token=lease["lease_token"],
        fencing_token=lease["fencing_token"],
        status="completed",
    )
    assert wrong_node.status_code == 409
    assert "matching active lease required" in wrong_node.get_json()["error"]

    wrong_resource = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-22",
        resource="publish:tagesschau_tr/2000/2026-08-22",
        lease_token=lease["lease_token"],
        fencing_token=lease["fencing_token"],
        status="completed",
    )
    assert wrong_resource.status_code == 409
    assert "matching active lease required" in wrong_resource.get_json()["error"]

    wrong_token = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-22",
        resource=resource,
        lease_token="wrong-token",
        fencing_token=lease["fencing_token"],
        status="completed",
    )
    assert wrong_token.status_code == 409
    assert "matching active lease required" in wrong_token.get_json()["error"]

    other_broadcast = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-23",
        resource=resource,
        lease_token=lease["lease_token"],
        fencing_token=lease["fencing_token"],
        status="completed",
    )
    assert other_broadcast.status_code == 400
    assert "resource must match broadcast_id" in other_broadcast.get_json()["error"]

    fresh_resource = "pipeline:tagesschau_tr/2000/2026-08-24"
    fresh = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=fresh_resource,
        ttl_seconds=30,
    ).get_json()["lease"]
    current["value"] = current["value"] + timedelta(seconds=31)
    expired_unrecorded = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-24",
        resource=fresh_resource,
        lease_token=fresh["lease_token"],
        fencing_token=fresh["fencing_token"],
        status="completed",
    )
    assert expired_unrecorded.status_code == 409
    assert "matching active lease required" in expired_unrecorded.get_json()["error"]

    released_unrecorded = _release_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        resource=fresh_resource,
        lease_token=fresh["lease_token"],
    )
    assert released_unrecorded.status_code == 200


def test_completed_resources_block_reacquire_but_not_unrelated_resources(
    client, control_plane_env, monkeypatch
):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: now)
    _heartbeat(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
    )

    recorder_resource = "recorder:tagesschau_tr/2000/2026-08-22"
    recorder_lease = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=recorder_resource,
    ).get_json()["lease"]
    recorded = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-22",
        resource=recorder_resource,
        lease_token=recorder_lease["lease_token"],
        fencing_token=recorder_lease["fencing_token"],
        status="recorded",
    )
    assert recorded.status_code == 200
    _release_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        resource=recorder_resource,
        lease_token=recorder_lease["lease_token"],
    )

    pipeline_resource = "pipeline:tagesschau_tr/2000/2026-08-22"
    pipeline_acquire = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=pipeline_resource,
    )
    assert pipeline_acquire.status_code == 200
    pipeline_lease = pipeline_acquire.get_json()["lease"]
    completed = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-22",
        resource=pipeline_resource,
        lease_token=pipeline_lease["lease_token"],
        fencing_token=pipeline_lease["fencing_token"],
        status="approved",
    )
    assert completed.status_code == 200
    _release_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        resource=pipeline_resource,
        lease_token=pipeline_lease["lease_token"],
    )

    reacquire_pipeline = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=pipeline_resource,
    )
    assert reacquire_pipeline.status_code == 409
    assert reacquire_pipeline.get_json()["latest_status"] == "approved"

    publish_acquire = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource="publish:tagesschau_tr/2000/2026-08-22",
    )
    assert publish_acquire.status_code == 200


def test_processing_blocks_reacquire_after_lease_expiry(client, control_plane_env, monkeypatch):
    current = {"value": datetime(2026, 8, 22, 12, 0, tzinfo=UTC)}
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: current["value"])
    _heartbeat(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
    )

    resource = "pipeline:tagesschau_tr/2000/2026-08-25"
    lease = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=resource,
        ttl_seconds=30,
    ).get_json()["lease"]
    processing = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-25",
        resource=resource,
        lease_token=lease["lease_token"],
        fencing_token=lease["fencing_token"],
        status="processing",
    )
    assert processing.status_code == 200

    current["value"] = current["value"] + timedelta(seconds=31)
    blocked = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=resource,
        ttl_seconds=30,
    )

    assert blocked.status_code == 409
    payload = blocked.get_json()
    assert payload["latest_status"] == "processing"
    assert "processing" in payload["error"]


def test_processing_can_transition_to_terminal_status_with_same_lease(
    client, control_plane_env, monkeypatch
):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: now)
    _heartbeat(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
    )

    resource = "pipeline:tagesschau_tr/2000/2026-08-30"
    lease = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=resource,
    ).get_json()["lease"]
    processing = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-30",
        resource=resource,
        lease_token=lease["lease_token"],
        fencing_token=lease["fencing_token"],
        status="processing",
    )
    assert processing.status_code == 200

    completed = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-30",
        resource=resource,
        lease_token=lease["lease_token"],
        fencing_token=lease["fencing_token"],
        status="completed",
    )
    assert completed.status_code == 200
    assert completed.get_json()["status"] == "completed"


def test_node_token_cannot_reconcile_uncertain_broadcast(client, control_plane_env, monkeypatch):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: now)
    _heartbeat(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
    )

    resource = "pipeline:tagesschau_tr/2000/2026-08-26"
    lease = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=resource,
    ).get_json()["lease"]
    _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-26",
        resource=resource,
        lease_token=lease["lease_token"],
        fencing_token=lease["fencing_token"],
        status="processing",
    )

    rejected = _reconcile_broadcast(
        client,
        control_plane_env["primary_token"],
        resource=resource,
        resolution="retry",
    )

    assert rejected.status_code == 401
    assert "operator token" in rejected.get_json()["error"]


def test_operator_retry_permits_reacquire_after_processing(
    client, control_plane_env, monkeypatch
):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: now)
    _heartbeat(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
    )

    resource = "pipeline:tagesschau_tr/2000/2026-08-27"
    lease = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=resource,
    ).get_json()["lease"]
    _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-27",
        resource=resource,
        lease_token=lease["lease_token"],
        fencing_token=lease["fencing_token"],
        status="processing",
    )
    _release_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        resource=resource,
        lease_token=lease["lease_token"],
    )

    reconciled = _reconcile_broadcast(
        client,
        control_plane_env["operator_token"],
        resource=resource,
        resolution="retry",
    )

    assert reconciled.status_code == 200
    payload = reconciled.get_json()
    assert payload["status"] == "failed"
    assert payload["resolution"] == "retry"

    reacquired = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=resource,
    )
    assert reacquired.status_code == 200

    conn = sqlite3.connect(control_plane_env["settings"].database_path)
    try:
        audit = conn.execute(
            """
            SELECT event_type, actor_type, details_json
            FROM audit_events
            WHERE event_type = 'broadcast.reconciled'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    finally:
        conn.close()

    assert audit is not None
    assert audit[0] == "broadcast.reconciled"
    assert audit[1] == "operator"
    assert '"resolution": "retry"' in audit[2]


def test_operator_completed_blocks_reacquire(client, control_plane_env, monkeypatch):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: now)
    _heartbeat(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
    )

    resource = "pipeline:tagesschau_tr/2000/2026-08-28"
    lease = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=resource,
    ).get_json()["lease"]
    _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-28",
        resource=resource,
        lease_token=lease["lease_token"],
        fencing_token=lease["fencing_token"],
        status="processing",
    )
    _release_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        resource=resource,
        lease_token=lease["lease_token"],
    )

    completed = _reconcile_broadcast(
        client,
        control_plane_env["operator_token"],
        resource=resource,
        resolution="completed",
    )

    assert completed.status_code == 200
    assert completed.get_json()["status"] == "completed"

    blocked = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=resource,
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["latest_status"] == "completed"


def test_publish_states_block_or_allow_reacquire(client, control_plane_env, monkeypatch):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: now)
    _heartbeat(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
    )

    publishing_resource = "publish:tagesschau_tr/2000/2026-08-22"
    publishing_lease = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=publishing_resource,
    ).get_json()["lease"]
    publishing = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-22",
        resource=publishing_resource,
        lease_token=publishing_lease["lease_token"],
        fencing_token=publishing_lease["fencing_token"],
        status="publishing",
    )
    assert publishing.status_code == 200
    _release_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        resource=publishing_resource,
        lease_token=publishing_lease["lease_token"],
    )
    blocked = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=publishing_resource,
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["latest_status"] == "publishing"

    failed_resource = "publish:tagesschau_tr/2000/2026-08-23"
    failed_lease = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=failed_resource,
    ).get_json()["lease"]
    publishing_failed = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-23",
        resource=failed_resource,
        lease_token=failed_lease["lease_token"],
        fencing_token=failed_lease["fencing_token"],
        status="publishing",
    )
    assert publishing_failed.status_code == 200
    publish_failed = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-23",
        resource=failed_resource,
        lease_token=failed_lease["lease_token"],
        fencing_token=failed_lease["fencing_token"],
        status="publish_failed",
    )
    assert publish_failed.status_code == 200
    _release_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        resource=failed_resource,
        lease_token=failed_lease["lease_token"],
    )
    retry = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=failed_resource,
    )
    assert retry.status_code == 200

    published_resource = "publish:tagesschau_tr/2000/2026-08-24"
    published_lease = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=published_resource,
    ).get_json()["lease"]
    _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-24",
        resource=published_resource,
        lease_token=published_lease["lease_token"],
        fencing_token=published_lease["fencing_token"],
        status="publishing",
    )
    published = _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-24",
        resource=published_resource,
        lease_token=published_lease["lease_token"],
        fencing_token=published_lease["fencing_token"],
        status="published",
        youtube_id="yt123",
    )
    assert published.status_code == 200
    _release_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        resource=published_resource,
        lease_token=published_lease["lease_token"],
    )
    published_reacquire = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=published_resource,
    )
    assert published_reacquire.status_code == 409
    assert published_reacquire.get_json()["latest_status"] == "published"


def test_publishing_can_be_reconciled_to_published_with_youtube_id(
    client, control_plane_env, monkeypatch
):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: now)
    _heartbeat(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
    )

    resource = "publish:tagesschau_tr/2000/2026-08-29"
    lease = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=resource,
    ).get_json()["lease"]
    _complete_broadcast(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        broadcast_id="tagesschau_tr/2000/2026-08-29",
        resource=resource,
        lease_token=lease["lease_token"],
        fencing_token=lease["fencing_token"],
        status="publishing",
    )
    _release_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        resource=resource,
        lease_token=lease["lease_token"],
    )

    missing_youtube = _reconcile_broadcast(
        client,
        control_plane_env["operator_token"],
        resource=resource,
        resolution="completed",
        status="published",
    )
    assert missing_youtube.status_code == 400
    assert "youtube_id is required" in missing_youtube.get_json()["error"]

    published = _reconcile_broadcast(
        client,
        control_plane_env["operator_token"],
        resource=resource,
        resolution="completed",
        status="published",
        youtube_id="yt999",
    )

    assert published.status_code == 200
    payload = published.get_json()
    assert payload["status"] == "published"
    assert payload["youtube_id"] == "yt999"

    blocked = _acquire_lease(
        client,
        control_plane_env["primary_token"],
        node_id="btcedu-primary",
        role="primary",
        resource=resource,
    )
    assert blocked.status_code == 409
    assert blocked.get_json()["latest_status"] == "published"


def test_schema_upgrade_backfills_broadcast_record_resource(tmp_path: Path):
    database_path = tmp_path / "legacy-control-plane.db"
    conn = sqlite3.connect(database_path)
    conn.executescript(
        """
        CREATE TABLE control_state (
            singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
            mode TEXT NOT NULL,
            next_fencing_token INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            updated_by TEXT NOT NULL
        );
        INSERT INTO control_state (
            singleton, mode, next_fencing_token, updated_at, updated_by
        ) VALUES (1, 'automatic', 1, '2026-08-22T10:00:00+00:00', 'system');
        CREATE TABLE broadcast_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            broadcast_id TEXT NOT NULL,
            node_id TEXT NOT NULL,
            lease_token TEXT NOT NULL,
            fencing_token INTEGER NOT NULL,
            status TEXT NOT NULL,
            youtube_id TEXT,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(broadcast_id, lease_token, status)
        );
        INSERT INTO broadcast_records (
            broadcast_id, node_id, lease_token, fencing_token,
            status, youtube_id, created_at, updated_at
        ) VALUES (
            'tagesschau_tr/2000/2026-08-22',
            'btcedu-primary',
            'legacy-lease',
            7,
            'published',
            'yt123',
            '2026-08-22T10:00:00+00:00',
            '2026-08-22T10:00:00+00:00'
        );
        """
    )
    conn.commit()
    conn.close()

    token_dir = tmp_path / "tokens"
    node_tokens = {
        "btcedu-primary": {
            "role": "primary",
            "token_file": str(_write_file(token_dir / "primary.token", "primary-secret-token")),
        },
        "btcedu-secondary": {
            "role": "secondary",
            "token_file": str(_write_file(token_dir / "secondary.token", "secondary-secret-token")),
        },
    }
    settings = ControlPlaneSettings(
        database_path=str(database_path),
        node_tokens_file=str(_write_file(token_dir / "node_tokens.json", json.dumps(node_tokens))),
        operator_token_file=str(_write_file(token_dir / "operator.token", "operator-secret-token")),
    )

    create_app(settings=settings)

    conn = sqlite3.connect(database_path)
    column_names = {row[1] for row in conn.execute("PRAGMA table_info(broadcast_records)")}
    assert "resource" in column_names
    resource = conn.execute("SELECT resource FROM broadcast_records").fetchone()[0]
    assert resource == "publish:tagesschau_tr/2000/2026-08-22"
    conn.close()


def test_operator_can_force_secondary_mode(client, control_plane_env, monkeypatch):
    now = datetime(2026, 8, 22, 12, 0, tzinfo=UTC)
    monkeypatch.setattr("btcedu.failover.control_plane.utcnow", lambda: now)
    _heartbeat(
        client,
        control_plane_env["secondary_token"],
        node_id="btcedu-secondary",
        role="secondary",
    )

    response = client.put(
        "/api/v1/mode",
        headers=_headers(control_plane_env["operator_token"]),
        json={"mode": "force_secondary"},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["mode"] == "force_secondary"
    assert payload["effective_owner_role"] == "secondary"
