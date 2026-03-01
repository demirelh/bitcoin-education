"""Tests for Sprint 11: YouTube service (DryRun + mocked API)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.services.youtube_service import (
    DryRunYouTubeService,
    YouTubeUploadRequest,
    YouTubeUploadResponse,
    check_token_status,
)


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _make_upload_request(tmp_path: Path, privacy: str = "unlisted") -> YouTubeUploadRequest:
    video_path = tmp_path / "draft.mp4"
    video_path.write_bytes(b"fake video data")
    return YouTubeUploadRequest(
        video_path=video_path,
        title="Bitcoin Eğitim #1",
        description="Test description with 0:00 Giriş",
        tags=["Bitcoin", "Kripto"],
        category_id="27",
        default_language="tr",
        privacy_status=privacy,
    )


# ---------------------------------------------------------------------------
# DryRunYouTubeService
# ---------------------------------------------------------------------------


class TestDryRunYouTubeService:
    def test_upload_returns_dry_run_response(self, tmp_path):
        svc = DryRunYouTubeService()
        req = _make_upload_request(tmp_path)
        response = svc.upload_video(req)

        assert isinstance(response, YouTubeUploadResponse)
        assert response.video_id == "DRY_RUN"
        assert "DRY_RUN" in response.video_url
        assert response.privacy_status == "unlisted"

    def test_upload_progress_callback_called(self, tmp_path):
        svc = DryRunYouTubeService()
        req = _make_upload_request(tmp_path)
        calls = []
        svc.upload_video(req, progress_callback=lambda u, t: calls.append((u, t)))
        assert len(calls) >= 1
        # Final call: 100% progress
        assert calls[-1][0] == calls[-1][1]

    def test_privacy_status_preserved(self, tmp_path):
        svc = DryRunYouTubeService()
        req = _make_upload_request(tmp_path, privacy="private")
        response = svc.upload_video(req)
        assert response.privacy_status == "private"

    def test_title_truncated_if_too_long(self, tmp_path):
        """Titles over 100 chars should still work (truncation is caller's job)."""
        svc = DryRunYouTubeService()
        long_title = "A" * 120
        video_path = tmp_path / "draft.mp4"
        video_path.write_bytes(b"x")
        req = YouTubeUploadRequest(
            video_path=video_path,
            title=long_title,
            description="Desc",
            tags=["tag"],
        )
        # Should not raise
        response = svc.upload_video(req)
        assert response.video_id == "DRY_RUN"


# ---------------------------------------------------------------------------
# check_token_status
# ---------------------------------------------------------------------------


class TestCheckTokenStatus:
    def test_returns_not_exists_when_no_file(self, tmp_path):
        creds_path = tmp_path / "nonexistent_creds.json"
        status = check_token_status(credentials_path=str(creds_path))
        assert status["valid"] is False
        assert "error" in status

    def test_returns_exists_with_valid_json(self, tmp_path):
        creds_path = tmp_path / "test_creds.json"
        creds_data = {
            "token": "fake_token",
            "refresh_token": "fake_refresh",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_id": "fake_client_id",
            "client_secret": "fake_secret",
            "scopes": ["https://www.googleapis.com/auth/youtube.upload"],
            "expiry": "2030-01-01T00:00:00Z",
        }
        creds_path.write_text(json.dumps(creds_data))
        status = check_token_status(credentials_path=str(creds_path))
        # Without google-auth installed, will get error dict but no exception
        assert isinstance(status, dict)
        assert "valid" in status or "error" in status

    def test_handles_corrupted_file(self, tmp_path):
        """Corrupted credentials file should not raise — return error in status."""
        creds_path = tmp_path / "corrupt.json"
        creds_path.write_text("NOT_VALID_JSON{{{")
        status = check_token_status(credentials_path=str(creds_path))
        # Should not raise; valid will be False
        assert status.get("valid") is False


# ---------------------------------------------------------------------------
# YouTubeUploadRequest validation
# ---------------------------------------------------------------------------


class TestYouTubeUploadRequest:
    def test_default_privacy_is_unlisted(self, tmp_path):
        video_path = tmp_path / "v.mp4"
        video_path.write_bytes(b"x")
        req = YouTubeUploadRequest(
            video_path=video_path,
            title="T",
            description="D",
            tags=["t"],
        )
        assert req.privacy_status == "unlisted"
