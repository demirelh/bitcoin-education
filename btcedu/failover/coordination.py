from __future__ import annotations

import logging
import re
import threading
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.failover.types import LeaseInfo, NodeRole
from btcedu.models.episode import Episode
from btcedu.services.failover_service import (
    FailoverControlPlaneClient,
    FailoverExecutionRejected,
    FailoverLeaseLostError,
    build_node_health,
    failover_boot_id,
    failover_enabled,
)
from btcedu.version import get_git_commit

logger = logging.getLogger(__name__)

_TITLE_DATE_RE = re.compile(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})")
_TITLE_EDITION_RE = re.compile(r"(\d{1,2})[:.](\d{2})\s*Uhr", re.IGNORECASE)
_SLUG_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
_SLUG_EDITION_RE = re.compile(r"_(\d{4})(?:\D|$)")


def canonical_broadcast_id(episode: Episode) -> str:
    profile = str(getattr(episode, "content_profile", "") or "default").strip()
    day = _broadcast_day(episode)
    edition = _broadcast_edition(episode)
    return f"{profile}/{edition}/{day.isoformat()}"


def pipeline_lease_resource(episode: Episode) -> str:
    return f"pipeline:{canonical_broadcast_id(episode)}"


def publish_lease_resource(episode: Episode) -> str:
    return f"publish:{canonical_broadcast_id(episode)}"


def broadcast_id_for_episode(episode: Episode) -> str:
    return canonical_broadcast_id(episode)


def _broadcast_day(episode: Episode) -> date:
    slug_day = _day_from_slug(episode.episode_id)
    if slug_day is not None:
        return slug_day
    title_day = _day_from_title(episode.title or "")
    if title_day is not None:
        return title_day
    if episode.published_at is None:
        raise ValueError(f"Episode {episode.episode_id} has no broadcast day")
    value = episode.published_at
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).date()


def _broadcast_edition(episode: Episode) -> str:
    slug_edition = _edition_from_slug(episode.episode_id)
    if slug_edition:
        return slug_edition
    title_edition = _edition_from_title(episode.title or "")
    if title_edition:
        return title_edition
    if episode.published_at is None:
        return "0000"
    return episode.published_at.strftime("%H%M")


def _day_from_title(title: str) -> date | None:
    match = _TITLE_DATE_RE.search(title or "")
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _day_from_slug(slug: str) -> date | None:
    match = _SLUG_DATE_RE.search(slug or "")
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _edition_from_title(title: str) -> str | None:
    match = _TITLE_EDITION_RE.search(title or "")
    if not match:
        return None
    return f"{int(match.group(1)):02d}{int(match.group(2)):02d}"


def _edition_from_slug(slug: str) -> str | None:
    match = _SLUG_EDITION_RE.search(slug or "")
    if not match:
        return None
    return match.group(1)


@dataclass
class LeaseGuard:
    client: FailoverControlPlaneClient | None
    node_id: str | None
    broadcast_id: str | None
    resource: str | None
    lease: LeaseInfo | None
    ttl_seconds: int = 0
    renew_interval_seconds: int = 0
    _stop_event: threading.Event | None = None
    _thread: threading.Thread | None = None
    _lost_error: Exception | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def enabled(self) -> bool:
        return self.client is not None and self.lease is not None and self.resource is not None

    @property
    def fencing_token(self) -> int | None:
        return self.lease.fencing_token if self.lease else None

    @property
    def lease_token(self) -> str | None:
        return self.lease.lease_token if self.lease else None

    def start(self) -> None:
        if not self.enabled or self.renew_interval_seconds <= 0:
            return
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._renew_loop,
            name=f"failover-lease-{self.resource}",
            daemon=True,
        )
        self._thread.start()

    def ensure_active(self) -> None:
        if not self.enabled:
            return
        if self._lost_error is not None:
            raise FailoverLeaseLostError(str(self._lost_error)) from self._lost_error
        if self.lease and datetime.now(UTC) >= self.lease.expires_at:
            raise FailoverLeaseLostError(
                f"Failover lease for {self.resource} expired at {self.lease.expires_at.isoformat()}"
            )

    def report_completion(self, *, status: str, youtube_id: str | None = None) -> None:
        if (
            not self.enabled
            or not self.broadcast_id
            or not self.lease_token
            or self.fencing_token is None
        ):
            return
        self.client.complete_broadcast(
            broadcast_id=self.broadcast_id,
            resource=self.resource or "",
            node_id=self.node_id or "",
            lease_token=self.lease_token,
            fencing_token=self.fencing_token,
            status=status,
            youtube_id=youtube_id,
        )

    def close(self) -> None:
        if not self.enabled:
            return
        if self._stop_event is not None:
            self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        try:
            self.client.release_lease(
                resource=self.resource or "",
                lease_token=self.lease_token or "",
                node_id=self.node_id or "",
            )
        except Exception:
            logger.warning("Could not release failover lease %s", self.resource, exc_info=True)

    def _renew_loop(self) -> None:
        if (
            not self.enabled
            or self._stop_event is None
            or self.client is None
            or self.lease is None
        ):
            return
        while not self._stop_event.wait(self.renew_interval_seconds):
            try:
                renewed = self.client.renew_lease(
                    resource=self.resource or "",
                    lease_token=self.lease.lease_token,
                    ttl_seconds=self.ttl_seconds,
                    node_id=self.node_id or "",
                )
                if not renewed.acquired or renewed.lease is None:
                    raise FailoverLeaseLostError(
                        renewed.error or f"Control plane rejected lease renewal for {self.resource}"
                    )
                with self._lock:
                    self.lease = replace(self.lease, expires_at=renewed.lease.expires_at)
            except Exception as exc:  # noqa: BLE001
                self._lost_error = exc
                self._stop_event.set()
                logger.error("Failover lease renewal failed for %s: %s", self.resource, exc)
                return


