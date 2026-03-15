"""Pexels stock photo API client with rate limiting and retry."""

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import requests

logger = logging.getLogger(__name__)

# Pexels API
API_BASE = "https://api.pexels.com/v1"
VIDEO_API_BASE = "https://api.pexels.com/videos"
DEFAULT_RATE_LIMIT = 180  # requests per hour (conservative; actual limit is 200)


@dataclass
class PexelsPhoto:
    """Single photo result from Pexels API."""

    id: int
    width: int
    height: int
    url: str  # Pexels page URL
    photographer: str
    photographer_url: str
    src_original: str  # Full-res download URL
    src_landscape: str  # 1200x627 landscape crop
    src_large2x: str  # 1880px wide
    alt: str  # Alt text / description
    avg_color: str  # Hex color


@dataclass
class PexelsSearchResult:
    """Response from a Pexels search."""

    query: str
    total_results: int
    photos: list[PexelsPhoto]
    page: int
    per_page: int


@dataclass
class PexelsVideoFile:
    """A single video file variant from Pexels."""

    id: int
    quality: str      # "hd", "sd", "uhd"
    file_type: str    # "video/mp4"
    width: int
    height: int
    fps: float
    link: str


@dataclass
class PexelsVideo:
    """Single video result from Pexels Video API."""

    id: int
    width: int
    height: int
    url: str           # Pexels page URL
    duration: int      # seconds
    image: str         # preview thumbnail URL
    user_name: str
    user_url: str
    video_files: list[PexelsVideoFile]


@dataclass
class PexelsVideoSearchResult:
    """Response from a Pexels video search."""

    query: str
    total_results: int
    videos: list[PexelsVideo]
    page: int
    per_page: int


class StockPhotoService(Protocol):
    """Protocol for stock photo services (future: Unsplash, Pixabay)."""

    def search(
        self, query: str, per_page: int, orientation: str
    ) -> PexelsSearchResult: ...

    def download_photo(self, photo: PexelsPhoto, target_path: Path) -> Path: ...


