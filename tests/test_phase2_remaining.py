"""Tests for Phase 2 remaining features: sketch preset, DALL-E Edit, alt frames."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from btcedu.services.ffmpeg_service import _STYLE_FILTER_PRESETS
from btcedu.services.image_gen_service import (
    DALLE2_EDIT_COST_256,
    DALLE2_EDIT_COST_512,
    DALLE2_EDIT_COST_1024,
    DallE3ImageService,
    ImageEditRequest,
)


class TestSketchStylePreset:
    def test_sketch_preset_exists(self):
        assert "sketch" in _STYLE_FILTER_PRESETS

    def test_sketch_preset_has_edgedetect(self):
        assert "edgedetect" in _STYLE_FILTER_PRESETS["sketch"]

    def test_sketch_preset_has_negate(self):
        assert "negate" in _STYLE_FILTER_PRESETS["sketch"]

    def test_all_presets_are_strings(self):
        for name, vf in _STYLE_FILTER_PRESETS.items():
            assert isinstance(vf, str), f"Preset {name} is not a string"

    def test_preset_count(self):
        assert len(_STYLE_FILTER_PRESETS) >= 4


class TestImageEditRequest:
    def test_defaults(self):
        req = ImageEditRequest(image_path=Path("test.png"), prompt="make it sketch")
        assert req.model == "dall-e-2"
        assert req.size == "1024x1024"
        assert req.mask_path is None

    def test_custom_fields(self):
        req = ImageEditRequest(
            image_path=Path("img.png"),
            prompt="edit this",
            size="512x512",
            mask_path=Path("mask.png"),
        )
        assert req.size == "512x512"
        assert req.mask_path == Path("mask.png")

    def test_image_path_is_path(self):
        req = ImageEditRequest(image_path=Path("/tmp/test.png"), prompt="test")
        assert isinstance(req.image_path, Path)


class TestDallE2EditCostConstants:
    def test_cost_256(self):
        assert DALLE2_EDIT_COST_256 == 0.016

    def test_cost_512(self):
        assert DALLE2_EDIT_COST_512 == 0.018

    def test_cost_1024(self):
        assert DALLE2_EDIT_COST_1024 == 0.020


class TestDallE3EditImage:
    @patch("openai.OpenAI")
    def test_edit_image_calls_api(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        mock_response = MagicMock()
        mock_response.model_dump.return_value = {
            "data": [{"url": "https://example.com/edited.png", "revised_prompt": "edited"}]
        }
        mock_client.images.edit.return_value = mock_response

        service = DallE3ImageService(api_key="test-key")

        with patch("builtins.open", MagicMock()):
            req = ImageEditRequest(image_path=Path("test.png"), prompt="make sketch")
            result = service.edit_image(req)

        assert result.image_url == "https://example.com/edited.png"
        assert result.cost_usd > 0
        assert result.model == "dall-e-2"
        mock_client.images.edit.assert_called_once()

    @patch("openai.OpenAI")
    def test_edit_image_with_mask(self, mock_openai_cls):
        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client

        mock_response = MagicMock()
        mock_response.model_dump.return_value = {
            "data": [{"url": "https://example.com/edited.png", "revised_prompt": "edited"}]
        }
        mock_client.images.edit.return_value = mock_response

        service = DallE3ImageService(api_key="test-key")

        with patch("builtins.open", MagicMock()):
            req = ImageEditRequest(
                image_path=Path("test.png"),
                prompt="inpaint this",
                mask_path=Path("mask.png"),
            )
            result = service.edit_image(req)

        assert result.image_url == "https://example.com/edited.png"
        call_kwargs = mock_client.images.edit.call_args
        assert "mask" in call_kwargs.kwargs or (
            call_kwargs[1] and "mask" in call_kwargs[1]
        )

    def test_edit_cost_computation(self):
        service = DallE3ImageService(api_key="test-key")
        assert service._compute_edit_cost("1024x1024") == 0.020
        assert service._compute_edit_cost("512x512") == 0.018
        assert service._compute_edit_cost("256x256") == 0.016


class TestDalleEditStyleProvider:
    """Test that frame extractor can branch to DALL-E edit style."""

    @patch("btcedu.services.image_gen_service.DallE3ImageService.download_image")
    @patch("openai.OpenAI")
    def test_dalle_edit_helper_calls_service(self, mock_openai_cls, mock_download):
        from btcedu.config import Settings
        from btcedu.core.frame_extractor import _apply_dalle_edit_style

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_response = MagicMock()
        mock_response.model_dump.return_value = {
            "data": [{"url": "https://example.com/edited.png", "revised_prompt": "edited"}]
        }
        mock_client.images.edit.return_value = mock_response

        settings = Settings(openai_api_key="test-key")
        with patch("builtins.open", MagicMock()):
            result = _apply_dalle_edit_style("input.png", "output.png", settings)
        assert result == "output.png"
        mock_client.images.edit.assert_called_once()

    @patch("btcedu.services.image_gen_service.DallE3ImageService.download_image")
    @patch("openai.OpenAI")
    def test_dalle_edit_helper_uses_correct_prompt(self, mock_openai_cls, mock_download):
        from btcedu.config import Settings
        from btcedu.core.frame_extractor import _apply_dalle_edit_style

        mock_client = MagicMock()
        mock_openai_cls.return_value = mock_client
        mock_response = MagicMock()
        mock_response.model_dump.return_value = {
            "data": [{"url": "https://example.com/edited.png", "revised_prompt": "edited"}]
        }
        mock_client.images.edit.return_value = mock_response

        settings = Settings(openai_api_key="test-key")
        with patch("builtins.open", MagicMock()):
            _apply_dalle_edit_style("input.png", "output.png", settings)

        # The function constructs an ImageEditRequest with sketch prompt
        call_kwargs = mock_client.images.edit.call_args
        prompt_arg = call_kwargs.kwargs.get("prompt", call_kwargs[1].get("prompt", ""))
        assert "sketch" in prompt_arg.lower()

    def test_style_provider_setting_default(self):
        from btcedu.config import Settings

        settings = Settings()
        assert settings.frame_extract_style_provider == "ffmpeg"

    def test_style_provider_setting_dalle_edit(self):
        from btcedu.config import Settings

        settings = Settings(frame_extract_style_provider="dalle_edit")
        assert settings.frame_extract_style_provider == "dalle_edit"
