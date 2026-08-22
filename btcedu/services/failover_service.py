from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from sqlalchemy import text
from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.failover.types import (
    FailoverMode,
    FailoverStatusSnapshot,
    HeartbeatResponse,
    LeaseAcquisition,
    LeaseInfo,
    NodeRole,
)
from btcedu.version import get_git_commit

logger = logging.getLogger(__name__)


class FailoverError(RuntimeError):
    """Base error for failover coordination problems."""


class FailoverConfigurationError(FailoverError):
    """Raised when failover is enabled but not configured correctly."""


class FailoverRequestError(FailoverError):
    """Raised when the control plane cannot be reached or returns an error."""


class FailoverExecutionRejected(FailoverError):
    """Raised when the current node is not allowed to start new work."""


class FailoverLeaseLostError(FailoverError):
    """Raised when a held lease can no longer be renewed safely."""


@dataclass(frozen=True)
class FailoverClientConfig:
    enabled: bool
    node_id: str
    node_role: NodeRole
    control_plane_url: str
    token: str
    request_timeout_seconds: int
    heartbeat_interval_seconds: int
    pipeline_lease_ttl_seconds: int
    pipeline_lease_renew_interval_seconds: int
    publish_lease_ttl_seconds: int


@dataclass(frozen=True)
class FailoverOperatorConfig:
    control_plane_url: str
    token: str
    request_timeout_seconds: int


def failover_enabled(settings: Settings) -> bool:
    return bool(getattr(settings, "failover_enabled", False))


def _normalize_url(url: str) -> str:
    normalized = str(url or "").strip().rstrip("/")
    if not normalized:
        raise FailoverConfigurationError("FAILOVER_CONTROL_PLANE_URL is required")
    return normalized


def _read_secret(value: str, file_path: str, label: str) -> str:
    direct = str(value or "").strip()
    if direct:
        return direct

    path_value = str(file_path or "").strip()
    if not path_value:
        raise FailoverConfigurationError(f"{label} is required")

    path = Path(path_value)
    try:
        secret = path.read_text(encoding="utf-8").strip()
    except OSError as exc:  # pragma: no cover - exercised via caller
        raise FailoverConfigurationError(f"Could not read {label} from {path}: {exc}") from exc
    if not secret:
        raise FailoverConfigurationError(f"{label} file {path} is empty")
    return secret


def _node_role(settings: Settings) -> NodeRole:
    role = str(getattr(settings, "failover_node_role", "") or "").strip().lower()
    if not role:
        raise FailoverConfigurationError("FAILOVER_NODE_ROLE is required")
    try:
        return NodeRole(role)
    except ValueError as exc:
        raise FailoverConfigurationError(
            "FAILOVER_NODE_ROLE must be 'primary' or 'secondary'"
        ) from exc


def load_failover_client_config(settings: Settings) -> FailoverClientConfig | None:
    if not failover_enabled(settings):
        return None

    node_id = str(getattr(settings, "failover_node_id", "") or "").strip()
    if not node_id:
        raise FailoverConfigurationError("FAILOVER_NODE_ID is required when failover is enabled")

    return FailoverClientConfig(
        enabled=True,
        node_id=node_id,
        node_role=_node_role(settings),
        control_plane_url=_normalize_url(getattr(settings, "failover_control_plane_url", "")),
        token=_read_secret(
            getattr(settings, "failover_token", ""),
            getattr(settings, "failover_token_file", ""),
            "FAILOVER_TOKEN or FAILOVER_TOKEN_FILE",
        ),
        request_timeout_seconds=max(
            1, int(getattr(settings, "failover_request_timeout_seconds", 5))
        ),
        heartbeat_interval_seconds=max(
            5, int(getattr(settings, "failover_heartbeat_interval_seconds", 60))
        ),
        pipeline_lease_ttl_seconds=max(
            30, int(getattr(settings, "failover_pipeline_lease_ttl_seconds", 540))
        ),
        pipeline_lease_renew_interval_seconds=max(
            5, int(getattr(settings, "failover_pipeline_lease_renew_interval_seconds", 120))
        ),
        publish_lease_ttl_seconds=max(
            30, int(getattr(settings, "failover_publish_lease_ttl_seconds", 900))
        ),
    )


def load_failover_operator_config(settings: Settings) -> FailoverOperatorConfig | None:
    if not failover_enabled(settings):
        return None

    token = getattr(settings, "failover_operator_token", "") or getattr(
        settings, "failover_operator_token_file", ""
    )
    if not str(token).strip():
        return None

    return FailoverOperatorConfig(
        control_plane_url=_normalize_url(getattr(settings, "failover_control_plane_url", "")),
        token=_read_secret(
            getattr(settings, "failover_operator_token", ""),
            getattr(settings, "failover_operator_token_file", ""),
            "FAILOVER_OPERATOR_TOKEN or FAILOVER_OPERATOR_TOKEN_FILE",
        ),
        request_timeout_seconds=max(
            1, int(getattr(settings, "failover_request_timeout_seconds", 5))
        ),
    )


