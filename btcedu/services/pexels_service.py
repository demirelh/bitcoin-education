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
