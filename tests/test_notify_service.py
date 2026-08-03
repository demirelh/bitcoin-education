"""Tests for the WhatsApp notification service client."""

from __future__ import annotations

import json
import urllib.error
from unittest.mock import MagicMock, patch

from btcedu.config import Settings
from btcedu.services.notify_service import (
    notify_stage_failure,
    send_notification,
)


def _settings(**overrides) -> Settings:
    base = {
        "notify_whatsapp_enabled": True,
        "notify_whatsapp_url": "http://127.0.0.1:3010",
        "dry_run": False,
    }
    base.update(overrides)
    return Settings(**base)


def _response(payload: dict) -> MagicMock:
    response = MagicMock()
    response.read.return_value = json.dumps(payload).encode("utf-8")
    response.__enter__ = lambda self: self
    response.__exit__ = lambda self, *args: False
    return response


class TestSendNotification:
    def test_posts_message_to_service(self):
        with patch("urllib.request.urlopen", return_value=_response({"success": True})) as mock:
            assert send_notification(_settings(), "Hallo") is True

        request = mock.call_args[0][0]
        assert request.full_url == "http://127.0.0.1:3010/send"
        assert json.loads(request.data.decode("utf-8")) == {"message": "Hallo"}

    def test_includes_token_and_recipient(self):
        settings = _settings(notify_whatsapp_token="secret", notify_whatsapp_number="4915112345678")
        with patch("urllib.request.urlopen", return_value=_response({"success": True})) as mock:
            assert send_notification(settings, "Hallo") is True

        request = mock.call_args[0][0]
        assert request.get_header("Authorization") == "Bearer secret"
        assert json.loads(request.data.decode("utf-8"))["to"] == "4915112345678"

    def test_disabled_does_not_call_service(self):
        with patch("urllib.request.urlopen") as mock:
            assert send_notification(_settings(notify_whatsapp_enabled=False), "Hallo") is False
        mock.assert_not_called()

    def test_dry_run_does_not_call_service(self):
        with patch("urllib.request.urlopen") as mock:
            assert send_notification(_settings(dry_run=True), "Hallo") is False
        mock.assert_not_called()

    def test_empty_message_is_ignored(self):
        with patch("urllib.request.urlopen") as mock:
            assert send_notification(_settings(), "   ") is False
        mock.assert_not_called()

    def test_connection_error_is_swallowed(self):
        with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
            assert send_notification(_settings(), "Hallo") is False

    def test_service_rejection_returns_false(self):
        with patch("urllib.request.urlopen", return_value=_response({"success": False})):
            assert send_notification(_settings(), "Hallo") is False

    def test_message_is_truncated(self):
        with patch("urllib.request.urlopen", return_value=_response({"success": True})) as mock:
            send_notification(_settings(), "x" * 5000)

        request = mock.call_args[0][0]
        assert len(json.loads(request.data.decode("utf-8"))["message"]) == 3000


class TestNotifyStageFailure:
    def test_message_contains_context(self):
        with patch("urllib.request.urlopen", return_value=_response({"success": True})) as mock:
            assert (
                notify_stage_failure(
                    _settings(),
                    episode_id="IuNt7iyNtkI",
                    episode_title="tagesschau 20:00 Uhr",
                    stage="render",
                    error="[not_found] chapter ch02 has unresolved media",
                    retry_count=1,
                )
                is True
            )

        message = json.loads(mock.call_args[0][0].data.decode("utf-8"))["message"]
        assert "IuNt7iyNtkI" in message
        assert "tagesschau 20:00 Uhr" in message
        assert "render" in message
        assert "unresolved media" in message
