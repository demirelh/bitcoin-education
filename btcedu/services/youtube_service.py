"""YouTube Data API v3 service: OAuth2, resumable upload, metadata."""

import json
import logging
import os
import stat
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

logger = logging.getLogger(__name__)

# Official quota calculator, verified 2026-08-22:
# https://developers.google.com/youtube/v3/determine_quota_cost
# videos.insert and search.list moved to their own granular quota buckets on
# 2026-06-01. Do not add videos.insert to the general-unit total.
YOUTUBE_VIDEOS_INSERT_CALLS = 1
YOUTUBE_VIDEOS_INSERT_DAILY_LIMIT = 100
YOUTUBE_CHANNELS_LIST_QUOTA_UNITS = 1
YOUTUBE_THUMBNAILS_SET_QUOTA_UNITS = 50
YOUTUBE_CAPTIONS_INSERT_QUOTA_UNITS = 400

# YouTube limits
YOUTUBE_MAX_TAG_CHARS = 500
YOUTUBE_MAX_TITLE_CHARS = 100
YOUTUBE_MAX_DESCRIPTION_CHARS = 5000


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------


@dataclass
class YouTubeUploadRequest:
    """Request for video upload to YouTube."""

    video_path: Path
    title: str
    description: str
    tags: list[str]
    category_id: str = "27"  # Education
    default_language: str = "tr"
    privacy_status: str = "unlisted"
    thumbnail_path: Path | None = None
    subtitle_path: Path | None = None


@dataclass(frozen=True)
class YouTubeTargetConfig:
    """Non-secret routing configuration for one YouTube destination."""

    name: str
    client_secrets_path: str
    credentials_path: str
    expected_channel_id: str
    default_privacy: str


@dataclass
class YouTubeQuotaUsage:
    """Quota charged by an upload, split by Google's current quota buckets."""

    upload_calls: int = 0
    general_units: int = 0
    breakdown: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "upload_calls": self.upload_calls,
            "upload_daily_limit": YOUTUBE_VIDEOS_INSERT_DAILY_LIMIT,
            "general_units": self.general_units,
            "breakdown": dict(self.breakdown),
        }


@dataclass
class YouTubeUploadResponse:
    """Response from a successful YouTube upload."""

    video_id: str
    video_url: str  # https://youtu.be/{video_id}
    status: str = "uploaded"
    privacy_status: str = "unlisted"
    channel_id: str | None = None
    quota_usage: YouTubeQuotaUsage = field(default_factory=YouTubeQuotaUsage)


class YouTubeAuthError(Exception):
    """OAuth2 authentication / token error."""


class YouTubeUploadError(Exception):
    """Upload or API error (non-quota)."""


class YouTubeUploadIndeterminateError(YouTubeUploadError):
    """Upload may have been accepted, so an automatic retry is unsafe."""


class YouTubeQuotaError(Exception):
    """Quota exceeded (HTTP 403 quotaExceeded)."""


def _ensure_private_credentials_file(path: Path) -> None:
    """Require an existing credential file to be a regular mode-0600 file."""
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise YouTubeAuthError(f"Could not securely open credentials file {path}: {exc}") from exc
    try:
        file_stat = os.fstat(fd)
        if not stat.S_ISREG(file_stat.st_mode):
            raise YouTubeAuthError(f"Credentials path is not a regular file: {path}")
        os.fchmod(fd, 0o600)
        mode = stat.S_IMODE(os.fstat(fd).st_mode)
        if mode != 0o600:
            raise YouTubeAuthError(
                f"Credentials file permissions are {mode:o}, expected 600: {path}"
            )
    except OSError as exc:
        raise YouTubeAuthError(
            f"Could not enforce mode 0600 on credentials file {path}: {exc}"
        ) from exc
    finally:
        os.close(fd)


def _write_private_credentials_file(path: Path, content: str) -> None:
    """Atomically write OAuth credentials without ever exposing broad modes."""
    temp_path = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    fd: int | None = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() or path.is_symlink():
            _ensure_private_credentials_file(path)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temp_path, flags, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o600)
        os.replace(temp_path, path)
        _ensure_private_credentials_file(path)
    except YouTubeAuthError:
        raise
    except OSError as exc:
        raise YouTubeAuthError(f"Could not securely write credentials file {path}: {exc}") from exc
    finally:
        if fd is not None:
            os.close(fd)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            logger.error("Could not remove incomplete credentials file %s", temp_path)


