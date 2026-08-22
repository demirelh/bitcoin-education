"""Tests for Sprint 11: YouTube service (DryRun + mocked API)."""

import json
import os
import stat
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.config import Settings
from btcedu.services.youtube_service import (
    DryRunYouTubeService,
    YouTubeAuthError,
    YouTubeDataAPIService,
    YouTubeUploadRequest,
    YouTubeUploadResponse,
    _ensure_private_credentials_file,
    _is_quota_error,
    _write_private_credentials_file,
    check_token_status,
    estimate_upload_quota,
    resolve_youtube_target,
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

    def test_status_repairs_credentials_mode(self, tmp_path):
        creds_path = tmp_path / "credentials.json"
        creds_path.write_text("{}")
        creds_path.chmod(0o644)

        check_token_status(credentials_path=str(creds_path))

        assert stat.S_IMODE(creds_path.stat().st_mode) == 0o600


class TestCredentialFilePermissions:
    def test_secure_writer_creates_mode_0600(self, tmp_path):
        path = tmp_path / "youtube" / "credentials.json"

        _write_private_credentials_file(path, '{"token": "secret"}')

        assert path.read_text() == '{"token": "secret"}'
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_secure_writer_maintains_mode_0600_on_replace(self, tmp_path):
        path = tmp_path / "credentials.json"
        path.write_text("old")
        path.chmod(0o644)

        _write_private_credentials_file(path, "new")

        assert path.read_text() == "new"
        assert stat.S_IMODE(path.stat().st_mode) == 0o600

    def test_secure_writer_preserves_original_on_replace_failure(self, tmp_path):
        path = tmp_path / "credentials.json"
        path.write_text("old")
        path.chmod(0o600)

        with (
            patch(
                "btcedu.services.youtube_service.os.replace",
                side_effect=OSError("disk failure"),
            ),
            pytest.raises(YouTubeAuthError, match="securely write"),
        ):
            _write_private_credentials_file(path, "new")

        assert path.read_text() == "old"
        assert not list(tmp_path.glob(".credentials.json.*.tmp"))

    def test_secure_reader_rejects_symlink(self, tmp_path):
        real_path = tmp_path / "real.json"
        link_path = tmp_path / "credentials.json"
        real_path.write_text("{}")
        os.symlink(real_path, link_path)

        with pytest.raises(YouTubeAuthError, match="securely open"):
            _ensure_private_credentials_file(link_path)


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


class TestYouTubeTargets:
    def test_test_target_uses_private_separate_credentials(self, tmp_path):
        settings = Settings(
            youtube_test_client_secrets_path=str(tmp_path / "test-client.json"),
            youtube_test_credentials_path=str(tmp_path / "test-token.json"),
            youtube_test_channel_id="UC_TEST",
        )

        target = resolve_youtube_target(settings, {"publish_target": "test"})

        assert target.name == "test"
        assert target.default_privacy == "private"
        assert target.credentials_path.endswith("test-token.json")
        assert target.expected_channel_id == "UC_TEST"

    def test_production_target_is_independent(self, tmp_path):
        settings = Settings(
            youtube_production_client_secrets_path=str(tmp_path / "prod-client.json"),
            youtube_production_credentials_path=str(tmp_path / "prod-token.json"),
            youtube_production_channel_id="UC_PROD",
        )

        target = resolve_youtube_target(settings, target_override="production")

        assert target.name == "production"
        assert target.default_privacy == "unlisted"
        assert target.credentials_path.endswith("prod-token.json")
        assert target.expected_channel_id == "UC_PROD"

    def test_rejects_unknown_target(self):
        with pytest.raises(ValueError, match="Unsupported YouTube target"):
            resolve_youtube_target(Settings(), target_override="staging")


class TestQuotaAccounting:
    def test_estimate_uses_current_separate_upload_bucket(self, tmp_path):
        req = _make_upload_request(tmp_path)
        req.thumbnail_path = tmp_path / "thumb.png"
        req.thumbnail_path.write_bytes(b"png")
        req.subtitle_path = tmp_path / "captions.srt"
        req.subtitle_path.write_text("1\n00:00:00,000 --> 00:00:01,000\nMerhaba")

        quota = estimate_upload_quota(req)

        assert quota.upload_calls == 1
        assert quota.general_units == 451
        assert quota.breakdown == {
            "channels.list": 1,
            "videos.insert": 1,
            "thumbnails.set": 50,
            "captions.insert": 400,
        }

    def test_only_authoritative_403_reasons_count_as_quota(self):
        quota_error = MagicMock(
            content=json.dumps(
                {"error": {"errors": [{"reason": "quotaExceeded"}]}}
            ).encode()
        )
        forbidden = MagicMock(
            content=json.dumps(
                {"error": {"errors": [{"reason": "forbidden"}]}}
            ).encode()
        )

        assert _is_quota_error(quota_error) is True
        assert _is_quota_error(forbidden) is False


class TestChannelVerification:
    def test_rejects_missing_expected_channel(self):
        service = YouTubeDataAPIService("credentials.json", target_name="test")

        with pytest.raises(YouTubeAuthError, match="YOUTUBE_TEST_CHANNEL_ID"):
            service._verify_channel(MagicMock())

    def test_rejects_wrong_authenticated_channel(self):
        youtube = MagicMock()
        youtube.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "UC_WRONG"}]
        }
        service = YouTubeDataAPIService(
            "credentials.json",
            expected_channel_id="UC_EXPECTED",
            target_name="production",
        )

        with pytest.raises(YouTubeAuthError, match="channel mismatch"):
            service._verify_channel(youtube)

    def test_accepts_configured_channel(self):
        youtube = MagicMock()
        youtube.channels.return_value.list.return_value.execute.return_value = {
            "items": [{"id": "UC_EXPECTED"}]
        }
        service = YouTubeDataAPIService(
            "credentials.json",
            expected_channel_id="UC_EXPECTED",
            target_name="production",
        )

        assert service._verify_channel(youtube) == "UC_EXPECTED"
