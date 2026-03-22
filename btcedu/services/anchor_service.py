"""D-ID anchor video service: generate talking-head videos from photo + audio."""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests

logger = logging.getLogger(__name__)

# D-ID API
DID_API_BASE = "https://api.d-id.com"
DID_COST_PER_SECOND = 0.015  # ~$0.90/min on Pro plan

# Poll settings
POLL_INTERVAL_SECONDS = 5
POLL_MAX_ATTEMPTS = 120  # 10 minutes max


@dataclass
class AnchorRequest:
    """Request for anchor video generation."""

    source_image_path: str  # Local path to anchor photo
    source_image_url: str  # Pre-uploaded URL (optional, preferred over path)
    audio_path: str  # Local path to TTS audio MP3
    chapter_id: str
    expression: str = "serious"


@dataclass
class AnchorResponse:
    """Response from anchor video generation."""

    video_path: str  # Local path to downloaded video
    chapter_id: str
    duration_seconds: float
    size_bytes: int
    cost_usd: float
    did_talk_id: str


class AnchorService(Protocol):
    """Protocol for anchor video generation services."""

    def generate_anchor_video(self, request: AnchorRequest) -> AnchorResponse: ...


class DIDService:
    """D-ID Talks API: photo + audio -> talking-head video."""

    def __init__(self, api_key: str, output_dir: str):
        self.api_key = api_key
        self.output_dir = Path(output_dir)
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Basic {api_key}",
            "Accept": "application/json",
        })

    def generate_anchor_video(self, request: AnchorRequest) -> AnchorResponse:
        """Generate a talking-head video via D-ID Talks API.

        1. Upload source image (if no URL provided)
        2. Create talk with audio
        3. Poll until done
        4. Download result video
        """
        # Resolve source image URL
        source_url = request.source_image_url
        if not source_url:
            source_url = self._upload_image(request.source_image_path)

        # Upload audio
        audio_url = self._upload_audio(request.audio_path)

        # Create talk
        talk_id = self._create_talk(source_url, audio_url, request.expression)

        # Poll until done
        result_url, duration = self._poll_talk(talk_id)

        # Download video
        output_path = self.output_dir / f"{request.chapter_id}.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        self._download_video(result_url, output_path)

        size_bytes = output_path.stat().st_size
        cost_usd = duration * DID_COST_PER_SECOND

        return AnchorResponse(
            video_path=str(output_path),
            chapter_id=request.chapter_id,
            duration_seconds=duration,
            size_bytes=size_bytes,
            cost_usd=cost_usd,
            did_talk_id=talk_id,
        )

    def _upload_image(self, image_path: str) -> str:
        """Upload source image to D-ID and return URL."""
        with open(image_path, "rb") as f:
            resp = self.session.post(
                f"{DID_API_BASE}/images",
                files={"image": (Path(image_path).name, f, "image/png")},
            )
        resp.raise_for_status()
        return resp.json()["url"]

    def _upload_audio(self, audio_path: str) -> str:
        """Upload audio file to D-ID and return URL."""
        with open(audio_path, "rb") as f:
            resp = self.session.post(
                f"{DID_API_BASE}/audios",
                files={"audio": (Path(audio_path).name, f, "audio/mpeg")},
            )
        resp.raise_for_status()
        return resp.json()["url"]

    def _create_talk(self, source_url: str, audio_url: str, expression: str) -> str:
        """Create a D-ID talk and return the talk ID."""
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
        resp = self.session.post(f"{DID_API_BASE}/talks", json=payload)
        resp.raise_for_status()
        return resp.json()["id"]

    def _poll_talk(self, talk_id: str) -> tuple[str, float]:
        """Poll until talk is done. Returns (result_url, duration_seconds)."""
        for attempt in range(POLL_MAX_ATTEMPTS):
            resp = self.session.get(f"{DID_API_BASE}/talks/{talk_id}")
            resp.raise_for_status()
            data = resp.json()

            status = data.get("status")
            if status == "done":
                result_url = data.get("result_url", "")
                duration = float(data.get("duration", 0))
                return result_url, duration
            elif status == "error":
                error_msg = data.get("error", {}).get("description", "Unknown D-ID error")
                raise RuntimeError(f"D-ID talk {talk_id} failed: {error_msg}")

            logger.debug("D-ID talk %s status: %s (attempt %d)", talk_id, status, attempt + 1)
            time.sleep(POLL_INTERVAL_SECONDS)

        raise TimeoutError(f"D-ID talk {talk_id} did not complete within timeout")

    def _download_video(self, url: str, output_path: Path) -> None:
        """Download the result video."""
        resp = requests.get(url, stream=True, timeout=120)
        resp.raise_for_status()
        with open(output_path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)


class DryRunAnchorService:
    """Placeholder anchor service for dry-run and testing."""

    def __init__(self, output_dir: str = ""):
        self.output_dir = Path(output_dir) if output_dir else Path("/tmp/anchor_dry_run")

    def generate_anchor_video(self, request: AnchorRequest) -> AnchorResponse:
        """Return a placeholder response without calling any API."""
        output_path = self.output_dir / f"{request.chapter_id}.mp4"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        # Write a tiny placeholder file
        output_path.write_bytes(b"\x00" * 1024)
        return AnchorResponse(
            video_path=str(output_path),
            chapter_id=request.chapter_id,
            duration_seconds=30.0,
            size_bytes=1024,
            cost_usd=0.0,
            did_talk_id="dry-run",
        )