def noop_lease_guard() -> LeaseGuard:
    return LeaseGuard(client=None, node_id=None, broadcast_id=None, resource=None, lease=None)


def acquire_pipeline_lease_guard(
    session: Session,
    episode: Episode,
    settings: Settings,
) -> LeaseGuard:
    if not failover_enabled(settings):
        return noop_lease_guard()

    client = FailoverControlPlaneClient.from_settings(settings)
    heartbeat = client.heartbeat(
        boot_id=failover_boot_id(),
        git_commit=get_git_commit(),
        health=build_node_health(settings, session),
    )
    if not heartbeat.eligible:
        owner = _owner_label(heartbeat.effective_owner_role, heartbeat.effective_owner_node)
        raise FailoverExecutionRejected(
            f"Node is not eligible for new pipeline work in mode {heartbeat.mode.value}"
            f"{f' (owner {owner})' if owner else ''}"
        )

    resource = pipeline_lease_resource(episode)
    acquisition = client.acquire_lease(
        resource=resource,
        ttl_seconds=int(getattr(settings, "failover_pipeline_lease_ttl_seconds", 540)),
    )
    if not acquisition.acquired or acquisition.lease is None:
        owner = acquisition.lease.node_id if acquisition.lease else heartbeat.effective_owner_node
        raise FailoverExecutionRejected(
            acquisition.error
            or f"Pipeline lease for {resource} is already held by {owner or 'another node'}"
        )

    guard = LeaseGuard(
        client=client,
        node_id=str(getattr(settings, "failover_node_id", "") or ""),
        broadcast_id=broadcast_id_for_episode(episode),
        resource=resource,
        lease=acquisition.lease,
        ttl_seconds=int(getattr(settings, "failover_pipeline_lease_ttl_seconds", 540)),
        renew_interval_seconds=int(
            getattr(settings, "failover_pipeline_lease_renew_interval_seconds", 120)
        ),
    )
    try:
        guard.report_completion(status="processing")
    except Exception:
        guard.close()
        raise
    guard.start()
    return guard


def acquire_publish_lease_guard(
    session: Session,
    episode: Episode,
    settings: Settings,
) -> LeaseGuard:
    if not failover_enabled(settings):
        return noop_lease_guard()

    client = FailoverControlPlaneClient.from_settings(settings)
    heartbeat = client.heartbeat(
        boot_id=failover_boot_id(),
        git_commit=get_git_commit(),
        health=build_node_health(settings, session),
    )
    if not heartbeat.eligible:
        owner = _owner_label(heartbeat.effective_owner_role, heartbeat.effective_owner_node)
        raise FailoverExecutionRejected(
            f"Node is not eligible to publish in mode {heartbeat.mode.value}"
            f"{f' (owner {owner})' if owner else ''}"
        )

    resource = publish_lease_resource(episode)
    acquisition = client.acquire_lease(
        resource=resource,
        ttl_seconds=int(getattr(settings, "failover_publish_lease_ttl_seconds", 900)),
    )
    if not acquisition.acquired or acquisition.lease is None:
        owner = acquisition.lease.node_id if acquisition.lease else heartbeat.effective_owner_node
        raise FailoverExecutionRejected(
            acquisition.error or f"Publish lease for {resource} is already held by {owner}"
        )

    guard = LeaseGuard(
        client=client,
        node_id=str(getattr(settings, "failover_node_id", "") or ""),
        broadcast_id=broadcast_id_for_episode(episode),
        resource=resource,
        lease=acquisition.lease,
        ttl_seconds=int(getattr(settings, "failover_publish_lease_ttl_seconds", 900)),
        renew_interval_seconds=int(
            getattr(settings, "failover_pipeline_lease_renew_interval_seconds", 120)
        ),
    )
    guard.start()
    return guard


def _owner_label(role: NodeRole | None, node_id: str | None) -> str:
    if role is None and not node_id:
        return ""
    if role is None:
        return str(node_id or "")
    if not node_id:
        return role.value
    return f"{role.value}/{node_id}"
