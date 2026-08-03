"""Push notifications for pipeline events.

Messages are handed to the local ``whatsapp-service`` REST API
(see ``/home/pi/services/whatsapp-service``). That service owns the WhatsApp
session and a durable outbox, so a notification is never lost even when the
WhatsApp connection is temporarily down.

Notification failures must never influence the pipeline: every function here
swallows its errors and only logs them.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from datetime import UTC, datetime

from btcedu.config import Settings

logger = logging.getLogger(__name__)

MAX_MESSAGE_LENGTH = 3000


def _post(settings: Settings, message: str) -> bool:
    payload: dict[str, str] = {"message": message[:MAX_MESSAGE_LENGTH]}
    if settings.notify_whatsapp_number:
        payload["to"] = settings.notify_whatsapp_number

    url = f"{settings.notify_whatsapp_url.rstrip('/')}/send"
    headers = {"Content-Type": "application/json"}
    if settings.notify_whatsapp_token:
        headers["Authorization"] = f"Bearer {settings.notify_whatsapp_token}"

    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=settings.notify_whatsapp_timeout) as response:
            body = response.read().decode("utf-8")
            data = json.loads(body) if body else {}
            if data.get("success"):
                return True
            logger.warning("WhatsApp notification rejected: %s", data.get("error", body))
            return False
    except urllib.error.HTTPError as exc:
        logger.warning("WhatsApp notification failed (HTTP %s): %s", exc.code, exc.reason)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.warning("WhatsApp notification could not be delivered: %s", exc)
    return False


def send_notification(settings: Settings, message: str) -> bool:
    """Send a plain notification. Returns True when the service accepted it."""
    if not settings.notify_whatsapp_enabled:
        logger.debug("WhatsApp notifications disabled, skipping message")
        return False
    if settings.dry_run:
        logger.info("[DRY RUN] Would send WhatsApp notification: %s", message.splitlines()[0])
        return False
    if not message.strip():
        return False
    return _post(settings, message)


def _request_json(settings: Settings, path: str, method: str = "GET") -> dict:
    """Call the whatsapp-service and return its JSON body (or an error dict)."""
    url = f"{settings.notify_whatsapp_url.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    if settings.notify_whatsapp_token:
        headers["Authorization"] = f"Bearer {settings.notify_whatsapp_token}"

    request = urllib.request.Request(url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=settings.notify_whatsapp_timeout) as response:
            body = response.read().decode("utf-8")
            return json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            return json.loads(body) if body else {"error": exc.reason}
        except json.JSONDecodeError:
            return {"error": f"HTTP {exc.code}: {body.strip() or exc.reason}"}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        logger.debug("WhatsApp service unreachable: %s", exc)
        return {"error": f"whatsapp service unreachable: {exc}"}


def get_pairing_status(settings: Settings) -> dict:
    """Return connection state plus the current QR code as a PNG data URL."""
    if not settings.notify_whatsapp_enabled:
        return {
            "enabled": False,
            "connected": False,
            "status": "disabled",
            "error": "WhatsApp notifications are disabled (NOTIFY_WHATSAPP_ENABLED)",
        }
    data = _request_json(settings, "/qr")
    data["enabled"] = True
    data.setdefault("connected", False)
    data.setdefault("status", "unknown")
    return data


def request_relink(settings: Settings) -> dict:
    """Ask the service to drop its session so a fresh QR code is generated."""
    if not settings.notify_whatsapp_enabled:
        return {"success": False, "error": "WhatsApp notifications are disabled"}
    data = _request_json(settings, "/relink", method="POST")
    data.setdefault("success", "error" not in data)
    return data


def notify_stage_failure(
    settings: Settings,
    *,
    episode_id: str,
    episode_title: str,
    stage: str,
    error: str,
    retry_count: int = 0,
) -> bool:
    """Notify about a failed pipeline stage."""
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    title = episode_title.strip() or "(no title)"
    message = (
        "\u274c btcedu Pipeline-Fehler\n"
        f"Episode: {title}\n"
        f"ID: {episode_id}\n"
        f"Stage: {stage}\n"
        f"Versuche: {retry_count}\n"
        f"Zeit: {timestamp}\n\n"
        f"{error.strip()}"
    )
    return send_notification(settings, message)
