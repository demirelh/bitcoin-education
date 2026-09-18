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


def test_flux_does_not_retry_forbidden_credentials():
    """A 403 fails identically on every attempt — fail fast and say why."""
    import pytest

    service = FluxImageService(api_key="bad-key")

    with (
        patch(
            "btcedu.services.flux_service.requests.post",
            return_value=_response(403),
        ) as mock_post,
        patch("btcedu.services.flux_service.time.sleep"),
        pytest.raises(RuntimeError, match="auth_error"),
    ):
        service._call_with_retry("https://fal.run/fal-ai/flux/dev", {}, {})

    assert mock_post.call_count == 1


def test_ideogram_does_not_retry_payment_required():
    """402 means the account is out of credits, not a transient server error."""
    import pytest

    from btcedu.services.ideogram_service import IdeogramImageService

    service = IdeogramImageService(api_key="spent-key")

    with (
        patch(
            "btcedu.services.ideogram_service.requests.post",
            return_value=_response(402),
        ) as mock_post,
        patch("btcedu.services.ideogram_service.time.sleep"),
        pytest.raises(RuntimeError, match="quota_exhausted"),
    ):
        service._call_with_retry("https://api.ideogram.ai/generate", {}, {})

    assert mock_post.call_count == 1


def test_flux_billing_lock_is_reported_as_quota_with_provider_detail():
    """fal.ai answers a billing lock with a 403 and the reason only in the body.

    Without the body the operator is told to check the API key, which is the
    wrong fix; the key is fine and the account needs a top-up.
    """
    import pytest

    service = FluxImageService(api_key="locked-account-key")
    locked = _response(403, b'{"detail":"User is locked. Reason: TOP_UP."}')

    with (
        patch("btcedu.services.flux_service.requests.post", return_value=locked) as mock_post,
        patch("btcedu.services.flux_service.time.sleep"),
        pytest.raises(RuntimeError) as excinfo,
    ):
        service._call_with_retry("https://fal.run/fal-ai/flux/dev", {}, {})

    assert mock_post.call_count == 1
    assert "quota_exhausted" in str(excinfo.value)
    assert "TOP_UP" in str(excinfo.value)
