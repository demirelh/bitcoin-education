"""Tests for Pexels stock photo API service."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.services.pexels_service import PexelsPhoto, PexelsSearchResult, PexelsService

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def sample_response():
    return json.loads((FIXTURES_DIR / "pexels_search_response.json").read_text())


@pytest.fixture
def service():
    return PexelsService(api_key="test-api-key-123")


@pytest.fixture
def sample_photo():
    return PexelsPhoto(
        id=12345,
        width=5000,
        height=3333,
        url="https://www.pexels.com/photo/golden-bitcoin-12345/",
        photographer="Test Photographer",
        photographer_url="https://www.pexels.com/@test-photographer",
        src_original="https://images.pexels.com/photos/12345/original.jpeg",
        src_landscape="https://images.pexels.com/photos/12345/landscape.jpeg",
        src_large2x="https://images.pexels.com/photos/12345/large2x.jpeg",
        alt="Golden Bitcoin coin on dark background",
        avg_color="#2D2D2D",
    )


class TestPexelsServiceInit:
    def test_requires_api_key(self):
        with pytest.raises(ValueError, match="API key is required"):
            PexelsService(api_key="")

    def test_accepts_valid_key(self):
        svc = PexelsService(api_key="valid-key")
        assert svc.api_key == "valid-key"


class TestPexelsSearch:
    @patch("btcedu.services.pexels_service.requests.request")
    def test_search_returns_photos(self, mock_request, service, sample_response):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_response
        mock_request.return_value = mock_resp

        result = service.search("bitcoin cryptocurrency")

        assert isinstance(result, PexelsSearchResult)
        assert result.query == "bitcoin cryptocurrency"
        assert result.total_results == 42
        assert len(result.photos) == 3
        assert result.photos[0].id == 12345
        assert result.photos[0].photographer == "Test Photographer"

    @patch("btcedu.services.pexels_service.requests.request")
    def test_search_passes_orientation(self, mock_request, service, sample_response):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_response
        mock_request.return_value = mock_resp

        service.search("test", orientation="portrait")

        call_kwargs = mock_request.call_args
        assert call_kwargs.kwargs["params"]["orientation"] == "portrait"

    @patch("btcedu.services.pexels_service.requests.request")
    def test_search_auth_header(self, mock_request, service, sample_response):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_response
        mock_request.return_value = mock_resp

        service.search("test")

        call_kwargs = mock_request.call_args
        assert call_kwargs.kwargs["headers"]["Authorization"] == "test-api-key-123"

    @patch("btcedu.services.pexels_service.time.sleep")
    @patch("btcedu.services.pexels_service.requests.request")
    def test_search_rate_limit_retry(self, mock_request, mock_sleep, service, sample_response):
        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.text = "Rate limited"

        success = MagicMock()
        success.status_code = 200
        success.json.return_value = sample_response

        mock_request.side_effect = [rate_limited, success]

        result = service.search("test")
        assert len(result.photos) == 3
        assert mock_sleep.call_count == 1

    @patch("btcedu.services.pexels_service.requests.request")
    def test_search_api_error_raises(self, mock_request, service):
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        mock_request.return_value = mock_resp

        with pytest.raises(RuntimeError, match="Pexels API error 500"):
            service.search("test")

    @patch("btcedu.services.pexels_service.time.sleep")
    @patch("btcedu.services.pexels_service.requests.request")
    def test_search_rate_limit_exhausted(self, mock_request, mock_sleep, service):
        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.text = "Rate limited"
        mock_request.return_value = rate_limited

        with pytest.raises(RuntimeError, match="rate limit exceeded"):
            service.search("test")

    @patch("btcedu.services.pexels_service.requests.request")
    def test_search_per_page_param(self, mock_request, service, sample_response):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_response
        mock_request.return_value = mock_resp

        service.search("test", per_page=3)
        call_kwargs = mock_request.call_args
        assert call_kwargs.kwargs["params"]["per_page"] == 3

    @patch("btcedu.services.pexels_service.requests.request")
    def test_search_photo_fields(self, mock_request, service, sample_response):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = sample_response
        mock_request.return_value = mock_resp

        result = service.search("test")
        photo = result.photos[0]

        assert photo.id == 12345
        assert photo.width == 5000
        assert photo.height == 3333
        assert "pexels.com" in photo.url
        assert photo.photographer == "Test Photographer"
        assert photo.alt == "Golden Bitcoin coin on dark background"
        assert photo.avg_color == "#2D2D2D"
        assert "pexels.com" in photo.src_original
        assert "pexels.com" in photo.src_large2x
        assert "pexels.com" in photo.src_landscape


class TestPexelsDownload:
    @patch("btcedu.services.pexels_service.requests.get")
    def test_download_photo_saves_file(self, mock_get, service, sample_photo, tmp_path):
        mock_resp = MagicMock()
        mock_resp.content = b"fake-jpeg-data-for-testing"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        target = tmp_path / "test_photo.jpg"
        result = service.download_photo(sample_photo, target)

        assert result == target
        assert target.exists()
        assert target.read_bytes() == b"fake-jpeg-data-for-testing"

    @patch("btcedu.services.pexels_service.requests.get")
    def test_download_creates_parent_dirs(self, mock_get, service, sample_photo, tmp_path):
        mock_resp = MagicMock()
        mock_resp.content = b"data"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        target = tmp_path / "subdir" / "nested" / "photo.jpg"
        service.download_photo(sample_photo, target)

        assert target.exists()

    @patch("btcedu.services.pexels_service.requests.get")
    def test_download_uses_correct_size_url(self, mock_get, service, sample_photo, tmp_path):
        mock_resp = MagicMock()
        mock_resp.content = b"data"
        mock_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_resp

        # Test landscape size
        target = tmp_path / "landscape.jpg"
        service.download_photo(sample_photo, target, size="landscape")
        mock_get.assert_called_with(sample_photo.src_landscape, timeout=60)

        # Test original size
        target2 = tmp_path / "original.jpg"
        service.download_photo(sample_photo, target2, size="original")
        mock_get.assert_called_with(sample_photo.src_original, timeout=60)


class TestRateLimit:
    def test_rate_limit_tracking(self):
        service = PexelsService(api_key="test", requests_per_hour=180)
        assert len(service._request_timestamps) == 0

        service._record_request()
        assert len(service._request_timestamps) == 1

        service._record_request()
        assert len(service._request_timestamps) == 2