def resolve_youtube_target(
    settings,
    youtube_config: dict | None = None,
    target_override: str | None = None,
) -> YouTubeTargetConfig:
    """Resolve test/production routing without putting credentials in a profile."""
    youtube_config = youtube_config or {}
    target = (
        target_override
        or youtube_config.get("publish_target")
        or getattr(settings, "youtube_default_target", "test")
    )
    if target not in {"test", "production"}:
        raise ValueError(f"Unsupported YouTube target: {target!r}")

    target_profile = (youtube_config.get("targets") or {}).get(target) or {}
    default_privacy = target_profile.get("default_privacy") or getattr(
        settings,
        f"youtube_{target}_default_privacy",
        "private" if target == "test" else "unlisted",
    )
    if default_privacy not in {"private", "unlisted", "public"}:
        raise ValueError(
            f"Unsupported privacy status {default_privacy!r} for YouTube target {target!r}"
        )

    return YouTubeTargetConfig(
        name=target,
        client_secrets_path=getattr(settings, f"youtube_{target}_client_secrets_path"),
        credentials_path=getattr(settings, f"youtube_{target}_credentials_path"),
        expected_channel_id=getattr(settings, f"youtube_{target}_channel_id", "").strip(),
        default_privacy=default_privacy,
    )


def estimate_upload_quota(req: YouTubeUploadRequest) -> YouTubeQuotaUsage:
    """Estimate the calls attempted by ``upload_video`` for current API costs."""
    breakdown = {
        "channels.list": YOUTUBE_CHANNELS_LIST_QUOTA_UNITS,
        "videos.insert": YOUTUBE_VIDEOS_INSERT_CALLS,
    }
    general_units = YOUTUBE_CHANNELS_LIST_QUOTA_UNITS
    if req.thumbnail_path and req.thumbnail_path.exists():
        breakdown["thumbnails.set"] = YOUTUBE_THUMBNAILS_SET_QUOTA_UNITS
        general_units += YOUTUBE_THUMBNAILS_SET_QUOTA_UNITS
    if req.subtitle_path and req.subtitle_path.exists():
        breakdown["captions.insert"] = YOUTUBE_CAPTIONS_INSERT_QUOTA_UNITS
        general_units += YOUTUBE_CAPTIONS_INSERT_QUOTA_UNITS
    return YouTubeQuotaUsage(
        upload_calls=YOUTUBE_VIDEOS_INSERT_CALLS,
        general_units=general_units,
        breakdown=breakdown,
    )


def _is_quota_error(exc) -> bool:
    """Return whether an HttpError carries an authoritative quota reason."""
    raw = getattr(exc, "content", b"")
    try:
        data = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    except (json.JSONDecodeError, TypeError, UnicodeDecodeError):
        return False
    if not isinstance(data, dict):
        return False
    reasons = {
        str(error.get("reason", ""))
        for error in ((data.get("error") or {}).get("errors") or [])
        if isinstance(error, dict)
    }
    return bool(
        reasons
        & {
            "dailyLimitExceeded",
            "quotaExceeded",
            "rateLimitExceeded",
            "uploadLimitExceeded",
            "userRateLimitExceeded",
        }
    )


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class YouTubeService(Protocol):
    """Interface for YouTube upload services."""

    def upload_video(
        self,
        req: YouTubeUploadRequest,
        progress_callback: Callable[[int, int], None] | None = None,
        accepted_callback: Callable[[str], None] | None = None,
    ) -> YouTubeUploadResponse: ...


# ---------------------------------------------------------------------------
# Dry-run implementation
# ---------------------------------------------------------------------------


class DryRunYouTubeService:
    """YouTubeService that never calls the real API — for dry-run and tests."""

    def upload_video(
        self,
        req: YouTubeUploadRequest,
        progress_callback: Callable[[int, int], None] | None = None,
        accepted_callback: Callable[[str], None] | None = None,
    ) -> YouTubeUploadResponse:
        logger.info(
            "[DRY RUN] Would upload '%s' to YouTube as '%s' (privacy=%s)",
            req.video_path,
            req.title,
            req.privacy_status,
        )
        if progress_callback:
            progress_callback(0, 1)
            progress_callback(1, 1)
        return YouTubeUploadResponse(
            video_id="DRY_RUN",
            video_url="https://youtu.be/DRY_RUN",
            status="dry_run",
            privacy_status=req.privacy_status,
        )


# ---------------------------------------------------------------------------
# Real YouTube Data API v3 implementation
# ---------------------------------------------------------------------------


