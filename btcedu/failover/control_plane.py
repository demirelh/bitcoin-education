from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from flask import Flask, current_app, g, jsonify, request
from pydantic import model_validator
from pydantic_settings import BaseSettings

from btcedu.failover.types import FailoverMode, NodeRole, utcnow
from btcedu.utils.secrets import install_log_redaction, is_secret_field

logger = logging.getLogger(__name__)


def _isoformat(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _parse_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class ControlPlaneSettings(BaseSettings):
    database_path: str = "data/control-plane/control_plane.db"
    operator_token: str = ""
    operator_token_file: str = ""
    node_tokens_file: str = ""
    heartbeat_stale_seconds: int = 180
    takeover_grace_seconds: int = 420
    stable_primary_recovery_seconds: int = 300

    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "env_prefix": "FAILOVER_CONTROL_PLANE_",
        "extra": "ignore",
    }

    @model_validator(mode="after")
    def _validate_settings(self) -> ControlPlaneSettings:
        if not self.node_tokens_file:
            raise ValueError("FAILOVER_CONTROL_PLANE_NODE_TOKENS_FILE is required")
        if not self.operator_token and not self.operator_token_file:
            raise ValueError(
                "FAILOVER_CONTROL_PLANE_OPERATOR_TOKEN or "
                "FAILOVER_CONTROL_PLANE_OPERATOR_TOKEN_FILE is required"
            )
        return self

    def __repr_args__(self):
        for name, value in super().__repr_args__():
            if name and is_secret_field(str(name)) and value:
                yield name, "[REDACTED]"
            else:
                yield name, value


@dataclass(frozen=True)
class ConfiguredNode:
    node_id: str
    role: NodeRole
    token_hash: str


def get_control_plane_settings() -> ControlPlaneSettings:
    return ControlPlaneSettings()


