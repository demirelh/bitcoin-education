from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any


class NodeRole(str, Enum):
    PRIMARY = "primary"
    SECONDARY = "secondary"


class FailoverMode(str, Enum):
    AUTOMATIC = "automatic"
    FORCE_PRIMARY = "force_primary"
    FORCE_SECONDARY = "force_secondary"
    PAUSED = "paused"


def utcnow() -> datetime:
    return datetime.now(UTC)


def parse_utc_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


@dataclass(frozen=True)
class LeaseInfo:
    fencing_token: int
    expires_at: datetime
    resource: str
    lease_token: str | None = None
    node_id: str | None = None
    role: NodeRole | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any] | None) -> LeaseInfo | None:
        if not payload:
            return None
        role = payload.get("role")
        return cls(
            fencing_token=int(payload["fencing_token"]),
            expires_at=parse_utc_timestamp(str(payload["expires_at"])) or utcnow(),
            resource=str(payload["resource"]),
            lease_token=(
                str(payload["lease_token"]).strip() if payload.get("lease_token") else None
            ),
            node_id=str(payload["node_id"]) if payload.get("node_id") else None,
            role=NodeRole(str(role)) if role else None,
        )

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "fencing_token": self.fencing_token,
            "expires_at": self.expires_at.isoformat(),
            "resource": self.resource,
            "node_id": self.node_id,
            "role": self.role.value if self.role else None,
        }
        if self.lease_token:
            payload["lease_token"] = self.lease_token
        return payload

    def to_public_dict(self) -> dict[str, Any]:
        return {
            "fencing_token": self.fencing_token,
            "expires_at": self.expires_at.isoformat(),
            "resource": self.resource,
            "node_id": self.node_id,
            "role": self.role.value if self.role else None,
        }


@dataclass(frozen=True)
class NodeStatusSnapshot:
    node_id: str
    role: NodeRole
    boot_id: str
    git_commit: str
    healthy: bool
    eligible: bool
    last_heartbeat_at: datetime | None
    healthy_since: datetime | None
    unhealthy_since: datetime | None
    health: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> NodeStatusSnapshot:
        return cls(
            node_id=str(payload["node_id"]),
            role=NodeRole(str(payload["role"])),
            boot_id=str(payload.get("boot_id") or ""),
            git_commit=str(payload.get("git_commit") or ""),
            healthy=bool(payload.get("healthy", False)),
            eligible=bool(payload.get("eligible", False)),
            last_heartbeat_at=parse_utc_timestamp(payload.get("last_heartbeat_at")),
            healthy_since=parse_utc_timestamp(payload.get("healthy_since")),
            unhealthy_since=parse_utc_timestamp(payload.get("unhealthy_since")),
            health=dict(payload.get("health") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "node_id": self.node_id,
            "role": self.role.value,
            "boot_id": self.boot_id,
            "git_commit": self.git_commit,
            "healthy": self.healthy,
            "eligible": self.eligible,
            "last_heartbeat_at": (
                self.last_heartbeat_at.isoformat() if self.last_heartbeat_at else None
            ),
            "healthy_since": self.healthy_since.isoformat() if self.healthy_since else None,
            "unhealthy_since": self.unhealthy_since.isoformat() if self.unhealthy_since else None,
            "health": self.health,
        }


@dataclass(frozen=True)
class FailoverStatusSnapshot:
    mode: FailoverMode
    effective_owner_role: NodeRole | None
    effective_owner_node: str | None
    nodes: list[NodeStatusSnapshot]
    active_leases: list[LeaseInfo]

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> FailoverStatusSnapshot:
        owner_role = payload.get("effective_owner_role")
        return cls(
            mode=FailoverMode(str(payload["mode"])),
            effective_owner_role=NodeRole(str(owner_role)) if owner_role else None,
            effective_owner_node=(
                str(payload["effective_owner_node"])
                if payload.get("effective_owner_node")
                else None
            ),
            nodes=[NodeStatusSnapshot.from_dict(item) for item in payload.get("nodes", [])],
            active_leases=[
                lease
                for item in payload.get("active_leases", [])
                if (lease := LeaseInfo.from_dict(item))
            ],
        )


@dataclass(frozen=True)
class HeartbeatResponse:
    mode: FailoverMode
    effective_owner_role: NodeRole | None
    effective_owner_node: str | None
    eligible: bool

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> HeartbeatResponse:
        owner_role = payload.get("effective_owner_role")
        return cls(
            mode=FailoverMode(str(payload["mode"])),
            effective_owner_role=NodeRole(str(owner_role)) if owner_role else None,
            effective_owner_node=(
                str(payload["effective_owner_node"])
                if payload.get("effective_owner_node")
                else None
            ),
            eligible=bool(payload.get("eligible", False)),
        )


@dataclass(frozen=True)
class LeaseAcquisition:
    acquired: bool
    lease: LeaseInfo | None = None
    error: str | None = None

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> LeaseAcquisition:
        return cls(
            acquired=bool(payload.get("acquired", False) or payload.get("renewed", False)),
            lease=LeaseInfo.from_dict(payload.get("lease")),
            error=str(payload["error"]) if payload.get("error") else None,
        )
