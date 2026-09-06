"""Provider-neutral talking-avatar video services."""

import logging
import mimetypes
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests

logger = logging.getLogger(__name__)

DID_API_BASE = "https://api.d-id.com"
HEYGEN_API_BASE = "https://api.heygen.com"
DID_COST_PER_SECOND = 0.015
HEYGEN_COST_PER_SECOND = {
    ("avatar_iii", "digital_twin"): 0.0167,
    ("avatar_iii", "studio_avatar"): 0.0167,
    ("avatar_iii", "photo_avatar"): 0.0433,
    ("avatar_iv", "digital_twin"): 0.0667,
    ("avatar_iv", "studio_avatar"): 0.0667,
    ("avatar_iv", "photo_avatar"): 0.05,
    ("avatar_v", "digital_twin"): 0.0667,
}
HEYGEN_MAX_ASSET_BYTES = 32 * 1024 * 1024

POLL_INTERVAL_SECONDS = 5
POLL_MAX_ATTEMPTS = 120


class AnchorAPIError(RuntimeError):
    """Structured provider failure without exposing request credentials."""

    def __init__(
        self,
        provider: str,
        status_code: int,
        detail: str,
        error_code: str | None = None,
    ):
        self.provider = provider
        self.status_code = status_code
        self.error_code = error_code
        self.detail = detail
        code_suffix = f" ({error_code})" if error_code else ""
        super().__init__(f"{provider} API error {status_code}{code_suffix}: {detail}")


@dataclass
class AnchorRequest:
    """Provider-neutral request for a talking-avatar video."""

    source_image_path: str
    source_image_url: str
    audio_path: str
    chapter_id: str
    expression: str = "serious"
    expected_duration_seconds: float = 0.0
    # Names the output file when one chapter yields several clips. Falls back to
    # chapter_id, which is what the chapter-based caller has always passed.
    clip_id: str = ""
    # Belt and braces on top of the durable job ledger: if the provider honours
    # the header, a retry of a request whose answer never arrived returns the
    # original job instead of starting a second, separately billed one. The
    # ledger does not depend on it — an ignored header changes nothing.
    idempotency_key: str = ""

    @property
    def output_name(self) -> str:
        return self.clip_id or self.chapter_id


@dataclass
class AnchorResponse:
    """Provider-neutral result from talking-avatar generation."""

    video_path: str
    chapter_id: str
    duration_seconds: float
    size_bytes: int
    cost_usd: float
    provider: str = "d-id"
    provider_job_id: str = ""
    output_format: str = "mp4"
    mime_type: str = "video/mp4"
    did_talk_id: str = ""

    def __post_init__(self) -> None:
        """Keep the legacy D-ID field synchronized with the generic job ID."""
        if self.did_talk_id and not self.provider_job_id:
            self.provider_job_id = self.did_talk_id
        elif self.provider == "d-id" and self.provider_job_id and not self.did_talk_id:
            self.did_talk_id = self.provider_job_id


class AnchorService(Protocol):
    """Protocol implemented by talking-avatar providers."""

    provider: str
    engine: str
    output_format: str
    output_extension: str
    mime_type: str

    def estimate_cost(self, duration_seconds: float) -> float: ...

    def generate_anchor_video(self, request: AnchorRequest) -> AnchorResponse: ...

    def submit_anchor_video(self, request: AnchorRequest) -> str:
        """Start a generation and return the provider's job id, nothing more.

        Split out from ``generate_anchor_video`` so the caller can persist the
        id before the long, fallible poll-and-download. That is the difference
        between a reboot resuming a job and a reboot buying it again.
        """
        ...

    def collect_anchor_video(
        self, provider_job_id: str, request: AnchorRequest
    ) -> AnchorResponse:
        """Poll an already-submitted job and download its result."""
        ...