class YouTubeDataAPIService:
    """YouTube Data API v3 wrapper using OAuth2 credentials.

    Credentials file must be pre-populated via ``authenticate()`` before
    instantiating this class, or by passing ``credentials`` directly in tests.
    """

    def __init__(
        self,
        credentials_path: str,
        chunk_size_bytes: int = 10 * 1024 * 1024,
        max_retries: int = 3,
        expected_channel_id: str = "",
        target_name: str = "test",
        *,
        _credentials=None,  # Injected in tests
    ):
        self._credentials_path = credentials_path
        self._chunk_size = chunk_size_bytes
        self._max_retries = max_retries
        self._expected_channel_id = expected_channel_id.strip()
        self._target_name = target_name
        self._credentials = _credentials  # Optional injected credentials

    # ------------------------------------------------------------------
    # OAuth2 helpers
    # ------------------------------------------------------------------

    def _load_credentials(self):
        """Load and refresh OAuth2 credentials from file."""
        if self._credentials is not None:
            return self._credentials  # Injected (test mode)

        try:
            from google.auth.transport.requests import Request
            from google.oauth2.credentials import Credentials
        except ImportError as exc:
            raise YouTubeAuthError(
                "google-auth not installed. Run: "
                "pip install google-auth google-auth-httplib2 google-auth-oauthlib"
            ) from exc

        creds_path = Path(self._credentials_path)
        if not creds_path.exists():
            raise YouTubeAuthError(
                f"YouTube credentials not found: {creds_path}. "
                "Run 'btcedu youtube-auth' to set up OAuth2."
            )
        _ensure_private_credentials_file(creds_path)

        try:
            creds = Credentials.from_authorized_user_file(str(creds_path))
        except Exception as exc:
            raise YouTubeAuthError(f"Could not load credentials: {exc}") from exc

        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    _write_private_credentials_file(creds_path, creds.to_json())
                except Exception as exc:
                    if isinstance(exc, YouTubeAuthError):
                        raise
                    raise YouTubeAuthError(f"Token refresh failed: {exc}") from exc
            else:
                raise YouTubeAuthError(
                    "Credentials invalid/expired and cannot refresh. "
                    "Run 'btcedu youtube-auth' to re-authenticate."
                )
        return creds

    def _build_client(self):
        """Build authenticated YouTube API client."""
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise YouTubeAuthError(
                "google-api-python-client not installed. Run: pip install google-api-python-client"
            ) from exc

        creds = self._load_credentials()
        return build("youtube", "v3", credentials=creds)

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    def upload_video(
        self,
        req: YouTubeUploadRequest,
        progress_callback: Callable[[int, int], None] | None = None,
        accepted_callback: Callable[[str], None] | None = None,
    ) -> YouTubeUploadResponse:
        """Upload video to YouTube with resumable upload.

        Args:
            req: Upload request with video path and metadata.
            progress_callback: Called with (bytes_uploaded, total_bytes).

        Returns:
            YouTubeUploadResponse with video_id and url.

        Raises:
            YouTubeAuthError: OAuth2 errors.
            YouTubeQuotaError: Quota exceeded (HTTP 403).
            YouTubeUploadError: Other API errors.
        """
        try:
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            raise YouTubeUploadError("google-api-python-client not installed.") from exc

        if not req.video_path.exists():
            raise YouTubeUploadError(f"Video file not found: {req.video_path}")

        youtube = self._build_client()
        channel_id = self._verify_channel(youtube)
        file_size = req.video_path.stat().st_size
        quota_usage = estimate_upload_quota(req)

        body = {
            "snippet": {
                "title": req.title[:YOUTUBE_MAX_TITLE_CHARS],
                "description": req.description[:YOUTUBE_MAX_DESCRIPTION_CHARS],
                "tags": req.tags,
                "categoryId": req.category_id,
                "defaultLanguage": req.default_language,
            },
            "status": {
                "privacyStatus": req.privacy_status,
            },
        }

        media = MediaFileUpload(
            str(req.video_path),
            mimetype="video/mp4",
            resumable=True,
            chunksize=self._chunk_size,
        )

        insert_request = youtube.videos().insert(
            part="snippet,status",
            body=body,
            media_body=media,
        )

        video_id = self._execute_upload(
            insert_request,
            file_size=file_size,
            progress_callback=progress_callback,
        )
        if not video_id:
            raise YouTubeUploadError("YouTube upload completed without returning a video ID")
        if accepted_callback:
            accepted_callback(video_id)

        # Upload thumbnail if available
        if req.thumbnail_path and req.thumbnail_path.exists():
            self._upload_thumbnail(youtube, video_id, req.thumbnail_path)

        if req.subtitle_path and req.subtitle_path.exists():
            self._upload_captions(
                youtube, video_id, req.subtitle_path, language=req.default_language
            )

        return YouTubeUploadResponse(
            video_id=video_id,
            video_url=f"https://youtu.be/{video_id}",
            status="uploaded",
            privacy_status=req.privacy_status,
            channel_id=channel_id,
            quota_usage=quota_usage,
        )

    def _verify_channel(self, youtube) -> str:
        """Fail before upload if OAuth credentials point at the wrong channel."""
        if not self._expected_channel_id:
            env_name = f"YOUTUBE_{self._target_name.upper()}_CHANNEL_ID"
            raise YouTubeAuthError(
                f"YouTube {self._target_name} target has no expected channel ID. "
                f"Set {env_name} before uploading."
            )
        try:
            response = youtube.channels().list(part="id", mine=True).execute()
        except Exception as exc:
            if getattr(exc, "status_code", None) == 403 and _is_quota_error(exc):
                raise YouTubeQuotaError(
                    f"YouTube API quota exceeded during channel verification: {exc}"
                ) from exc
            raise YouTubeAuthError(
                f"Could not verify authenticated YouTube {self._target_name} channel: {exc}"
            ) from exc
        items = response.get("items", [])
        actual_channel_id = items[0].get("id", "") if items else ""
        if actual_channel_id != self._expected_channel_id:
            raise YouTubeAuthError(
                f"YouTube {self._target_name} target channel mismatch: "
                f"expected {self._expected_channel_id!r}, authenticated {actual_channel_id!r}"
            )
        return actual_channel_id

    def _upload_captions(
        self,
        youtube,
        video_id: str,
        subtitle_path: Path,
        language: str = "tr",
        name: str = "",
    ) -> bool:
        """Attach a caption track to an uploaded video.

        Deliberately non-fatal, like the thumbnail: the video is already
        published at this point, and a missing caption track is a thing to fix
        afterwards, not a reason to fail a publish that otherwise succeeded.

        Needs the ``youtube.force-ssl`` scope — ``youtube.upload`` alone is not
        enough, so credentials issued before that scope was added will be
        rejected here and have to be re-authorised once.
        """
        try:
            from googleapiclient.http import MediaFileUpload

            media = MediaFileUpload(str(subtitle_path), mimetype="application/octet-stream")
            youtube.captions().insert(
                part="snippet",
                body={
                    "snippet": {
                        "videoId": video_id,
                        "language": language,
                        "name": name,
                        "isDraft": False,
                    }
                },
                media_body=media,
            ).execute()
            logger.info("Caption track uploaded for video %s (%s)", video_id, language)
            return True
        except Exception as exc:
            logger.warning("Caption upload failed (non-critical): %s", exc)
            return False

    def _execute_upload(
        self,
        insert_request,
        file_size: int,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> str:
        """Execute resumable upload, retrying on transient errors."""
        response = None
        attempt = 0

        while response is None:
            try:
                status, response = insert_request.next_chunk()
                if status:
                    uploaded = status.resumable_progress
                    pct = int(uploaded / file_size * 100) if file_size else 0
                    logger.info("Upload progress: %d%% (%d / %d bytes)", pct, uploaded, file_size)
                    if progress_callback:
                        progress_callback(uploaded, file_size)
                if response:
                    logger.info("Upload complete: video_id=%s", response.get("id"))
                    if progress_callback:
                        progress_callback(file_size, file_size)
            except Exception as exc:
                # Check for quota exceeded
                try:
                    from googleapiclient.errors import HttpError

                    if isinstance(exc, HttpError):
                        if exc.status_code == 403:
                            if _is_quota_error(exc):
                                raise YouTubeQuotaError(
                                    f"YouTube API quota exceeded: {exc}"
                                ) from exc
                            raise YouTubeUploadError(
                                f"YouTube API forbidden (403): {exc}"
                            ) from exc
                        if exc.status_code in (400, 401):
                            raise YouTubeUploadError(
                                f"YouTube API error {exc.status_code}: {exc}"
                            ) from exc
                        # Transient: 500, 502, 503, 504
                        if exc.status_code in (500, 502, 503, 504) and attempt < self._max_retries:
                            attempt += 1
                            wait_secs = 2**attempt
                            logger.warning(
                                "Transient upload error (attempt %d/%d), retrying in %ds: %s",
                                attempt,
                                self._max_retries,
                                wait_secs,
                                exc,
                            )
                            time.sleep(wait_secs)
                            continue
                except ImportError:
                    pass

                # Non-retryable or retries exhausted
                if attempt >= self._max_retries:
                    raise YouTubeUploadIndeterminateError(
                        f"Upload outcome is indeterminate after "
                        f"{self._max_retries} retries: {exc}"
                    ) from exc
                attempt += 1
                time.sleep(2**attempt)

        return response.get("id", "")

    def _upload_thumbnail(self, youtube, video_id: str, thumbnail_path: Path) -> bool:
        """Upload thumbnail image for a video."""
        try:
            from googleapiclient.http import MediaFileUpload

            media = MediaFileUpload(str(thumbnail_path), mimetype="image/png")
            youtube.thumbnails().set(videoId=video_id, media_body=media).execute()
            logger.info("Thumbnail uploaded for video %s", video_id)
            return True
        except Exception as exc:
            logger.warning("Thumbnail upload failed (non-critical): %s", exc)
            return False


# ---------------------------------------------------------------------------
# OAuth2 setup helpers
# ---------------------------------------------------------------------------

YOUTUBE_SCOPES = [
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube",
    # Required by captions.insert. Adding a scope invalidates existing
    # credentials, so `btcedu youtube-auth --target ...` has to be run once more
    # after this; until then captions simply fail and the upload does not.
    "https://www.googleapis.com/auth/youtube.force-ssl",
]


def authenticate(
    client_secrets_path: str,
    credentials_path: str,
    expected_channel_id: str = "",
    target_name: str = "test",
) -> dict:
    """Run OAuth2 consent flow and save credentials.

    Opens browser for Google OAuth consent.
    Saves refresh token to ``credentials_path``.

    Returns:
        dict with ``channel_name`` and ``channel_id`` of authenticated channel.

    Raises:
        YouTubeAuthError: If client secrets are missing or auth fails.
    """
    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError as exc:
        raise YouTubeAuthError(
            "google-auth-oauthlib not installed. Run: "
            "pip install google-auth-oauthlib google-api-python-client"
        ) from exc

    secrets_path = Path(client_secrets_path)
    if not secrets_path.exists():
        raise YouTubeAuthError(
            f"Client secrets file not found: {secrets_path}. "
            "Download from Google Cloud Console → APIs & Services → Credentials."
        )
    _ensure_private_credentials_file(secrets_path)

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), YOUTUBE_SCOPES)
    # Fixed port + no browser so headless SSH works with port-forwarding:
    #   ssh -L 8085:localhost:8085 pi@<host>
    creds = flow.run_local_server(port=8085, open_browser=False)

    # Fetch channel info before saving so the wrong account can never replace
    # the credentials for an explicitly configured target.
    youtube = build("youtube", "v3", credentials=creds)
    channel_resp = youtube.channels().list(part="snippet", mine=True).execute()
    items = channel_resp.get("items", [])
    if items:
        snippet = items[0].get("snippet", {})
        channel = {
            "channel_id": items[0].get("id"),
            "channel_name": snippet.get("title", "Unknown"),
        }
    else:
        channel = {"channel_id": None, "channel_name": "Unknown"}
    if expected_channel_id and channel["channel_id"] != expected_channel_id:
        raise YouTubeAuthError(
            f"YouTube {target_name} target channel mismatch: expected "
            f"{expected_channel_id!r}, authenticated {channel['channel_id']!r}"
        )

    creds_path = Path(credentials_path)
    _write_private_credentials_file(creds_path, creds.to_json())
    logger.info("Credentials saved to %s", creds_path)
    return channel


def check_token_status(credentials_path: str) -> dict:
    """Check if stored OAuth credentials are valid.

    Returns:
        dict with ``valid``, ``expired``, ``expiry``, ``can_refresh`` fields.
    """
    creds_path = Path(credentials_path)
    if not creds_path.exists():
        return {
            "valid": False,
            "expired": None,
            "expiry": None,
            "can_refresh": False,
            "error": "No credentials file found",
        }

    try:
        _ensure_private_credentials_file(creds_path)
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(str(creds_path))
        return {
            "valid": creds.valid,
            "expired": creds.expired,
            "expiry": creds.expiry.isoformat() if creds.expiry else None,
            "can_refresh": bool(creds.refresh_token),
        }
    except Exception as exc:
        return {
            "valid": False,
            "expired": None,
            "expiry": None,
            "can_refresh": False,
            "error": str(exc),
        }
