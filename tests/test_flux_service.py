from pathlib import Path
from unittest.mock import patch

import requests

from btcedu.services.flux_service import FluxImageService


def _response(status_code: int, content: bytes = b"") -> requests.Response:
    response = requests.Response()
    response.status_code = status_code
    response._content = content
    response.url = "https://v3b.fal.media/files/generated.png"
    return response


def test_download_image_retries_transient_cdn_failure(tmp_path: Path):
    target_path = tmp_path / "image.png"

    with (
        patch(
            "btcedu.services.flux_service.requests.get",
            side_effect=[_response(500), _response(200, b"image-data")],
        ) as mock_get,
        patch("btcedu.services.retry.time.sleep"),
    ):
        result = FluxImageService.download_image(
            "https://v3b.fal.media/files/generated.png",
            target_path,
        )

    assert result == target_path
    assert target_path.read_bytes() == b"image-data"
    assert mock_get.call_count == 2


def test_ideogram_download_image_retries_transient_cdn_failure(tmp_path: Path):
    """Ideogram downloads the same way Flux does and must survive the same 500."""
    from btcedu.services.ideogram_service import IdeogramImageService

    target_path = tmp_path / "image.png"

    with (
        patch(
            "btcedu.services.ideogram_service.requests.get",
            side_effect=[_response(500), _response(200, b"image-data")],
        ) as mock_get,
        patch("btcedu.services.retry.time.sleep"),
    ):
        result = IdeogramImageService.download_image(
            "https://ideogram.ai/files/generated.png",
            target_path,
        )

    assert result == target_path
    assert target_path.read_bytes() == b"image-data"
    assert mock_get.call_count == 2