def heygen_cost_per_second(engine: str, avatar_type: str) -> float:
    """Return the published HeyGen self-serve rate for an engine/avatar pair."""
    key = (engine.strip().lower(), avatar_type.strip().lower())
    try:
        return HEYGEN_COST_PER_SECOND[key]
    except KeyError as exc:
        raise ValueError(
            f"Unsupported HeyGen pricing combination: engine={engine!r}, "
            f"avatar_type={avatar_type!r}"
        ) from exc


class DIDService:
    """D-ID Talks API: source photo plus pre-generated audio."""

    provider = "d-id"
    engine = "talks"
    output_format = "mp4"
    output_extension = ".mp4"
    mime_type = "video/mp4"

    def __init__(
        self,
        api_key: str,
        output_dir: str,
        cost_per_second_usd: float = DID_COST_PER_SECOND,
    ):
        if not api_key:
            raise ValueError("D-ID anchor provider requires DID_API_KEY")
        if cost_per_second_usd < 0:
            raise ValueError("D-ID cost_per_second_usd must be non-negative")
        self.api_key = api_key
        self.output_dir = Path(output_dir)
        self.cost_per_second_usd = cost_per_second_usd
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Basic {api_key}",
                "Accept": "application/json",
            }
        )

    def estimate_cost(self, duration_seconds: float) -> float:
        return round(max(0.0, duration_seconds) * self.cost_per_second_usd, 6)

    def generate_anchor_video(self, request: AnchorRequest) -> AnchorResponse:
        """Generate a D-ID talking-head video and download it."""
        talk_id = self.submit_anchor_video(request)
        return self.collect_anchor_video(talk_id, request)

    def submit_anchor_video(self, request: AnchorRequest) -> str:
        source_url = request.source_image_url
        if not source_url:
            source_url = self._upload_image(request.source_image_path)

        audio_url = self._upload_audio(request.audio_path)
        return self._create_talk(source_url, audio_url, request.expression)

    def collect_anchor_video(self, provider_job_id: str, request: AnchorRequest) -> AnchorResponse:
        result_url, duration = self._poll_talk(provider_job_id)

        output_path = self.output_dir / f"{request.output_name}{self.output_extension}"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._download_video(result_url, output_path)

        return AnchorResponse(
            video_path=str(output_path),
            chapter_id=request.chapter_id,
            duration_seconds=duration,
            size_bytes=output_path.stat().st_size,
            cost_usd=self.estimate_cost(duration),
            provider=self.provider,
            provider_job_id=provider_job_id,
            output_format=self.output_format,
            mime_type=self.mime_type,
        )

    def _upload_image(self, image_path: str) -> str:
        path = _require_file(image_path, "D-ID source image")
        mime_type = mimetypes.guess_type(path.name)[0] or "image/png"
        with path.open("rb") as file_obj:
            response = self.session.post(
                f"{DID_API_BASE}/images",
                files={"image": (path.name, file_obj, mime_type)},
                timeout=120,
            )
        data = _response_data(response, "D-ID")
        return _required_string(data, "url", "D-ID image upload response")

    def _upload_audio(self, audio_path: str) -> str:
        path = _require_file(audio_path, "D-ID audio")
        mime_type = _audio_mime_type(path)
        with path.open("rb") as file_obj:
            response = self.session.post(
                f"{DID_API_BASE}/audios",
                files={"audio": (path.name, file_obj, mime_type)},
                timeout=120,
            )
        data = _response_data(response, "D-ID")
        return _required_string(data, "url", "D-ID audio upload response")

    def _create_talk(self, source_url: str, audio_url: str, expression: str) -> str:
        payload = {
            "source_url": source_url,
            "script": {
                "type": "audio",
                "audio_url": audio_url,
            },
            "config": {
                "result_format": "mp4",
                "expression": {"expressions": [{"expression": expression, "intensity": 0.5}]},
            },
        }
        response = self.session.post(f"{DID_API_BASE}/talks", json=payload, timeout=120)
        data = _response_data(response, "D-ID")
        return _required_string(data, "id", "D-ID talk response")

    def _poll_talk(self, talk_id: str) -> tuple[str, float]:
        for attempt in range(POLL_MAX_ATTEMPTS):
            response = self.session.get(f"{DID_API_BASE}/talks/{talk_id}", timeout=120)
            data = _response_data(response, "D-ID")
            status = str(data.get("status") or "").lower()

            if status == "done":
                result_url = _required_string(data, "result_url", "D-ID completed talk")
                return result_url, _required_duration(data, "duration", "D-ID completed talk")
            if status == "error":
                error = data.get("error")
                detail = (
                    str(error.get("description") or "Unknown D-ID error")
                    if isinstance(error, dict)
                    else str(error or "Unknown D-ID error")
                )
                raise AnchorAPIError("D-ID", 422, detail, "generation_failed")

            logger.debug("D-ID talk %s status: %s (attempt %d)", talk_id, status, attempt + 1)
            time.sleep(POLL_INTERVAL_SECONDS)

        raise TimeoutError(f"D-ID talk {talk_id} did not complete within timeout")

    def _download_video(self, url: str, output_path: Path) -> None:
        """Download through the legacy D-ID test seam."""
        _download_video("D-ID", url, output_path)