def failover_feed_detection_enabled(settings: Settings) -> bool:
    if not failover_enabled(settings):
        return True
    try:
        return _node_role(settings) != NodeRole.SECONDARY
    except FailoverConfigurationError:
        return True


def failover_boot_id() -> str:
    candidate = Path("/proc/sys/kernel/random/boot_id")
    try:
        value = candidate.read_text(encoding="utf-8").strip()
    except OSError:
        return "unknown"
    return value or "unknown"


def build_node_health(settings: Settings, session: Session | None = None) -> dict[str, Any]:
    database_ok = True
    if session is not None:
        try:
            session.execute(text("SELECT 1"))
        except Exception:
            database_ok = False

    local_recorder_available = False
    try:
        from btcedu.profiles import get_registry

        profile = get_registry(settings).get(getattr(settings, "default_content_profile", ""))
        config = profile.local_recorder_config()
        base_dir = config.get("base_dir")
        local_recorder_available = bool(base_dir) and Path(str(base_dir)).exists()
    except Exception:
        local_recorder_available = False

    health = {
        "ok": database_ok,
        "database_ok": database_ok,
        "feed_detection_enabled": failover_feed_detection_enabled(settings),
        "hostname": socket.gethostname(),
        "local_recorder_available": local_recorder_available,
        "git_commit": get_git_commit(),
    }
    return health