class PexelsService:
    """Pexels stock photo API client with rate limiting."""

    def __init__(self, api_key: str, requests_per_hour: int = DEFAULT_RATE_LIMIT):
        if not api_key:
            raise ValueError("Pexels API key is required")
        self.api_key = api_key
        self.requests_per_hour = requests_per_hour
        self._request_timestamps: list[float] = []

    def search(
        self,
        query: str,
        per_page: int = 8,
        page: int = 1,
        orientation: str = "landscape",
        size: str = "large",
    ) -> PexelsSearchResult:
        """Search Pexels for photos matching query.

        Args:
            query: Search terms (English works best)
            per_page: Results per page (1-80)
            page: Page number
            orientation: "landscape", "portrait", or "square"
            size: "large", "medium", or "small"

        Returns:
            PexelsSearchResult with photos

        Raises:
            RuntimeError: On API error or rate limit exceeded after retries.
        """
        self._rate_limit_wait()

        params = {
            "query": query,
            "per_page": per_page,
            "page": page,
            "orientation": orientation,
            "size": size,
        }
        headers = {"Authorization": self.api_key}

        response = self._request_with_retry(
            "GET", f"{API_BASE}/search", headers=headers, params=params
        )

        data = response.json()
        self._record_request()

        photos = [
            PexelsPhoto(
                id=p["id"],
                width=p["width"],
                height=p["height"],
                url=p["url"],
                photographer=p["photographer"],
                photographer_url=p.get("photographer_url", ""),
                src_original=p["src"]["original"],
                src_landscape=p["src"]["landscape"],
                src_large2x=p["src"]["large2x"],
                alt=p.get("alt", ""),
                avg_color=p.get("avg_color", ""),
            )
            for p in data.get("photos", [])
        ]

        return PexelsSearchResult(
            query=query,
            total_results=data.get("total_results", 0),
            photos=photos,
            page=data.get("page", page),
            per_page=data.get("per_page", per_page),
        )

    def download_photo(
        self,
        photo: PexelsPhoto,
        target_path: Path,
        size: str = "large2x",
    ) -> Path:
        """Download photo to local file.

        Args:
            photo: PexelsPhoto to download
            target_path: Where to save the file
            size: Which size to download ("original", "large2x", "landscape")

        Returns:
            Path to saved file
        """
        url_map = {
            "original": photo.src_original,
            "large2x": photo.src_large2x,
            "landscape": photo.src_landscape,
        }
        url = url_map.get(size, photo.src_large2x)

        response = requests.get(url, timeout=60)
        response.raise_for_status()

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(response.content)

        logger.info(
            f"Downloaded Pexels photo {photo.id} to {target_path} "
            f"({len(response.content)} bytes)"
        )
        return target_path

    def search_videos(
        self,
        query: str,
        per_page: int = 5,
        page: int = 1,
        orientation: str = "landscape",
        size: str = "large",
    ) -> PexelsVideoSearchResult:
        """Search Pexels for videos matching query.

        Args:
            query: Search terms (English works best)
            per_page: Results per page (1-80)
            page: Page number
            orientation: "landscape", "portrait", or "square"
            size: "large", "medium", or "small"

        Returns:
            PexelsVideoSearchResult with videos

        Raises:
            RuntimeError: On API error or rate limit exceeded after retries.
        """
        self._rate_limit_wait()

        params = {
            "query": query,
            "per_page": per_page,
            "page": page,
            "orientation": orientation,
            "size": size,
        }
        headers = {"Authorization": self.api_key}

        response = self._request_with_retry(
            "GET", f"{VIDEO_API_BASE}/search", headers=headers, params=params
        )

        data = response.json()
        self._record_request()

        videos = []
        for v in data.get("videos", []):
            user = v.get("user", {})
            video_files = [
                PexelsVideoFile(
                    id=vf.get("id", 0),
                    quality=vf.get("quality", ""),
                    file_type=vf.get("file_type", "video/mp4"),
                    width=vf.get("width", 0),
                    height=vf.get("height", 0),
                    fps=float(vf.get("fps", 0)),
                    link=vf.get("link", ""),
                )
                for vf in v.get("video_files", [])
            ]
            videos.append(
                PexelsVideo(
                    id=v["id"],
                    width=v.get("width", 0),
                    height=v.get("height", 0),
                    url=v.get("url", ""),
                    duration=v.get("duration", 0),
                    image=v.get("image", ""),
                    user_name=user.get("name", ""),
                    user_url=user.get("url", ""),
                    video_files=video_files,
                )
            )

        return PexelsVideoSearchResult(
            query=query,
            total_results=data.get("total_results", 0),
            videos=videos,
            page=data.get("page", page),
            per_page=data.get("per_page", per_page),
        )

    def download_video(
        self,
        video: PexelsVideo,
        target_path: Path,
        preferred_quality: str = "hd",
    ) -> Path:
        """Download video file (HD variant preferred).

        Args:
            video: PexelsVideo to download
            target_path: Where to save the file
            preferred_quality: Preferred quality ("hd", "sd")

        Returns:
            Path to saved file
        """
        video_file = self._select_video_file(video, preferred_quality)
        if not video_file:
            raise ValueError(f"No suitable video file found for Pexels video {video.id}")

        response = requests.get(video_file.link, timeout=120)
        response.raise_for_status()

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(response.content)

        logger.info(
            "Downloaded Pexels video %d to %s (%d bytes, quality=%s)",
            video.id, target_path, len(response.content), video_file.quality,
        )
        return target_path

    def download_video_preview(
        self,
        video: PexelsVideo,
        target_path: Path,
    ) -> Path:
        """Download video preview thumbnail.

        Args:
            video: PexelsVideo whose preview to download
            target_path: Where to save the thumbnail

        Returns:
            Path to saved file
        """
        if not video.image:
            raise ValueError(f"No preview image URL for Pexels video {video.id}")

        response = requests.get(video.image, timeout=60)
        response.raise_for_status()

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_bytes(response.content)

        logger.info(
            "Downloaded Pexels video preview %d to %s (%d bytes)",
            video.id, target_path, len(response.content),
        )
        return target_path

    def _select_video_file(
        self,
        video: PexelsVideo,
        preferred_quality: str,
    ) -> PexelsVideoFile | None:
        """Select best video file from available variants.

        Priority:
        1. quality == preferred_quality and width >= 1280
        2. Highest resolution available

        Args:
            video: PexelsVideo with video_files list
            preferred_quality: Preferred quality string ("hd", "sd")

        Returns:
            Best PexelsVideoFile, or None if no files available
        """
        if not video.video_files:
            return None

        # Try to find preferred quality at minimum resolution
        preferred = [
            vf for vf in video.video_files
            if vf.quality == preferred_quality and vf.width >= 1280
        ]
        if preferred:
            # Pick highest resolution among preferred quality
            return max(preferred, key=lambda vf: vf.width * vf.height)

        # Fallback: highest resolution available
        return max(video.video_files, key=lambda vf: vf.width * vf.height)

    def _request_with_retry(
        self,
        method: str,
        url: str,
        max_retries: int = 3,
        **kwargs,
    ) -> requests.Response:
        """Make HTTP request with exponential backoff retry on 429."""
        for attempt in range(max_retries):
            response = requests.request(method, url, timeout=30, **kwargs)

            if response.status_code == 429:
                if attempt < max_retries - 1:
                    wait_time = min(2 ** (attempt + 1), 60)
                    logger.warning(
                        f"Pexels rate limit hit (attempt {attempt + 1}/{max_retries}), "
                        f"retrying in {wait_time}s..."
                    )
                    time.sleep(wait_time)
                    continue
                raise RuntimeError("Pexels rate limit exceeded after retries")

            if response.status_code != 200:
                raise RuntimeError(
                    f"Pexels API error {response.status_code}: {response.text[:200]}"
                )

            return response

        raise RuntimeError(f"Pexels API request failed after {max_retries} attempts")

    def _rate_limit_wait(self) -> None:
        """Block if approaching rate limit. Uses sliding window."""
        now = time.monotonic()
        window = 3600  # 1 hour in seconds

        # Remove timestamps older than 1 hour
        self._request_timestamps = [
            t for t in self._request_timestamps if now - t < window
        ]

        if len(self._request_timestamps) >= self.requests_per_hour:
            oldest = self._request_timestamps[0]
            sleep_time = window - (now - oldest) + 1
            if sleep_time > 0:
                logger.info(f"Pexels rate limit approaching, sleeping {sleep_time:.0f}s")
                time.sleep(sleep_time)

    def _record_request(self) -> None:
        """Record a request timestamp for rate limiting."""
        self._request_timestamps.append(time.monotonic())