class HeyGenService:
    """HeyGen v3 avatar video API driven by pre-generated TTS audio.

    WebM alpha is available at this API boundary; active profiles use MP4 until
    the renderer can composite a transparent presenter over a studio.
    """

    provider = "heygen"

    def __init__(
        self,
        api_key: str,
        output_dir: str,
        avatar_id: str,
        engine: str = "avatar_iv",
        avatar_type: str = "digital_twin",
        output_format: str = "mp4",
        resolution: str = "1080p",
        aspect_ratio: str = "auto",
        cost_per_second_usd: float | None = None,
    ):
        if not api_key:
            raise ValueError("HeyGen anchor provider requires HEYGEN_API_KEY")
        if not avatar_id:
            raise ValueError("HeyGen anchor provider requires HEYGEN_AVATAR_ID")

        engine = engine.strip().lower()
        if engine not in {"avatar_iii", "avatar_iv", "avatar_v"}:
            raise ValueError(f"Unsupported HeyGen engine: {engine!r}")
        output_format = output_format.strip().lower()
        if output_format not in {"mp4", "webm"}:
            raise ValueError("HeyGen output_format must be 'mp4' or 'webm'")
        if cost_per_second_usd is None:
            cost_per_second_usd = heygen_cost_per_second(engine, avatar_type)
        if cost_per_second_usd < 0:
            raise ValueError("HeyGen cost_per_second_usd must be non-negative")

        self.output_dir = Path(output_dir)
        self.avatar_id = avatar_id
        self.engine = engine
        self.avatar_type = avatar_type.strip().lower()
        self.output_format = output_format
        self.output_extension = f".{output_format}"
        self.mime_type = "video/webm" if output_format == "webm" else "video/mp4"
        self.resolution = resolution
        self.aspect_ratio = aspect_ratio
        self.cost_per_second_usd = cost_per_second_usd
        self.session = requests.Session()
        self.session.headers.update(
            {
                "x-api-key": api_key,
                "Accept": "application/json",
            }
        )

    def estimate_cost(self, duration_seconds: float) -> float:
        return round(max(0.0, duration_seconds) * self.cost_per_second_usd, 6)

    def generate_anchor_video(self, request: AnchorRequest) -> AnchorResponse:
        """Upload TTS audio, create an avatar video, poll, and download it."""
        video_id = self.submit_anchor_video(request)
        return self.collect_anchor_video(video_id, request)

    def submit_anchor_video(self, request: AnchorRequest) -> str:
        audio_asset_id = self._upload_audio(request.audio_path)
        return self._create_video(
            audio_asset_id,
            request.chapter_id,
            idempotency_key=request.idempotency_key,
        )

    def collect_anchor_video(self, provider_job_id: str, request: AnchorRequest) -> AnchorResponse:
        result_url, duration = self._poll_video(provider_job_id)

        output_path = self.output_dir / f"{request.output_name}{self.output_extension}"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _download_video("HeyGen", result_url, output_path)

        return AnchorResponse(
            video_path=str(output_path),
            chapter_id=request.chapter_id,
            duration_seconds=duration,
            size_bytes=output_path.stat().st_size,
            cost_usd=self.estimate_cost(duration),
            provider=self.provider,
            provider_job_id=provider_job_id,
            output_format=self.output_format,
            mime_type=self.mime_type,
        )

    def _upload_audio(self, audio_path: str) -> str:
        path = _require_file(audio_path, "HeyGen audio")
        if path.stat().st_size > HEYGEN_MAX_ASSET_BYTES:
            raise ValueError(
                f"HeyGen audio asset exceeds the 32 MB upload limit: {path.stat().st_size} bytes"
            )
        with path.open("rb") as file_obj:
            response = self.session.post(
                f"{HEYGEN_API_BASE}/v3/assets",
                files={"file": (path.name, file_obj, _audio_mime_type(path))},
                timeout=120,
            )
        data = _response_data(response, "HeyGen")
        return _required_string(data, "asset_id", "HeyGen asset upload response")

    def _create_video(
        self,
        audio_asset_id: str,
        chapter_id: str,
        idempotency_key: str = "",
    ) -> str:
        payload = {
            "type": "avatar",
            "avatar_id": self.avatar_id,
            "audio_asset_id": audio_asset_id,
            "title": chapter_id,
            "resolution": self.resolution,
            "aspect_ratio": self.aspect_ratio,
            "output_format": self.output_format,
            "engine": {"type": self.engine},
        }
        # An extra layer, not the guarantee. Providers that honour the header
        # return the original job for a repeated key within their retention
        # window (24 hours is the common one); providers that ignore it lose
        # nothing, because the ledger has already decided whether this request
        # may be sent at all.
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else None
        response = self.session.post(
            f"{HEYGEN_API_BASE}/v3/videos",
            json=payload,
            headers=headers,
            timeout=120,
        )
        data = _response_data(response, "HeyGen")
        resolved_format = str(data.get("output_format") or self.output_format).lower()
        if resolved_format != self.output_format:
            raise AnchorAPIError(
                "HeyGen",
                502,
                f"requested {self.output_format}, provider resolved {resolved_format}",
                "unexpected_output_format",
            )
        return _required_string(data, "video_id", "HeyGen create-video response")

    def _poll_video(self, video_id: str) -> tuple[str, float]:
        active_statuses = {"waiting", "pending", "processing"}
        for attempt in range(POLL_MAX_ATTEMPTS):
            response = self.session.get(f"{HEYGEN_API_BASE}/v3/videos/{video_id}", timeout=120)
            data = _response_data(response, "HeyGen")
            status = str(data.get("status") or "").lower()

            if status == "completed":
                result_url = _required_string(data, "video_url", "HeyGen completed video")
                return result_url, _required_duration(data, "duration", "HeyGen completed video")
            if status == "failed":
                detail = str(data.get("failure_message") or "Unknown HeyGen generation error")
                error_code = str(data.get("failure_code") or "generation_failed")
                raise AnchorAPIError("HeyGen", 422, detail, error_code)
            if status not in active_statuses:
                raise AnchorAPIError(
                    "HeyGen",
                    502,
                    f"unexpected video status {status!r}",
                    "unexpected_status",
                )

            logger.debug(
                "HeyGen video %s status: %s (attempt %d)",
                video_id,
                status,
                attempt + 1,
            )
            time.sleep(POLL_INTERVAL_SECONDS)

        raise TimeoutError(f"HeyGen video {video_id} did not complete within timeout")


