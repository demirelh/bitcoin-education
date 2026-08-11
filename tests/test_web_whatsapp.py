"""Tests for the WhatsApp pairing endpoints of the dashboard.

Covers:
- GET /api/whatsapp/status (QR code proxy)
- POST /api/whatsapp/relink
- POST /api/whatsapp/test
- GET /whatsapp (pairing page)
"""

from unittest.mock import patch

import pytest
from flask import Flask

from btcedu.config import Settings
from btcedu.web.api import api_bp


@pytest.fixture
def settings():
    return Settings(
        notify_whatsapp_enabled=True,
        notify_whatsapp_url="http://127.0.0.1:3010",
        dry_run=False,
    )


@pytest.fixture
def client(settings):
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["settings"] = settings
    app.register_blueprint(api_bp, url_prefix="/api")
    return app.test_client()


class TestWhatsAppStatus:
    def test_returns_service_payload(self, client):
        payload = {
            "status": "awaiting_qr",
            "connected": False,
            "dataUrl": "data:image/png;base64,x",
        }
        with patch("btcedu.services.notify_service._request_json", return_value=payload):
            response = client.get("/api/whatsapp/status")

        assert response.status_code == 200
        body = response.get_json()
        assert body["enabled"] is True
        assert body["dataUrl"].startswith("data:image/png")

    def test_reports_unreachable_service(self, client):
        with patch(
            "btcedu.services.notify_service._request_json",
            return_value={"error": "whatsapp service unreachable: refused"},
        ):
            body = client.get("/api/whatsapp/status").get_json()

        assert body["connected"] is False
        assert "unreachable" in body["error"]

    def test_disabled_notifications(self, client, settings):
        settings.notify_whatsapp_enabled = False
        body = client.get("/api/whatsapp/status").get_json()
        assert body["enabled"] is False
        assert body["status"] == "disabled"


class TestWhatsAppRelink:
    def test_success(self, client):
        with patch(
            "btcedu.services.notify_service._request_json",
            return_value={"success": True, "status": "connecting"},
        ):
            response = client.post("/api/whatsapp/relink")

        assert response.status_code == 200
        assert response.get_json()["success"] is True

    def test_failure_returns_502(self, client):
        with patch(
            "btcedu.services.notify_service._request_json",
            return_value={"error": "whatsapp service unreachable"},
        ):
            response = client.post("/api/whatsapp/relink")

        assert response.status_code == 502
        assert response.get_json()["success"] is False


class TestWhatsAppTest:
    def test_sends_default_message(self, client):
        with patch("btcedu.services.notify_service._post", return_value=True) as mock:
            response = client.post("/api/whatsapp/test", json={})

        assert response.status_code == 200
        assert response.get_json()["success"] is True
        assert "btcedu" in mock.call_args[0][1]

    def test_sends_custom_message(self, client):
        with patch("btcedu.services.notify_service._post", return_value=True) as mock:
            client.post("/api/whatsapp/test", json={"message": "Hallo Pi"})

        assert mock.call_args[0][1] == "Hallo Pi"

    def test_failure_returns_502(self, client):
        with patch("btcedu.services.notify_service._post", return_value=False):
            response = client.post("/api/whatsapp/test", json={})

        assert response.status_code == 502
        assert response.get_json()["success"] is False


class TestPairingPage:
    def test_page_renders(self, tmp_path):
        from btcedu.web.app import create_app

        app = create_app(
            Settings(
                database_url=f"sqlite:///{tmp_path / 'test.db'}",
                logs_dir=str(tmp_path / "logs"),
            )
        )
        app.config["TESTING"] = True
        response = app.test_client().get("/whatsapp")

        assert response.status_code == 200
        assert b"api/whatsapp/status" in response.data