class FailoverControlPlaneClient:
    """Small typed client for the external failover control plane."""

    def __init__(
        self,
        *,
        control_plane_url: str,
        token: str,
        timeout_seconds: int,
        node_id: str | None = None,
        node_role: NodeRole | None = None,
    ) -> None:
        self._base_url = _normalize_url(control_plane_url)
        self._token = str(token or "").strip()
        self._timeout = max(1, int(timeout_seconds))
        self._node_id = node_id
        self._node_role = node_role
        if not self._token:
            raise FailoverConfigurationError("Failover bearer token is missing")

    @classmethod
    def from_settings(cls, settings: Settings) -> FailoverControlPlaneClient:
        config = load_failover_client_config(settings)
        if config is None:
            raise FailoverConfigurationError("Failover is disabled")
        return cls(
            control_plane_url=config.control_plane_url,
            token=config.token,
            timeout_seconds=config.request_timeout_seconds,
            node_id=config.node_id,
            node_role=config.node_role,
        )

    @classmethod
    def operator_from_settings(cls, settings: Settings) -> FailoverControlPlaneClient:
        config = load_failover_operator_config(settings)
        if config is None:
            raise FailoverConfigurationError(
                "FAILOVER_OPERATOR_TOKEN or FAILOVER_OPERATOR_TOKEN_FILE is required"
            )
        return cls(
            control_plane_url=config.control_plane_url,
            token=config.token,
            timeout_seconds=config.request_timeout_seconds,
        )

    def status(self) -> FailoverStatusSnapshot:
        payload = self._request("GET", "/api/v1/status")
        return FailoverStatusSnapshot.from_dict(payload)

    def heartbeat(
        self,
        *,
        node_id: str | None = None,
        role: NodeRole | None = None,
        boot_id: str,
        git_commit: str,
        health: dict[str, Any],
    ) -> HeartbeatResponse:
        resolved_node_id = node_id or self._node_id
        resolved_role = role or self._node_role
        if not resolved_node_id or resolved_role is None:
            raise FailoverConfigurationError("Node id and role are required for heartbeat")
        payload = self._request(
            "POST",
            "/api/v1/heartbeat",
            {
                "node_id": resolved_node_id,
                "role": resolved_role.value,
                "boot_id": boot_id,
                "git_commit": git_commit,
                "health": health,
            },
        )
        return HeartbeatResponse.from_dict(payload)

    def acquire_lease(
        self,
        *,
        resource: str,
        ttl_seconds: int,
        node_id: str | None = None,
        role: NodeRole | None = None,
    ) -> LeaseAcquisition:
        resolved_node_id = node_id or self._node_id
        resolved_role = role or self._node_role
        if not resolved_node_id or resolved_role is None:
            raise FailoverConfigurationError("Node id and role are required for lease acquisition")
        payload, status_code = self._request_raw(
            "POST",
            "/api/v1/leases/acquire",
            {
                "node_id": resolved_node_id,
                "role": resolved_role.value,
                "resource": resource,
                "ttl_seconds": int(ttl_seconds),
            },
            tolerated_status_codes={403, 409},
        )
        if status_code in {403, 409}:
            message = str(payload.get("error") or "lease acquisition rejected")
            return LeaseAcquisition(
                acquired=False,
                lease=LeaseInfo.from_dict(payload.get("lease")),
                error=message,
            )
        return LeaseAcquisition.from_dict(payload)

    def renew_lease(
        self,
        *,
        resource: str,
        lease_token: str,
        ttl_seconds: int,
        node_id: str | None = None,
    ) -> LeaseAcquisition:
        resolved_node_id = node_id or self._node_id
        if not resolved_node_id:
            raise FailoverConfigurationError("Node id is required for lease renewal")
        payload, status_code = self._request_raw(
            "POST",
            "/api/v1/leases/renew",
            {
                "node_id": resolved_node_id,
                "resource": resource,
                "lease_token": lease_token,
                "ttl_seconds": int(ttl_seconds),
            },
            tolerated_status_codes={403, 409},
        )
        if status_code in {403, 409}:
            message = str(payload.get("error") or "lease renewal rejected")
            return LeaseAcquisition(
                acquired=False,
                lease=LeaseInfo.from_dict(payload.get("lease")),
                error=message,
            )
        return LeaseAcquisition(
            acquired=bool(payload.get("renewed", False)),
            lease=LeaseInfo.from_dict(payload.get("lease")),
            error=str(payload["error"]) if payload.get("error") else None,
        )

    def release_lease(
        self, *, resource: str, lease_token: str, node_id: str | None = None
    ) -> bool:
        resolved_node_id = node_id or self._node_id
        if not resolved_node_id:
            raise FailoverConfigurationError("Node id is required for lease release")
        payload, status_code = self._request_raw(
            "POST",
            "/api/v1/leases/release",
            {
                "node_id": resolved_node_id,
                "resource": resource,
                "lease_token": lease_token,
            },
            tolerated_status_codes={404, 409},
        )
        if status_code in {404, 409}:
            return False
        return bool(payload.get("released", False))

    def complete_broadcast(
        self,
        *,
        broadcast_id: str,
        resource: str,
        node_id: str,
        lease_token: str,
        fencing_token: int,
        status: str,
        youtube_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "broadcast_id": broadcast_id,
            "resource": resource,
            "node_id": node_id,
            "lease_token": lease_token,
            "fencing_token": int(fencing_token),
            "status": status,
        }
        if youtube_id:
            payload["youtube_id"] = youtube_id
        return self._request("POST", "/api/v1/broadcasts/complete", payload)

    def reconcile_broadcast(
        self,
        *,
        resource: str,
        resolution: str,
        status: str | None = None,
        youtube_id: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "resource": resource,
            "resolution": str(resolution or "").strip().lower(),
        }
        if status:
            payload["status"] = str(status).strip().lower()
        if youtube_id:
            payload["youtube_id"] = youtube_id
        return self._request("POST", "/api/v1/broadcasts/reconcile", payload)

    def set_mode(self, mode: FailoverMode | str) -> FailoverStatusSnapshot:
        resolved_mode = mode.value if isinstance(mode, FailoverMode) else FailoverMode(mode).value
        payload = self._request("PUT", "/api/v1/mode", {"mode": resolved_mode})
        return FailoverStatusSnapshot.from_dict(payload)

    def _request(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        data, _status = self._request_raw(method, path, payload)
        return data

    def _request_raw(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        tolerated_status_codes: set[int] | None = None,
    ) -> tuple[dict[str, Any], int]:
        url = f"{self._base_url}{path}"
        try:
            response = requests.request(
                method,
                url,
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=(self._timeout, self._timeout),
            )
        except requests.Timeout as exc:
            raise FailoverRequestError(f"Control plane request timed out: {method} {url}") from exc
        except requests.RequestException as exc:
            raise FailoverRequestError(
                f"Control plane request failed: {method} {url}: {exc}"
            ) from exc

        try:
            data = response.json()
        except ValueError:
            data = {}

        if tolerated_status_codes and response.status_code in tolerated_status_codes:
            return data, response.status_code
        if not response.ok:
            error = str(data.get("error") or f"HTTP {response.status_code} from {url}")
            raise FailoverRequestError(error)
        return data, response.status_code


def proxy_failover_status(settings: Settings) -> dict[str, Any]:
    if not failover_enabled(settings):
        return {"enabled": False}

    operator_configured = load_failover_operator_config(settings) is not None
    try:
        if operator_configured:
            client = FailoverControlPlaneClient.operator_from_settings(settings)
        else:
            client = FailoverControlPlaneClient.from_settings(settings)
        status = client.status()
    except FailoverError as exc:
        raise FailoverRequestError(str(exc)) from exc

    return {
        "enabled": True,
        "operator_configured": operator_configured,
        "mode": status.mode.value,
        "effective_owner_role": (
            status.effective_owner_role.value if status.effective_owner_role else None
        ),
        "effective_owner_node": status.effective_owner_node,
        "nodes": [node.to_dict() for node in status.nodes],
        "active_leases": [lease.to_public_dict() for lease in status.active_leases],
    }