class DryRunAnchorService:
    """Local placeholder implementation for dry-run and tests."""

    def __init__(
        self,
        output_dir: str = "",
        provider: str = "d-id",
        engine: str = "talks",
        output_format: str = "mp4",
    ):
        self.output_dir = Path(output_dir) if output_dir else Path("data/outputs/anchor_dry_run")
        self.provider = provider
        self.engine = engine
        self.output_format = output_format
        self.output_extension = f".{output_format}"
        self.mime_type = "video/webm" if output_format == "webm" else "video/mp4"

    def estimate_cost(self, duration_seconds: float) -> float:
        del duration_seconds
        return 0.0

    def generate_anchor_video(self, request: AnchorRequest) -> AnchorResponse:
        job_id = self.submit_anchor_video(request)
        return self.collect_anchor_video(job_id, request)

    def submit_anchor_video(self, request: AnchorRequest) -> str:
        del request
        return "dry-run"

    def collect_anchor_video(self, provider_job_id: str, request: AnchorRequest) -> AnchorResponse:
        output_path = self.output_dir / f"{request.output_name}{self.output_extension}"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"\x00" * 1024)
        duration = request.expected_duration_seconds or 30.0
        return AnchorResponse(
            video_path=str(output_path),
            chapter_id=request.chapter_id,
            duration_seconds=duration,
            size_bytes=1024,
            cost_usd=0.0,
            provider=self.provider,
            provider_job_id=provider_job_id or "dry-run",
            output_format=self.output_format,
            mime_type=self.mime_type,
        )