def create_app(settings: ControlPlaneSettings | None = None) -> Flask:
    app = Flask(__name__)
    resolved_settings = settings or get_control_plane_settings()
    install_log_redaction(resolved_settings)
    node_credentials = _load_configured_nodes(resolved_settings)
    operator_token_hash = _hash_token(
        _read_secret(
            resolved_settings.operator_token,
            resolved_settings.operator_token_file,
            "FAILOVER_CONTROL_PLANE_OPERATOR_TOKEN",
        )
    )
    _init_db(resolved_settings.database_path)

    app.config["settings"] = resolved_settings
    app.config["configured_nodes"] = {node.node_id: node for node in node_credentials}
    app.config["configured_hashes"] = {node.token_hash: node for node in node_credentials}
    app.config["operator_token_hash"] = operator_token_hash

    @app.teardown_appcontext
    def _close_db(_error: Exception | None) -> None:
        connection = g.pop("control_plane_db", None)
        if connection is not None:
            connection.close()

    @app.route("/health")
    def health():
        return jsonify({"status": "ok"})

    @app.route("/ready")
    def ready():
        try:
            conn = _db()
            conn.execute("SELECT mode, next_fencing_token FROM control_state WHERE singleton = 1")
        except Exception as exc:  # pragma: no cover - defensive
            return jsonify({"status": "not_ready", "error": str(exc)}), 503
        return jsonify({"status": "ready"})

    @app.route("/api/v1/status")
    def status():
        _authenticate_status_request()
        return jsonify(_status_payload(_db(), _settings()))

    @app.route("/api/v1/heartbeat", methods=["POST"])
    def heartbeat():
        configured = _authenticate_node_request()
        payload = _json_body()
        node_id = _require_string(payload, "node_id")
        role = _require_role(payload, "role")
        if configured.node_id != node_id or configured.role != role:
            return _error(403, "node token does not match node_id/role")

        boot_id = _require_string(payload, "boot_id")
        git_commit = _require_string(payload, "git_commit")
        health = payload.get("health") or {}
        if not isinstance(health, dict):
            return _error(400, "health must be an object")

        conn = _db()
        now = utcnow()
        previous = conn.execute("SELECT * FROM nodes WHERE node_id = ?", (node_id,)).fetchone()
        previous_snapshot = (
            _node_snapshot_from_row(previous, _settings(), now) if previous else None
        )
        health_ok = bool(health.get("ok", True))

        healthy_since = None
        unhealthy_since = None
        if health_ok:
            if (
                previous_snapshot
                and previous_snapshot["healthy"]
                and previous_snapshot["boot_id"] == boot_id
            ):
                healthy_since = previous_snapshot["healthy_since"] or now
            else:
                healthy_since = now
        else:
            if (
                previous_snapshot
                and not previous_snapshot["healthy"]
                and previous_snapshot["boot_id"] == boot_id
                and previous_snapshot["unhealthy_since"] is not None
            ):
                unhealthy_since = previous_snapshot["unhealthy_since"]
            else:
                unhealthy_since = now

        conn.execute(
            """
            INSERT INTO nodes (
                node_id, role, boot_id, git_commit, health_json,
                last_heartbeat_at, first_seen_at, healthy_since, unhealthy_since, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                role = excluded.role,
                boot_id = excluded.boot_id,
                git_commit = excluded.git_commit,
                health_json = excluded.health_json,
                last_heartbeat_at = excluded.last_heartbeat_at,
                healthy_since = excluded.healthy_since,
                unhealthy_since = excluded.unhealthy_since,
                updated_at = excluded.updated_at
            """,
            (
                node_id,
                role.value,
                boot_id,
                git_commit,
                json.dumps(health, sort_keys=True),
                _isoformat(now),
                previous["first_seen_at"] if previous is not None else _isoformat(now),
                _isoformat(healthy_since),
                _isoformat(unhealthy_since),
                _isoformat(now),
            ),
        )
        conn.commit()

        if previous is None:
            _audit(
                conn,
                "node.registered",
                "node",
                node_id,
                node_id,
                {"role": role.value, "boot_id": boot_id},
            )
        elif previous["boot_id"] != boot_id:
            _audit(
                conn,
                "node.rebooted",
                "node",
                node_id,
                node_id,
                {"previous_boot_id": previous["boot_id"], "boot_id": boot_id},
            )
        elif previous_snapshot and previous_snapshot["healthy"] != health_ok:
            _audit(
                conn,
                "node.health_changed",
                "node",
                node_id,
                node_id,
                {"healthy": health_ok},
            )

        status_payload = _status_payload(conn, _settings())
        node_payload = next(
            (item for item in status_payload["nodes"] if item["node_id"] == node_id),
            None,
        )
        status_payload["eligible"] = bool(node_payload["eligible"]) if node_payload else False
        return jsonify(
            {
                "mode": status_payload["mode"],
                "effective_owner_role": status_payload["effective_owner_role"],
                "effective_owner_node": status_payload["effective_owner_node"],
                "eligible": status_payload["eligible"],
            }
        )

    @app.route("/api/v1/mode", methods=["PUT"])
    def set_mode():
        _authenticate_operator_request()
        payload = _json_body()
        try:
            mode = FailoverMode(_require_string(payload, "mode"))
        except ValueError:
            return _error(
                400,
                "mode must be one of automatic, force_primary, force_secondary, paused",
            )

        conn = _db()
        now = utcnow()
        conn.execute(
            """
            UPDATE control_state
            SET mode = ?, updated_at = ?, updated_by = ?
            WHERE singleton = 1
            """,
            (mode.value, _isoformat(now), "operator"),
        )
        conn.commit()
        _audit(
            conn,
            "mode.changed",
            "operator",
            "operator",
            "mode",
            {"mode": mode.value},
        )
        return jsonify(_status_payload(conn, _settings()))

    @app.route("/api/v1/leases/acquire", methods=["POST"])
    def acquire_lease():
        configured = _authenticate_node_request()
        payload = _json_body()
        node_id = _require_string(payload, "node_id")
        role = _require_role(payload, "role")
        resource = _require_string(payload, "resource")
        ttl_seconds = _require_positive_int(payload, "ttl_seconds")
        if configured.node_id != node_id or configured.role != role:
            return _error(403, "node token does not match node_id/role")

        now = utcnow()
        conn = _db()
        with _begin_immediate(conn):
            _cleanup_expired_leases(conn, now)
            status_payload = _status_payload(conn, _settings(), now=now)
            node_payload = next(
                (item for item in status_payload["nodes"] if item["node_id"] == node_id),
                None,
            )
            if not _eligible_for_node(
                node_payload,
                status_payload["mode"],
                status_payload["nodes"],
                status_payload["active_leases"],
                _settings(),
                now,
            ):
                return _error(
                    403,
                    "node is not eligible for new leases",
                    acquired=False,
                    lease=None,
                )

            durable_record = _latest_broadcast_record_for_resource(conn, resource)
            if durable_record is not None and _status_blocks_reacquire(
                str(durable_record["status"])
            ):
                _audit(
                    conn,
                    "lease.reacquire_blocked",
                    "node",
                    node_id,
                    resource,
                    {
                        "status": durable_record["status"],
                        "fencing_token": int(durable_record["fencing_token"]),
                    },
                )
                return _error(
                    409,
                    _reacquire_block_message(durable_record),
                    acquired=False,
                    lease=None,
                    latest_status=durable_record["status"],
                )

            existing = conn.execute(
                "SELECT * FROM leases WHERE resource = ?",
                (resource,),
            ).fetchone()
            if existing is not None and _parse_timestamp(existing["expires_at"]) > now:
                lease = _lease_from_row(existing, include_token=False)
                _audit(
                    conn,
                    "lease.conflict",
                    "node",
                    node_id,
                    resource,
                    {"held_by": existing["node_id"], "resource": resource},
                )
                return _error(
                    409,
                    "resource already has an active lease",
                    acquired=False,
                    lease=lease,
                )

            state = conn.execute(
                "SELECT next_fencing_token FROM control_state WHERE singleton = 1"
            ).fetchone()
            fencing_token = int(state["next_fencing_token"])
            conn.execute(
                "UPDATE control_state SET next_fencing_token = ? WHERE singleton = 1",
                (fencing_token + 1,),
            )

            lease_token = str(uuid.uuid4())
            expires_at = now + timedelta(seconds=ttl_seconds)
            conn.execute(
                """
                INSERT OR REPLACE INTO leases (
                    resource, node_id, role, lease_token, fencing_token,
                    expires_at, acquired_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    resource,
                    node_id,
                    role.value,
                    lease_token,
                    fencing_token,
                    _isoformat(expires_at),
                    _isoformat(now),
                    _isoformat(now),
                ),
            )
            _audit(
                conn,
                "lease.acquired",
                "node",
                node_id,
                resource,
                {
                    "lease_token": lease_token,
                    "fencing_token": fencing_token,
                    "expires_at": _isoformat(expires_at),
                },
            )
        lease = {
            "resource": resource,
            "node_id": node_id,
            "role": role.value,
            "lease_token": lease_token,
            "fencing_token": fencing_token,
            "expires_at": _isoformat(expires_at),
        }
        return jsonify({"acquired": True, "lease": lease})

    @app.route("/api/v1/leases/renew", methods=["POST"])
    def renew_lease():
        configured = _authenticate_node_request()
        payload = _json_body()
        node_id = _require_string(payload, "node_id")
        resource = _require_string(payload, "resource")
        lease_token = _require_string(payload, "lease_token")
        ttl_seconds = _require_positive_int(payload, "ttl_seconds")
        if configured.node_id != node_id:
            return _error(403, "node token does not match node_id")

        now = utcnow()
        conn = _db()
        with _begin_immediate(conn):
            _cleanup_expired_leases(conn, now)
            existing = conn.execute(
                """
                SELECT * FROM leases
                WHERE resource = ? AND node_id = ? AND lease_token = ?
                """,
                (resource, node_id, lease_token),
            ).fetchone()
            if existing is None:
                return _error(409, "active lease not found", renewed=False, lease=None)

            expires_at = now + timedelta(seconds=ttl_seconds)
            conn.execute(
                "UPDATE leases SET expires_at = ?, updated_at = ? WHERE resource = ?",
                (_isoformat(expires_at), _isoformat(now), resource),
            )
            _audit(
                conn,
                "lease.renewed",
                "node",
                node_id,
                resource,
                {"lease_token": lease_token, "expires_at": _isoformat(expires_at)},
            )
        lease = _lease_from_row(
            conn.execute("SELECT * FROM leases WHERE resource = ?", (resource,)).fetchone()
        )
        return jsonify({"renewed": True, "lease": lease})

    @app.route("/api/v1/leases/release", methods=["POST"])
    def release_lease():
        configured = _authenticate_node_request()
        payload = _json_body()
        node_id = _require_string(payload, "node_id")
        resource = _require_string(payload, "resource")
        lease_token = _require_string(payload, "lease_token")
        if configured.node_id != node_id:
            return _error(403, "node token does not match node_id")

        conn = _db()
        with _begin_immediate(conn):
            deleted = conn.execute(
                "DELETE FROM leases WHERE resource = ? AND node_id = ? AND lease_token = ?",
                (resource, node_id, lease_token),
            ).rowcount
            if deleted:
                _audit(
                    conn,
                    "lease.released",
                    "node",
                    node_id,
                    resource,
                    {"lease_token": lease_token},
                )
        return jsonify({"released": bool(deleted)})

    @app.route("/api/v1/broadcasts/complete", methods=["POST"])
    def complete_broadcast():
        configured = _authenticate_node_request()
        payload = _json_body()
        node_id = _require_string(payload, "node_id")
        if configured.node_id != node_id:
            return _error(403, "node token does not match node_id")

        broadcast_id = _require_string(payload, "broadcast_id")
        resource = _require_string(payload, "resource")
        lease_token = _require_string(payload, "lease_token")
        fencing_token = _require_positive_int(payload, "fencing_token")
        status = _normalize_status(_require_string(payload, "status"))
        youtube_id = str(payload.get("youtube_id") or "").strip() or None
        if _resource_broadcast_id(resource) != broadcast_id:
            return _error(400, "resource must match broadcast_id")
        if status == "published" and youtube_id is None:
            return _error(400, "youtube_id is required for published status")

        conn = _db()
        now = utcnow()
        with _begin_immediate(conn):
            existing = conn.execute(
                """
                SELECT * FROM broadcast_records
                WHERE broadcast_id = ? AND resource = ? AND node_id = ?
                  AND lease_token = ? AND fencing_token = ? AND status = ?
                """,
                (broadcast_id, resource, node_id, lease_token, fencing_token, status),
            ).fetchone()
            if existing is not None:
                stored_youtube_id = str(existing["youtube_id"] or "").strip() or None
                if stored_youtube_id != youtube_id:
                    return _error(409, "idempotency conflict for broadcast completion")
                return jsonify(_broadcast_record_from_row(existing))

            latest_resource = _latest_broadcast_record_for_resource(conn, resource)
            if latest_resource is not None and fencing_token < int(
                latest_resource["fencing_token"]
            ):
                return _error(
                    409,
                    "stale fencing token",
                    recorded=False,
                    latest_fencing_token=int(latest_resource["fencing_token"]),
                )

            previous_for_lease = _latest_broadcast_record_for_lease(
                conn,
                resource=resource,
                node_id=node_id,
                lease_token=lease_token,
                fencing_token=fencing_token,
            )
            if previous_for_lease is not None and not _allowed_status_transition(
                resource,
                str(previous_for_lease["status"]),
                status,
            ):
                return _error(
                    409,
                    (
                        "invalid status transition for broadcast completion: "
                        f"{previous_for_lease['status']} -> {status}"
                    ),
                    recorded=False,
                )

            active_lease = conn.execute(
                """
                SELECT * FROM leases
                WHERE resource = ? AND node_id = ? AND lease_token = ? AND fencing_token = ?
                  AND expires_at > ?
                """,
                (resource, node_id, lease_token, fencing_token, _isoformat(now)),
            ).fetchone()
            if active_lease is None:
                return _error(409, "matching active lease required", recorded=False)

            conn.execute(
                """
                INSERT INTO broadcast_records (
                    broadcast_id, resource, node_id, lease_token, fencing_token,
                    status, youtube_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    broadcast_id,
                    resource,
                    node_id,
                    lease_token,
                    fencing_token,
                    status,
                    youtube_id,
                    _isoformat(now),
                    _isoformat(now),
                ),
            )
            _audit(
                conn,
                "broadcast.completed",
                "node",
                node_id,
                resource,
                {
                    "broadcast_id": broadcast_id,
                    "lease_token": lease_token,
                    "fencing_token": fencing_token,
                    "status": status,
                    "youtube_id": youtube_id,
                },
            )

        row = conn.execute(
            """
            SELECT * FROM broadcast_records
            WHERE broadcast_id = ? AND resource = ? AND node_id = ?
              AND lease_token = ? AND fencing_token = ? AND status = ?
            """,
            (broadcast_id, resource, node_id, lease_token, fencing_token, status),
        ).fetchone()
        return jsonify(_broadcast_record_from_row(row))

    @app.route("/api/v1/broadcasts/reconcile", methods=["POST"])
    def reconcile_broadcast():
        _authenticate_operator_request()
        payload = _json_body()
        resource = _require_string(payload, "resource")
        resolution = _normalize_status(_require_string(payload, "resolution"))
        requested_status = _normalize_status(payload.get("status")) or None
        youtube_id = str(payload.get("youtube_id") or "").strip() or None
        broadcast_id = _resource_broadcast_id(resource)
        if broadcast_id is None:
            return _error(400, "resource must include a broadcast_id")
        if resolution not in {"retry", "completed"}:
            return _error(400, "resolution must be one of retry, completed")

        conn = _db()
        now = utcnow()
        with _begin_immediate(conn):
            latest = _latest_broadcast_record_for_resource(conn, resource)
            if latest is None:
                return _error(404, "no durable broadcast status found for resource", recorded=False)

            latest_status = _normalize_status(latest["status"])
            if not _is_uncertain_status(latest_status):
                return _error(
                    409,
                    f"{resource} is not in an uncertain state",
                    recorded=False,
                    latest_status=latest_status,
                )

            try:
                status = _resolve_reconciliation_status(
                    resource=resource,
                    resolution=resolution,
                    requested_status=requested_status,
                    youtube_id=youtube_id,
                )
            except ValueError as exc:
                return _error(400, str(exc), recorded=False, latest_status=latest_status)

            existing = conn.execute(
                """
                SELECT * FROM broadcast_records
                WHERE broadcast_id = ? AND resource = ? AND node_id = ?
                  AND lease_token = ? AND fencing_token = ? AND status = ?
                """,
                (
                    str(latest["broadcast_id"]),
                    resource,
                    str(latest["node_id"]),
                    str(latest["lease_token"]),
                    int(latest["fencing_token"]),
                    status,
                ),
            ).fetchone()
            if existing is not None:
                stored_youtube_id = str(existing["youtube_id"] or "").strip() or None
                if stored_youtube_id != youtube_id:
                    return _error(
                        409,
                        "idempotency conflict for broadcast reconciliation",
                        recorded=False,
                    )
                response = _broadcast_record_from_row(existing)
                response["resolution"] = resolution
                return jsonify(response)

            conn.execute(
                """
                INSERT INTO broadcast_records (
                    broadcast_id, resource, node_id, lease_token, fencing_token,
                    status, youtube_id, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(latest["broadcast_id"]),
                    resource,
                    str(latest["node_id"]),
                    str(latest["lease_token"]),
                    int(latest["fencing_token"]),
                    status,
                    youtube_id,
                    _isoformat(now),
                    _isoformat(now),
                ),
            )
            _audit(
                conn,
                "broadcast.reconciled",
                "operator",
                "operator",
                resource,
                {
                    "broadcast_id": broadcast_id,
                    "resolution": resolution,
                    "previous_status": latest_status,
                    "status": status,
                    "youtube_id": youtube_id,
                    "node_id": str(latest["node_id"]),
                    "lease_token": str(latest["lease_token"]),
                    "fencing_token": int(latest["fencing_token"]),
                },
            )

        row = conn.execute(
            """
            SELECT * FROM broadcast_records
            WHERE broadcast_id = ? AND resource = ? AND node_id = ?
              AND lease_token = ? AND fencing_token = ? AND status = ?
            """,
            (
                str(latest["broadcast_id"]),
                resource,
                str(latest["node_id"]),
                str(latest["lease_token"]),
                int(latest["fencing_token"]),
                status,
            ),
        ).fetchone()
        response = _broadcast_record_from_row(row)
        response["resolution"] = resolution
        return jsonify(response)

    @app.errorhandler(PermissionError)
    def _handle_permission(exc: PermissionError):
        return _error(401, str(exc))

    @app.errorhandler(ValueError)
    def _handle_value_error(exc: ValueError):
        return _error(400, str(exc))

    return app


def _settings() -> ControlPlaneSettings:
    return current_app.config["settings"]


def _db() -> sqlite3.Connection:
    if "control_plane_db" not in g:
        settings = current_app.config["settings"]
        g.control_plane_db = _connect_db(settings.database_path)
    return g.control_plane_db


def _connect_db(database_path: str) -> sqlite3.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


def _init_db(database_path: str) -> None:
    conn = _connect_db(database_path)
    now = _isoformat(utcnow())
    try:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS control_state (
                singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                mode TEXT NOT NULL,
                next_fencing_token INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL,
                updated_by TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS nodes (
                node_id TEXT PRIMARY KEY,
                role TEXT NOT NULL,
                boot_id TEXT NOT NULL,
                git_commit TEXT NOT NULL,
                health_json TEXT NOT NULL,
                last_heartbeat_at TEXT NOT NULL,
                first_seen_at TEXT NOT NULL,
                healthy_since TEXT,
                unhealthy_since TEXT,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leases (
                resource TEXT PRIMARY KEY,
                node_id TEXT NOT NULL,
                role TEXT NOT NULL,
                lease_token TEXT NOT NULL UNIQUE,
                fencing_token INTEGER NOT NULL,
                expires_at TEXT NOT NULL,
                acquired_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_leases_node_id ON leases(node_id);
            CREATE INDEX IF NOT EXISTS idx_leases_expires_at ON leases(expires_at);
            CREATE TABLE IF NOT EXISTS broadcast_records (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                broadcast_id TEXT NOT NULL,
                resource TEXT NOT NULL,
                node_id TEXT NOT NULL,
                lease_token TEXT NOT NULL,
                fencing_token INTEGER NOT NULL,
                status TEXT NOT NULL,
                youtube_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(broadcast_id, lease_token, status)
            );
            CREATE INDEX IF NOT EXISTS idx_broadcast_records_broadcast_id
                ON broadcast_records(broadcast_id);
            CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                actor_type TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                resource TEXT,
                details_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            """
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO control_state (
                singleton, mode, next_fencing_token, updated_at, updated_by
            ) VALUES (1, ?, 1, ?, 'system')
            """,
            (FailoverMode.AUTOMATIC.value, now),
        )
        _upgrade_broadcast_records_schema(conn)
        conn.commit()
    finally:
        conn.close()


def _load_configured_nodes(settings: ControlPlaneSettings) -> list[ConfiguredNode]:
    path = Path(settings.node_tokens_file)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Could not read node credentials from {path}: {exc}") from exc

    entries: list[dict[str, Any]] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, dict):
                entries.append(item)
    elif isinstance(raw, dict):
        if isinstance(raw.get("nodes"), list):
            for item in raw["nodes"]:
                if isinstance(item, dict):
                    entries.append(item)
        else:
            for node_id, value in raw.items():
                if isinstance(value, str):
                    entries.append({"node_id": node_id, "token": value})
                elif isinstance(value, dict):
                    entry = dict(value)
                    entry.setdefault("node_id", node_id)
                    entries.append(entry)

    configured: list[ConfiguredNode] = []
    for entry in entries:
        node_id = str(entry.get("node_id") or "").strip()
        role_raw = str(entry.get("role") or "").strip().lower()
        if not node_id or not role_raw:
            continue
        role = NodeRole(role_raw)
        token = _read_secret(
            str(entry.get("token") or ""),
            str(entry.get("token_file") or ""),
            f"token for node {node_id}",
        )
        configured.append(
            ConfiguredNode(
                node_id=node_id,
                role=role,
                token_hash=_hash_token(token),
            )
        )
    if not configured:
        raise ValueError("No node credentials were loaded for the control plane")
    return configured


def _read_secret(value: str, file_path: str, label: str) -> str:
    direct = str(value or "").strip()
    if direct:
        return direct
    path_value = str(file_path or "").strip()
    if not path_value:
        raise ValueError(f"{label} is required")
    try:
        secret = Path(path_value).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"Could not read {label}: {exc}") from exc
    if not secret:
        raise ValueError(f"{label} is empty")
    return secret


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _authenticate_status_request() -> None:
    if _authenticate_operator_request(optional=True):
        return
    _authenticate_node_request()


def _authenticate_operator_request(*, optional: bool = False) -> bool:
    token = _bearer_token()
    if not token:
        if optional:
            return False
        raise PermissionError("missing bearer token")
    provided_hash = _hash_token(token)
    if hmac.compare_digest(provided_hash, current_app.config["operator_token_hash"]):
        return True
    if optional:
        return False
    raise PermissionError("invalid operator token")


def _authenticate_node_request() -> ConfiguredNode:
    token = _bearer_token()
    if not token:
        raise PermissionError("missing bearer token")
    provided_hash = _hash_token(token)
    configured = current_app.config["configured_hashes"].get(provided_hash)
    if configured is None:
        raise PermissionError("invalid node token")
    return configured


def _bearer_token() -> str | None:
    header = request.headers.get("Authorization", "")
    prefix = "Bearer "
    if not header.startswith(prefix):
        return None
    return header[len(prefix) :].strip()


@contextmanager
def _begin_immediate(conn: sqlite3.Connection) -> Iterator[None]:
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _status_payload(
    conn: sqlite3.Connection,
    settings: ControlPlaneSettings,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    now = now or utcnow()
    _cleanup_expired_leases(conn, now)

    mode_row = conn.execute("SELECT mode FROM control_state WHERE singleton = 1").fetchone()
    mode = FailoverMode(mode_row["mode"]) if mode_row is not None else FailoverMode.AUTOMATIC

    node_rows = {row["node_id"]: row for row in conn.execute("SELECT * FROM nodes").fetchall()}
    configured_nodes: dict[str, ConfiguredNode] = current_app.config["configured_nodes"]
    nodes = []
    for node_id, configured in configured_nodes.items():
        nodes.append(_node_payload(node_rows.get(node_id), configured, settings, now))
    for node_id, row in node_rows.items():
        if node_id not in configured_nodes:
            nodes.append(
                _node_payload(
                    row,
                    ConfiguredNode(node_id=node_id, role=NodeRole(row["role"]), token_hash=""),
                    settings,
                    now,
                )
            )

    active_leases = [
        _lease_from_row(row, include_token=False)
        for row in conn.execute("SELECT * FROM leases").fetchall()
    ]
    effective_role, effective_node = _effective_owner(mode, nodes, active_leases, settings, now)

    for node in nodes:
        node["eligible"] = _eligible_for_node(node, mode.value, nodes, active_leases, settings, now)

    serialized_nodes = [
        {
            **node,
            "last_heartbeat_at": _isoformat(node["last_heartbeat_at"]),
            "healthy_since": _isoformat(node["healthy_since"]),
            "unhealthy_since": _isoformat(node["unhealthy_since"]),
        }
        for node in nodes
    ]

    return {
        "mode": mode.value,
        "effective_owner_role": effective_role.value if effective_role else None,
        "effective_owner_node": effective_node,
        "nodes": serialized_nodes,
        "active_leases": active_leases,
    }


def _node_payload(
    row: sqlite3.Row | None,
    configured: ConfiguredNode,
    settings: ControlPlaneSettings,
    now: datetime,
) -> dict[str, Any]:
    snapshot = _node_snapshot_from_row(row, settings, now)
    return {
        "node_id": configured.node_id,
        "role": configured.role.value,
        "boot_id": snapshot["boot_id"],
        "git_commit": snapshot["git_commit"],
        "healthy": snapshot["healthy"],
        "eligible": False,
        "last_heartbeat_at": snapshot["last_heartbeat_at"],
        "healthy_since": snapshot["healthy_since"],
        "unhealthy_since": snapshot["unhealthy_since"],
        "health": snapshot["health"],
    }


def _node_snapshot_from_row(
    row: sqlite3.Row | None,
    settings: ControlPlaneSettings,
    now: datetime,
) -> dict[str, Any]:
    if row is None:
        return {
            "boot_id": "",
            "git_commit": "",
            "healthy": False,
            "last_heartbeat_at": None,
            "healthy_since": None,
            "unhealthy_since": None,
            "health": {},
        }
    last_heartbeat_at = _parse_timestamp(row["last_heartbeat_at"])
    healthy_since = _parse_timestamp(row["healthy_since"])
    unhealthy_since = _parse_timestamp(row["unhealthy_since"])
    health = json.loads(row["health_json"]) if row["health_json"] else {}
    age_seconds = None
    if last_heartbeat_at is not None:
        age_seconds = (now - last_heartbeat_at).total_seconds()
    fresh = age_seconds is not None and age_seconds <= settings.heartbeat_stale_seconds
    healthy = fresh and bool(health.get("ok", True))
    return {
        "boot_id": row["boot_id"],
        "git_commit": row["git_commit"],
        "healthy": healthy,
        "last_heartbeat_at": last_heartbeat_at,
        "healthy_since": healthy_since,
        "unhealthy_since": unhealthy_since,
        "health": health,
    }


def _effective_owner(
    mode: FailoverMode,
    nodes: list[dict[str, Any]],
    active_leases: list[dict[str, Any]],
    settings: ControlPlaneSettings,
    now: datetime,
) -> tuple[NodeRole | None, str | None]:
    if mode == FailoverMode.PAUSED:
        return None, None
    if mode == FailoverMode.FORCE_PRIMARY:
        return NodeRole.PRIMARY, _owner_node_for_role(NodeRole.PRIMARY, nodes, active_leases)
    if mode == FailoverMode.FORCE_SECONDARY:
        return NodeRole.SECONDARY, _owner_node_for_role(NodeRole.SECONDARY, nodes, active_leases)

    primary = _latest_node(nodes, NodeRole.PRIMARY)
    secondary = _latest_node(nodes, NodeRole.SECONDARY)
    active_primary = any(lease["role"] == NodeRole.PRIMARY.value for lease in active_leases)
    active_secondary = any(lease["role"] == NodeRole.SECONDARY.value for lease in active_leases)

    if primary and primary["healthy"]:
        if active_secondary and not _primary_recovered_stably(primary, settings, now):
            return NodeRole.SECONDARY, _owner_node_for_role(
                NodeRole.SECONDARY, nodes, active_leases
            )
        return NodeRole.PRIMARY, _owner_node_for_role(NodeRole.PRIMARY, nodes, active_leases)

    if (
        _primary_takeover_ready(primary, settings, now)
        and secondary
        and secondary["healthy"]
        and not active_primary
    ):
        return NodeRole.SECONDARY, _owner_node_for_role(
            NodeRole.SECONDARY, nodes, active_leases
        )

    if active_primary:
        return NodeRole.PRIMARY, _owner_node_for_role(NodeRole.PRIMARY, nodes, active_leases)
    if active_secondary:
        return NodeRole.SECONDARY, _owner_node_for_role(NodeRole.SECONDARY, nodes, active_leases)
    return None, None


def _eligible_for_node(
    node: dict[str, Any] | None,
    mode: str,
    nodes: list[dict[str, Any]],
    active_leases: list[dict[str, Any]],
    settings: ControlPlaneSettings,
    now: datetime,
) -> bool:
    if not node or not node["healthy"]:
        return False
    role = NodeRole(node["role"])
    resolved_mode = FailoverMode(mode)
    if resolved_mode == FailoverMode.PAUSED:
        return False
    if resolved_mode == FailoverMode.FORCE_PRIMARY:
        return role == NodeRole.PRIMARY
    if resolved_mode == FailoverMode.FORCE_SECONDARY:
        return role == NodeRole.SECONDARY

    primary = _latest_node(nodes, NodeRole.PRIMARY)
    active_primary = any(lease["role"] == NodeRole.PRIMARY.value for lease in active_leases)
    active_secondary = any(lease["role"] == NodeRole.SECONDARY.value for lease in active_leases)

    if role == NodeRole.PRIMARY:
        if active_secondary:
            return _primary_recovered_stably(node, settings, now)
        return True

    if active_primary:
        return False
    if active_secondary:
        return not (
            primary
            and primary["healthy"]
            and _primary_recovered_stably(primary, settings, now)
        )
    return _primary_takeover_ready(primary, settings, now)


def _primary_takeover_ready(
    primary: dict[str, Any] | None,
    settings: ControlPlaneSettings,
    now: datetime,
) -> bool:
    if primary is None:
        return True
    if primary["healthy"]:
        return False
    unhealthy_since = primary.get("unhealthy_since")
    if unhealthy_since is not None:
        return (now - unhealthy_since).total_seconds() >= settings.takeover_grace_seconds
    last_heartbeat_at = primary.get("last_heartbeat_at")
    if last_heartbeat_at is None:
        return True
    return (
        now - last_heartbeat_at
    ).total_seconds() >= settings.heartbeat_stale_seconds + settings.takeover_grace_seconds


def _primary_recovered_stably(
    primary: dict[str, Any] | None,
    settings: ControlPlaneSettings,
    now: datetime,
) -> bool:
    if primary is None or not primary["healthy"] or primary.get("healthy_since") is None:
        return False
    return (
        now - primary["healthy_since"]
    ).total_seconds() >= settings.stable_primary_recovery_seconds


def _latest_node(nodes: list[dict[str, Any]], role: NodeRole) -> dict[str, Any] | None:
    candidates = [node for node in nodes if node["role"] == role.value]
    if not candidates:
        return None
    return max(
        candidates,
        key=lambda node: node["last_heartbeat_at"] or datetime.min.replace(tzinfo=UTC),
    )


def _owner_node_for_role(
    role: NodeRole,
    nodes: list[dict[str, Any]],
    active_leases: list[dict[str, Any]],
) -> str | None:
    healthy = [node for node in nodes if node["role"] == role.value and node["healthy"]]
    if healthy:
        return max(
            healthy,
            key=lambda node: node["last_heartbeat_at"] or datetime.min.replace(tzinfo=UTC),
        )["node_id"]
    for lease in active_leases:
        if lease["role"] == role.value:
            return lease["node_id"]
    latest = _latest_node(nodes, role)
    return latest["node_id"] if latest else None


def _cleanup_expired_leases(conn: sqlite3.Connection, now: datetime) -> None:
    conn.execute("DELETE FROM leases WHERE expires_at <= ?", (_isoformat(now),))


def _upgrade_broadcast_records_schema(conn: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(broadcast_records)").fetchall()
    }
    if "resource" not in columns:
        conn.execute("ALTER TABLE broadcast_records ADD COLUMN resource TEXT")
    conn.execute(
        """
        UPDATE broadcast_records
        SET resource = CASE
            WHEN status IN ('published', 'publishing', 'publish_failed')
                THEN 'publish:' || broadcast_id
            WHEN status = 'recorded'
                THEN 'recorder:' || broadcast_id
            ELSE 'pipeline:' || broadcast_id
        END
        WHERE COALESCE(TRIM(resource), '') = ''
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_broadcast_records_resource
        ON broadcast_records(resource)
        """
    )
    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_broadcast_records_resource_fencing
        ON broadcast_records(resource, fencing_token, id)
        """
    )


