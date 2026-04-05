"""Tests for Gemini 2.0 Flash image editing service."""

import base64
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.services.gemini_image_service import (
    GEMINI_FLASH_COST_PER_EDIT,
    GeminiEditRequest,
    GeminiImageService,
)


@pytest.fixture
def service():
    return GeminiImageService(api_key="test-key", model="gemini-2.0-flash-exp")


@pytest.fixture
def sample_image(tmp_path):
    """Create a minimal PNG file."""
    # Minimal 1x1 PNG
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde\x00"
        b"\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00"
        b"\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    img = tmp_path / "test_frame.png"
    img.write_bytes(png_bytes)
    return img


def _make_gemini_response(image_bytes=b"edited-image-data"):
    """Build a mock Gemini API response with an image."""
    return {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "Here is the edited image."},
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": base64.b64encode(image_bytes).decode(),
                            }
                        },
                    ]
                }
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 500,
            "candidatesTokenCount": 300,
        },
    }


class TestGeminiImageService:
    def test_edit_image_success(self, service, sample_image, tmp_path):
        output = tmp_path / "output.png"
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = _make_gemini_response(b"edited-png-data")

        with patch("btcedu.services.gemini_image_service.requests.post", return_value=mock_resp):
            result = service.edit_image(
                GeminiEditRequest(image_path=sample_image, prompt="Translate text to Turkish"),
                output_path=output,
            )

        assert result.output_path == output
        assert output.read_bytes() == b"edited-png-data"
        assert result.cost_usd == GEMINI_FLASH_COST_PER_EDIT
        assert result.prompt_tokens == 500
        assert result.completion_tokens == 300
        assert result.model == "gemini-2.0-flash-exp"

    def test_edit_image_no_image_in_response(self, service, sample_image, tmp_path):
        output = tmp_path / "output.png"
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {"content": {"parts": [{"text": "I cannot edit this image."}]}}
            ],
            "usageMetadata": {},
        }

        with patch("btcedu.services.gemini_image_service.requests.post", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="did not return an image"):
                service.edit_image(
                    GeminiEditRequest(image_path=sample_image, prompt="test"),
                    output_path=output,
                )

    def test_edit_image_api_error(self, service, sample_image, tmp_path):
        output = tmp_path / "output.png"
        mock_resp = MagicMock()
        mock_resp.ok = False
        mock_resp.status_code = 400
        mock_resp.text = "Invalid request"

        with patch("btcedu.services.gemini_image_service.requests.post", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="Gemini API error 400"):
                service.edit_image(
                    GeminiEditRequest(image_path=sample_image, prompt="test"),
                    output_path=output,
                )

    def test_edit_image_retries_on_rate_limit(self, service, sample_image, tmp_path):
        output = tmp_path / "output.png"

        rate_limited = MagicMock()
        rate_limited.ok = False
        rate_limited.status_code = 429

        success = MagicMock()
        success.ok = True
        success.status_code = 200
        success.json.return_value = _make_gemini_response()

        with patch(
            "btcedu.services.gemini_image_service.requests.post",
            side_effect=[rate_limited, success],
        ):
            with patch("btcedu.services.gemini_image_service.time.sleep"):
                result = service.edit_image(
                    GeminiEditRequest(image_path=sample_image, prompt="test"),
                    output_path=output,
                )

        assert result.output_path == output

    def test_edit_image_retries_on_server_error(self, service, sample_image, tmp_path):
        output = tmp_path / "output.png"

        server_error = MagicMock()
        server_error.ok = False
        server_error.status_code = 500

        success = MagicMock()
        success.ok = True
        success.status_code = 200
        success.json.return_value = _make_gemini_response()

        with patch(
            "btcedu.services.gemini_image_service.requests.post",
            side_effect=[server_error, success],
        ):
            with patch("btcedu.services.gemini_image_service.time.sleep"):
                result = service.edit_image(
                    GeminiEditRequest(image_path=sample_image, prompt="test"),
                    output_path=output,
                )

        assert result.output_path == output

    def test_edit_image_exhausts_retries(self, service, sample_image, tmp_path):
        output = tmp_path / "output.png"

        rate_limited = MagicMock()
        rate_limited.ok = False
        rate_limited.status_code = 429

        with patch(
            "btcedu.services.gemini_image_service.requests.post",
            return_value=rate_limited,
        ):
            with patch("btcedu.services.gemini_image_service.time.sleep"):
                with pytest.raises(RuntimeError, match="failed after"):
                    service.edit_image(
                        GeminiEditRequest(image_path=sample_image, prompt="test"),
                        output_path=output,
                    )

    def test_detect_mime(self):
        assert GeminiImageService._detect_mime(Path("test.png")) == "image/png"
        assert GeminiImageService._detect_mime(Path("test.jpg")) == "image/jpeg"
        assert GeminiImageService._detect_mime(Path("test.jpeg")) == "image/jpeg"
        assert GeminiImageService._detect_mime(Path("test.webp")) == "image/webp"
        assert GeminiImageService._detect_mime(Path("test.bmp")) == "image/png"  # fallback

    def test_no_candidates_raises(self, service, sample_image, tmp_path):
        output = tmp_path / "output.png"
        mock_resp = MagicMock()
        mock_resp.ok = True
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"candidates": [], "usageMetadata": {}}

        with patch("btcedu.services.gemini_image_service.requests.post", return_value=mock_resp):
            with pytest.raises(RuntimeError, match="no candidates"):
                service.edit_image(
                    GeminiEditRequest(image_path=sample_image, prompt="test"),
                    output_path=output,
                )
