"""Tests for provider-neutral talking-avatar services."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.services.anchor_service import (
    AnchorAPIError,
    AnchorRequest,
    AnchorResponse,
    DIDService,
    DryRunAnchorService,
    HeyGenService,
    heygen_cost_per_second,
)


class TestDryRunAnchorService:
    """Tests for DryRunAnchorService."""

    def test_generate_returns_placeholder(self, tmp_path):
        service = DryRunAnchorService(output_dir=str(tmp_path / "anchor"))
        request = AnchorRequest(
            source_image_path="photo.png",
            source_image_url="",
            audio_path="audio.mp3",
            chapter_id="ch_01",
        )
        response = service.generate_anchor_video(request)

        assert response.chapter_id == "ch_01"
        assert response.duration_seconds == 30.0
        assert response.cost_usd == 0.0
        assert response.did_talk_id == "dry-run"
        assert response.provider == "d-id"
        assert response.provider_job_id == "dry-run"
        assert Path(response.video_path).exists()

    def test_generate_creates_output_dir(self, tmp_path):
        service = DryRunAnchorService(output_dir=str(tmp_path / "new" / "anchor"))
        request = AnchorRequest(
            source_image_path="photo.png",
            source_image_url="",
            audio_path="audio.mp3",
            chapter_id="ch_02",
        )
        response = service.generate_anchor_video(request)
        assert Path(response.video_path).exists()

    def test_generates_multiple_chapters(self, tmp_path):
        service = DryRunAnchorService(output_dir=str(tmp_path / "anchor"))
        for i in range(3):
            request = AnchorRequest(
                source_image_path="photo.png",
                source_image_url="",
                audio_path=f"audio_{i}.mp3",
                chapter_id=f"ch_{i:02d}",
            )
            response = service.generate_anchor_video(request)
            assert response.chapter_id == f"ch_{i:02d}"

    def test_dry_run_retains_optional_webm_capability(self, tmp_path):
        service = DryRunAnchorService(
            output_dir=str(tmp_path / "anchor"),
            provider="heygen",
            engine="avatar_iv",
            output_format="webm",
        )
        response = service.generate_anchor_video(
            AnchorRequest(
                source_image_path="",
                source_image_url="",
                audio_path="audio.mp3",
                chapter_id="ch_01",
                expected_duration_seconds=12.5,
            )
        )

        assert response.provider == "heygen"
        assert response.did_talk_id == ""
        assert response.duration_seconds == 12.5
        assert response.video_path.endswith(".webm")
        assert response.mime_type == "video/webm"


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

    def test_estimates_cost_from_duration(self, service):
        assert service.estimate_cost(10) == 0.15


class TestHeyGenService:
    """Tests for the HeyGen v3 pre-generated-audio adapter."""

    @pytest.fixture
    def service(self, tmp_path):
        return HeyGenService(
            api_key="test-key",
            output_dir=str(tmp_path / "anchor"),
            avatar_id="avatar_123",
            engine="avatar_iv",
            avatar_type="digital_twin",
            output_format="webm",
        )

    def test_headers_use_heygen_api_key(self, service):
        assert service.session.headers["x-api-key"] == "test-key"
        assert "Authorization" not in service.session.headers

    def test_upload_audio_uses_v3_asset_contract(self, service, tmp_path):
        audio_path = tmp_path / "narration.mp3"
        audio_path.write_bytes(b"\xff\xfb\x90" + b"\x00" * 100)
        response = MagicMock(status_code=200)
        response.json.return_value = {"data": {"asset_id": "asset_123"}}
        service.session.post = MagicMock(return_value=response)

        asset_id = service._upload_audio(str(audio_path))

        assert asset_id == "asset_123"
        call = service.session.post.call_args
        assert call.args[0].endswith("/v3/assets")
        assert call.kwargs["files"]["file"][0] == "narration.mp3"
        assert call.kwargs["files"]["file"][2] == "audio/mpeg"
        assert call.kwargs["timeout"] == 120

    def test_create_video_can_request_optional_transparent_webm(self, service):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "data": {
                "video_id": "video_123",
                "status": "waiting",
                "output_format": "webm",
            }
        }
        service.session.post = MagicMock(return_value=response)

        video_id = service._create_video("asset_123", "ch_01")

        assert video_id == "video_123"
        call = service.session.post.call_args
        assert call.args[0].endswith("/v3/videos")
        payload = call.kwargs["json"]
        assert payload["type"] == "avatar"
        assert payload["avatar_id"] == "avatar_123"
        assert payload["audio_asset_id"] == "asset_123"
        assert "script" not in payload
        assert payload["engine"] == {"type": "avatar_iv"}
        assert payload["output_format"] == "webm"
        assert "background" not in payload

    def test_create_video_rejects_provider_format_mismatch(self, service):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "data": {"video_id": "video_123", "output_format": "mp4"}
        }
        service.session.post = MagicMock(return_value=response)

        with pytest.raises(AnchorAPIError, match="provider resolved mp4"):
            service._create_video("asset_123", "ch_01")

    def test_poll_video_returns_completed_result(self, service):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "data": {
                "id": "video_123",
                "status": "completed",
                "video_url": "https://files.heygen.com/video.webm",
                "duration": 12.5,
            }
        }
        service.session.get = MagicMock(return_value=response)

        assert service._poll_video("video_123") == (
            "https://files.heygen.com/video.webm",
            12.5,
        )

    def test_poll_video_raises_structured_failure(self, service):
        response = MagicMock(status_code=200)
        response.json.return_value = {
            "data": {
                "status": "failed",
                "failure_code": "avatar_not_matted",
                "failure_message": "This avatar does not support webm output",
            }
        }
        service.session.get = MagicMock(return_value=response)

        with pytest.raises(AnchorAPIError) as exc_info:
            service._poll_video("video_123")

        assert exc_info.value.provider == "HeyGen"
        assert exc_info.value.error_code == "avatar_not_matted"
        assert "does not support webm" in str(exc_info.value)

    def test_api_error_retains_status_and_code(self, service, tmp_path):
        audio_path = tmp_path / "narration.mp3"
        audio_path.write_bytes(b"\xff\xfb\x90" + b"\x00" * 100)
        response = MagicMock(status_code=402)
        response.json.return_value = {
            "error": {
                "code": "insufficient_credit",
                "message": "Additional credit is required",
            }
        }
        service.session.post = MagicMock(return_value=response)

        with pytest.raises(AnchorAPIError) as exc_info:
            service._upload_audio(str(audio_path))

        assert exc_info.value.status_code == 402
        assert exc_info.value.error_code == "insufficient_credit"
        assert "Additional credit is required" in str(exc_info.value)

    def test_estimates_published_avatar_iv_digital_twin_cost(self, service):
        assert service.estimate_cost(30) == 2.001

    def test_requires_credentials_and_avatar(self, tmp_path):
        with pytest.raises(ValueError, match="HEYGEN_API_KEY"):
            HeyGenService("", str(tmp_path), "avatar_123")
        with pytest.raises(ValueError, match="HEYGEN_AVATAR_ID"):
            HeyGenService("test-key", str(tmp_path), "")


class TestHeyGenPricing:
    def test_known_engine_avatar_rates(self):
        assert heygen_cost_per_second("avatar_iv", "photo_avatar") == 0.05
        assert heygen_cost_per_second("avatar_iii", "studio_avatar") == 0.0167

    def test_unknown_pricing_combination_is_rejected(self):
        with pytest.raises(ValueError, match="Unsupported HeyGen pricing combination"):
            heygen_cost_per_second("avatar_v", "photo_avatar")


class TestAnchorRequest:
    """Tests for AnchorRequest dataclass."""

    def test_default_expression(self):
        req = AnchorRequest(
            source_image_path="photo.png",
            source_image_url="",
            audio_path="audio.mp3",
            chapter_id="ch_01",
        )
        assert req.expression == "serious"

    def test_custom_expression(self):
        req = AnchorRequest(
            source_image_path="photo.png",
            source_image_url="https://example.com/photo.png",
            audio_path="audio.mp3",
            chapter_id="ch_01",
            expression="happy",
        )
        assert req.expression == "happy"
        assert req.source_image_url == "https://example.com/photo.png"


class TestAnchorResponse:
    def test_legacy_did_identifier_populates_generic_job_id(self):
        response = AnchorResponse(
            video_path="anchor/ch_01.mp4",
            chapter_id="ch_01",
            duration_seconds=3.0,
            size_bytes=100,
            cost_usd=0.045,
            did_talk_id="talk_123",
        )
        assert response.provider == "d-id"
        assert response.provider_job_id == "talk_123"

    def test_generic_did_job_id_populates_legacy_identifier(self):
        response = AnchorResponse(
            video_path="anchor/ch_01.mp4",
            chapter_id="ch_01",
            duration_seconds=3.0,
            size_bytes=100,
            cost_usd=0.045,
            provider="d-id",
            provider_job_id="talk_123",
        )
        assert response.did_talk_id == "talk_123"
