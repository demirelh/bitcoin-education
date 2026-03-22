"""Tests for D-ID anchor video service."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.services.anchor_service import (
    AnchorRequest,
    DIDService,
    DryRunAnchorService,
)


class TestDryRunAnchorService:
    """Tests for DryRunAnchorService."""

    def test_generate_returns_placeholder(self, tmp_path):
        service = DryRunAnchorService(output_dir=str(tmp_path / "anchor"))
        request = AnchorRequest(
            source_image_path="/tmp/photo.png",
            source_image_url="",
            audio_path="/tmp/audio.mp3",
            chapter_id="ch_01",
        )
        response = service.generate_anchor_video(request)

        assert response.chapter_id == "ch_01"
        assert response.duration_seconds == 30.0
        assert response.cost_usd == 0.0
        assert response.did_talk_id == "dry-run"
        assert Path(response.video_path).exists()

    def test_generate_creates_output_dir(self, tmp_path):
        service = DryRunAnchorService(output_dir=str(tmp_path / "new" / "anchor"))
        request = AnchorRequest(
            source_image_path="/tmp/photo.png",
            source_image_url="",
            audio_path="/tmp/audio.mp3",
            chapter_id="ch_02",
        )
        response = service.generate_anchor_video(request)
        assert Path(response.video_path).exists()

    def test_generates_multiple_chapters(self, tmp_path):
        service = DryRunAnchorService(output_dir=str(tmp_path / "anchor"))
        for i in range(3):
            request = AnchorRequest(
                source_image_path="/tmp/photo.png",
                source_image_url="",
                audio_path=f"/tmp/audio_{i}.mp3",
                chapter_id=f"ch_{i:02d}",
            )
            response = service.generate_anchor_video(request)
            assert response.chapter_id == f"ch_{i:02d}"


class TestDIDService:
    """Tests for DIDService with mocked HTTP."""

    @pytest.fixture
    def service(self, tmp_path):
        return DIDService(api_key="test-key", output_dir=str(tmp_path / "anchor"))

    def test_create_talk_sends_correct_payload(self, service):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"id": "talk_123"}
        mock_resp.raise_for_status = MagicMock()

        service.session.post = MagicMock(return_value=mock_resp)

        talk_id = service._create_talk(
            "https://example.com/image.png",
            "https://example.com/audio.mp3",
            "serious",
        )

        assert talk_id == "talk_123"
        call_args = service.session.post.call_args
        assert "talks" in call_args[0][0]
        payload = call_args[1]["json"]
        assert payload["source_url"] == "https://example.com/image.png"
        assert payload["script"]["audio_url"] == "https://example.com/audio.mp3"

    def test_poll_talk_returns_on_done(self, service):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "done",
            "result_url": "https://example.com/result.mp4",
            "duration": 30.5,
        }
        mock_resp.raise_for_status = MagicMock()
        service.session.get = MagicMock(return_value=mock_resp)

        result_url, duration = service._poll_talk("talk_123")
        assert result_url == "https://example.com/result.mp4"
        assert duration == 30.5

    def test_poll_talk_raises_on_error(self, service):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "error",
            "error": {"description": "Content policy violation"},
        }
        mock_resp.raise_for_status = MagicMock()
        service.session.get = MagicMock(return_value=mock_resp)

        with pytest.raises(RuntimeError, match="Content policy violation"):
            service._poll_talk("talk_123")

    @patch("btcedu.services.anchor_service.POLL_MAX_ATTEMPTS", 2)
    @patch("btcedu.services.anchor_service.POLL_INTERVAL_SECONDS", 0)
    def test_poll_talk_raises_on_timeout(self, service):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "started"}
        mock_resp.raise_for_status = MagicMock()
        service.session.get = MagicMock(return_value=mock_resp)

        with pytest.raises(TimeoutError, match="did not complete"):
            service._poll_talk("talk_123")

    def test_upload_image(self, service, tmp_path):
        img_path = tmp_path / "test.png"
        img_path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"url": "https://d-id.com/images/abc123"}
        mock_resp.raise_for_status = MagicMock()
        service.session.post = MagicMock(return_value=mock_resp)

        url = service._upload_image(str(img_path))
        assert url == "https://d-id.com/images/abc123"

    def test_upload_audio(self, service, tmp_path):
        audio_path = tmp_path / "test.mp3"
        audio_path.write_bytes(b"\xff\xfb\x90" + b"\x00" * 100)

        mock_resp = MagicMock()
        mock_resp.json.return_value = {"url": "https://d-id.com/audios/def456"}
        mock_resp.raise_for_status = MagicMock()
        service.session.post = MagicMock(return_value=mock_resp)

        url = service._upload_audio(str(audio_path))
        assert url == "https://d-id.com/audios/def456"

    def test_headers_include_auth(self, service):
        assert "Authorization" in service.session.headers
        assert service.session.headers["Authorization"] == "Basic test-key"


class TestAnchorRequest:
    """Tests for AnchorRequest dataclass."""

    def test_default_expression(self):
        req = AnchorRequest(
            source_image_path="/tmp/photo.png",
            source_image_url="",
            audio_path="/tmp/audio.mp3",
            chapter_id="ch_01",
        )
        assert req.expression == "serious"

    def test_custom_expression(self):
        req = AnchorRequest(
            source_image_path="/tmp/photo.png",
            source_image_url="https://example.com/photo.png",
            audio_path="/tmp/audio.mp3",
            chapter_id="ch_01",
            expression="happy",
        )
        assert req.expression == "happy"
        assert req.source_image_url == "https://example.com/photo.png"