def _resource_broadcast_id(resource: str) -> str | None:
    kind, separator, broadcast_id = str(resource or "").partition(":")
    resolved = broadcast_id.strip()
    if not separator or not resolved:
        return None
    if kind.strip().lower() == "broadcast":
        recorder_broadcast_id, recorder_separator, recorder_suffix = resolved.rpartition(":")
        if recorder_separator and recorder_broadcast_id.strip() and recorder_suffix.strip():
            return recorder_broadcast_id.strip()
    return resolved


def _resource_kind(resource: str) -> str:
    kind, _separator, _broadcast_id = str(resource or "").partition(":")
    return kind.strip().lower()


def _latest_broadcast_record_for_resource(
    conn: sqlite3.Connection,
    resource: str,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM broadcast_records
        WHERE resource = ?
        ORDER BY fencing_token DESC, id DESC
        LIMIT 1
        """,
        (resource,),
    ).fetchone()


def _latest_broadcast_record_for_lease(
    conn: sqlite3.Connection,
    *,
    resource: str,
    node_id: str,
    lease_token: str,
    fencing_token: int,
) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT * FROM broadcast_records
        WHERE resource = ? AND node_id = ? AND lease_token = ? AND fencing_token = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (resource, node_id, lease_token, fencing_token),
    ).fetchone()


def _normalize_status(value: Any) -> str:
    return str(value or "").strip().lower()


def _is_uncertain_status(status: str) -> bool:
    return _normalize_status(status) in {"processing", "publishing"}


def _retry_status_for_resource(resource: str) -> str:
    return "publish_failed" if _resource_kind(resource) == "publish" else "failed"


def _default_completed_status_for_resource(resource: str) -> str:
    return "published" if _resource_kind(resource) == "publish" else "completed"


def _resolve_reconciliation_status(
    *,
    resource: str,
    resolution: str,
    requested_status: str | None,
    youtube_id: str | None,
) -> str:
    if resolution == "retry":
        expected = _retry_status_for_resource(resource)
        status = requested_status or expected
        if status != expected:
            raise ValueError(
                f"retry resolution for {resource} must use status {expected}"
            )
        return status

    if resolution != "completed":
        raise ValueError("resolution must be one of retry, completed")

    status = requested_status or _default_completed_status_for_resource(resource)
    if _is_uncertain_status(status):
        raise ValueError("completed resolution requires an authoritative terminal status")
    if not _status_blocks_reacquire(status):
        raise ValueError("completed resolution requires a terminal blocking status")
    if _resource_kind(resource) == "publish" and status != "published":
        raise ValueError("publish resources may only be reconciled as published")
    if status == "published" and youtube_id is None:
        raise ValueError("youtube_id is required for published status")
    return status


def _status_blocks_reacquire(status: str) -> bool:
    normalized = _normalize_status(status)
    if not normalized:
        return False
    return normalized != "failed" and not normalized.endswith("_failed")


def _reacquire_block_message(row: sqlite3.Row) -> str:
    status = _normalize_status(row["status"])
    resource = str(row["resource"] or "").strip()
    if status == "processing":
        return (
            f"{resource} is already marked processing; paid pipeline work may have started "
            "and reacquisition is blocked until an operator reconciles it"
        )
    if status == "publishing":
        return (
            f"{resource} is already marked publishing; the upload outcome is uncertain "
            "and reacquisition is blocked until an operator reconciles it"
        )
    return f"{resource} is already recorded with status {status}"


def _allowed_status_transition(resource: str, previous_status: str, next_status: str) -> bool:
    previous = _normalize_status(previous_status)
    nxt = _normalize_status(next_status)
    if previous == nxt:
        return True
    if resource.startswith("pipeline:") and previous == "processing":
        return bool(nxt) and not _is_uncertain_status(nxt)
    if resource.startswith("publish:") and previous == "publishing":
        return nxt in {"published", "publish_failed"}
    return False


def _lease_from_row(
    row: sqlite3.Row | None,
    *,
    include_token: bool = True,
) -> dict[str, Any] | None:
    if row is None:
        return None
    lease = {
        "resource": row["resource"],
        "node_id": row["node_id"],
        "role": row["role"],
        "fencing_token": int(row["fencing_token"]),
        "expires_at": row["expires_at"],
    }
    if include_token:
        lease["lease_token"] = row["lease_token"]
    return lease


def _broadcast_record_from_row(row: sqlite3.Row | None) -> dict[str, Any]:
    if row is None:
        return {"recorded": False}
    return {
        "recorded": True,
        "broadcast_id": row["broadcast_id"],
        "resource": row["resource"],
        "node_id": row["node_id"],
        "lease_token": row["lease_token"],
        "fencing_token": int(row["fencing_token"]),
        "status": row["status"],
        "youtube_id": row["youtube_id"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


def _audit(
    conn: sqlite3.Connection,
    event_type: str,
    actor_type: str,
    actor_id: str,
    resource: str | None,
    details: dict[str, Any],
) -> None:
    conn.execute(
        """
        INSERT INTO audit_events (
            event_type, actor_type, actor_id, resource, details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            actor_type,
            actor_id,
            resource,
            json.dumps(details, sort_keys=True),
            _isoformat(utcnow()),
        ),
    )


def _json_body() -> dict[str, Any]:
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise ValueError("request body must be a JSON object")
    return payload


def _require_string(payload: dict[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise ValueError(f"{key} is required")
    return value


def _require_role(payload: dict[str, Any], key: str) -> NodeRole:
    try:
        return NodeRole(_require_string(payload, key).lower())
    except ValueError as exc:
        raise ValueError(f"{key} must be 'primary' or 'secondary'") from exc


def _require_positive_int(payload: dict[str, Any], key: str) -> int:
    value = payload.get(key)
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{key} must be a positive integer") from exc
    if parsed <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return parsed


def _error(status_code: int, message: str, **payload: Any):
    body = {"error": message, **payload}
    return jsonify(body), status_code
