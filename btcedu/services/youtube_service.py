"""YouTube Data API v3 service: OAuth2, resumable upload, metadata."""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

logger = logging.getLogger(__name__)

# YouTube upload costs 1600 quota units per upload
YOUTUBE_UPLOAD_QUOTA_UNITS = 1600

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


@dataclass
class YouTubeUploadResponse:
    """Response from a successful YouTube upload."""

    video_id: str
    video_url: str  # https://youtu.be/{video_id}
    status: str = "uploaded"
    privacy_status: str = "unlisted"


class YouTubeAuthError(Exception):
    """OAuth2 authentication / token error."""


class YouTubeUploadError(Exception):
    """Upload or API error (non-quota)."""


class YouTubeQuotaError(Exception):
    """Quota exceeded (HTTP 403 quotaExceeded)."""


# ---------------------------------------------------------------------------
# Protocol
# ---------------------------------------------------------------------------


class YouTubeService(Protocol):
    """Interface for YouTube upload services."""

    def upload_video(
        self,
        req: YouTubeUploadRequest,
        progress_callback: Callable[[int, int], None] | None = None,
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
        *,
        _credentials=None,  # Injected in tests
    ):
        self._credentials_path = credentials_path
        self._chunk_size = chunk_size_bytes
        self._max_retries = max_retries
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

        try:
            creds = Credentials.from_authorized_user_file(str(creds_path))
        except Exception as exc:
            raise YouTubeAuthError(f"Could not load credentials: {exc}") from exc

        if not creds.valid:
            if creds.expired and creds.refresh_token:
                try:
                    creds.refresh(Request())
                    # Save refreshed token
                    creds_path.write_text(creds.to_json(), encoding="utf-8")
                except Exception as exc:
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
                "google-api-python-client not installed. Run: "
                "pip install google-api-python-client"
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
            from googleapiclient.errors import HttpError
            from googleapiclient.http import MediaFileUpload
        except ImportError as exc:
            raise YouTubeUploadError(
                "google-api-python-client not installed."
            ) from exc

        if not req.video_path.exists():
            raise YouTubeUploadError(f"Video file not found: {req.video_path}")

        youtube = self._build_client()
        file_size = req.video_path.stat().st_size

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

        # Upload thumbnail if available
        if req.thumbnail_path and req.thumbnail_path.exists():
            self._upload_thumbnail(youtube, video_id, req.thumbnail_path)

        return YouTubeUploadResponse(
            video_id=video_id,
            video_url=f"https://youtu.be/{video_id}",
            status="uploaded",
        )

    def _execute_upload(
        self,
        insert_request,
        file_size: int,
        progress_callback: Callable[[int, int], None] | None = None,
    ) -> str:
        """Execute resumable upload, retrying on transient errors."""
        try:
            from googleapiclient.errors import HttpError
            from googleapiclient.http import DEFAULT_CHUNK_SIZE
        except ImportError:
            DEFAULT_CHUNK_SIZE = 100 * 1024 * 1024

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
                            raise YouTubeQuotaError(
                                f"YouTube API quota exceeded: {exc}"
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
                    raise YouTubeUploadError(
                        f"Upload failed after {self._max_retries} retries: {exc}"
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
]


def authenticate(
    client_secrets_path: str,
    credentials_path: str,
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

    flow = InstalledAppFlow.from_client_secrets_file(str(secrets_path), YOUTUBE_SCOPES)
    creds = flow.run_local_server(port=0)  # Opens browser for consent

    # Save credentials
    creds_path = Path(credentials_path)
    creds_path.parent.mkdir(parents=True, exist_ok=True)
    creds_path.write_text(creds.to_json(), encoding="utf-8")
    logger.info("Credentials saved to %s", creds_path)

    # Fetch channel info to confirm
    youtube = build("youtube", "v3", credentials=creds)
    channel_resp = youtube.channels().list(part="snippet", mine=True).execute()
    items = channel_resp.get("items", [])
    if items:
        snippet = items[0].get("snippet", {})
        return {
            "channel_id": items[0].get("id"),
            "channel_name": snippet.get("title", "Unknown"),
        }
    return {"channel_id": None, "channel_name": "Unknown"}


def check_token_status(credentials_path: str) -> dict:
    """Check if stored OAuth credentials are valid.

    Returns:
        dict with ``valid``, ``expired``, ``expiry``, ``can_refresh`` fields.
    """
    creds_path = Path(credentials_path)
    if not creds_path.exists():
        return {"valid": False, "expired": None, "expiry": None, "can_refresh": False,
                "error": "No credentials file found"}

    try:
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_file(str(creds_path))
        return {
            "valid": creds.valid,
            "expired": creds.expired,
            "expiry": creds.expiry.isoformat() if creds.expiry else None,
            "can_refresh": bool(creds.refresh_token),
        }
    except Exception as exc:
        return {"valid": False, "expired": None, "expiry": None, "can_refresh": False,
                "error": str(exc)}