def _response_data(response, provider: str) -> dict:
    status_code = getattr(response, "status_code", 200)
    if not isinstance(status_code, int):
        status_code = 200
    try:
        payload = response.json()
    except (TypeError, ValueError) as exc:
        if status_code >= 400:
            detail = str(getattr(response, "text", "") or "non-JSON error response")[:300]
            raise AnchorAPIError(provider, status_code, detail) from exc
        raise AnchorAPIError(provider, 502, "provider returned invalid JSON") from exc

    if status_code >= 400:
        error = payload.get("error") if isinstance(payload, dict) else None
        if not isinstance(error, dict) and isinstance(payload, dict):
            error = payload.get("detail")
        if isinstance(error, dict):
            error_code = error.get("code") or error.get("status")
            detail = error.get("message") or error.get("description") or str(error)
        else:
            error_code = None
            detail = str(error or getattr(response, "text", "") or "provider request failed")
        raise AnchorAPIError(
            provider,
            status_code,
            str(detail)[:300],
            str(error_code or "") or None,
        )

    if not isinstance(payload, dict):
        raise AnchorAPIError(provider, 502, "provider returned a non-object response")
    data = payload.get("data", payload)
    if not isinstance(data, dict):
        raise AnchorAPIError(provider, 502, "provider response data is not an object")
    return data


def _required_string(data: dict, key: str, context: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise AnchorAPIError("Avatar provider", 502, f"{context} is missing {key!r}")
    return value


def _required_duration(data: dict, key: str, context: str) -> float:
    try:
        duration = float(data.get(key))
    except (TypeError, ValueError) as exc:
        raise AnchorAPIError("Avatar provider", 502, f"{context} has invalid {key!r}") from exc
    if duration <= 0:
        raise AnchorAPIError("Avatar provider", 502, f"{context} has non-positive {key!r}")
    return duration


def _require_file(path_value: str, label: str) -> Path:
    path = Path(path_value)
    if not path.is_file():
        raise FileNotFoundError(f"{label} not found: {path}")
    return path


def _audio_mime_type(path: Path) -> str:
    mime_type = mimetypes.guess_type(path.name)[0]
    if mime_type in {"audio/mpeg", "audio/wav", "audio/x-wav"}:
        return "audio/wav" if mime_type == "audio/x-wav" else mime_type
    raise ValueError(f"Unsupported anchor audio format: {path.suffix or path.name}")


def _download_video(provider: str, url: str, output_path: Path) -> None:
    if not url:
        raise AnchorAPIError(provider, 502, "completed video response has no download URL")
    response = requests.get(url, stream=True, timeout=120)
    status_code = getattr(response, "status_code", 200)
    if isinstance(status_code, int) and status_code >= 400:
        raise AnchorAPIError(provider, status_code, "video download failed")
    with output_path.open("wb") as file_obj:
        for chunk in response.iter_content(chunk_size=8192):
            if chunk:
                file_obj.write(chunk)
