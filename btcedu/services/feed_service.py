import hashlib
import json
import logging
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import feedparser

from btcedu.models.schemas import EpisodeInfo

logger = logging.getLogger(__name__)


def _struct_to_datetime(st: object) -> datetime | None:
    """Convert feedparser's time.struct_time to timezone-aware datetime."""
    if st is None:
        return None
    try:
        from calendar import timegm
        return datetime.fromtimestamp(timegm(st), tz=timezone.utc)
    except Exception:
        return None


def _extract_youtube_video_id(entry: dict) -> str | None:
    """Extract YouTube video ID from a feed entry."""
    # feedparser exposes yt:videoId as yt_videoid
    vid = getattr(entry, "yt_videoid", None)
    if vid:
        return vid
    # Fallback: parse from link URL
    link = entry.get("link", "")
    if "youtube.com/watch" in link and "v=" in link:
        return link.split("v=")[1].split("&")[0]
    return None


def _make_fallback_id(url: str) -> str:
    """Generate a stable episode ID from a URL via sha1."""
    return hashlib.sha1(url.encode()).hexdigest()[:12]


def parse_youtube_rss(feed_content: str) -> list[EpisodeInfo]:
    """Parse a YouTube channel Atom feed and return episode info list."""
    feed = feedparser.parse(feed_content)
    episodes = []
    for entry in feed.entries:
        video_id = _extract_youtube_video_id(entry)
        if not video_id:
            continue
        link = entry.get("link", f"https://www.youtube.com/watch?v={video_id}")
        published = _struct_to_datetime(entry.get("published_parsed"))
        episodes.append(
            EpisodeInfo(
                episode_id=video_id,
                title=entry.get("title", "Untitled"),
                published_at=published,
                url=link,
                source="youtube_rss",
            )
        )
    return episodes


def parse_rss(feed_content: str) -> list[EpisodeInfo]:
    """Parse a generic RSS/Atom feed and return episode info list."""
    feed = feedparser.parse(feed_content)
    episodes = []
    for entry in feed.entries:
        link = entry.get("link", "")
        if not link:
            continue
        episode_id = _make_fallback_id(link)
        published = _struct_to_datetime(entry.get("published_parsed"))
        episodes.append(
            EpisodeInfo(
                episode_id=episode_id,
                title=entry.get("title", "Untitled"),
                published_at=published,
                url=link,
                source="rss",
            )
        )
    return episodes


def fetch_feed(url: str, timeout: int = 30) -> str:
    """Fetch RSS/Atom feed content from a URL.

    Uses feedparser's built-in HTTP fetching but returns raw content
    for testability. In practice, we parse directly.
    """
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": "btcedu/0.1"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8")


def parse_feed(feed_content: str, source_type: str) -> list[EpisodeInfo]:
    """Parse feed content based on source type."""
    if source_type == "youtube_rss":
        return parse_youtube_rss(feed_content)
    return parse_rss(feed_content)


def _find_ytdlp() -> str:
    """Locate the yt-dlp binary (same logic as download_service)."""
    path = shutil.which("yt-dlp") or str(Path(sys.executable).parent / "yt-dlp")
    return path


def fetch_channel_videos_ytdlp(channel_id: str, timeout: int = 120) -> list[EpisodeInfo]:
    """List all videos from a YouTube channel using yt-dlp --flat-playlist -J.

    Returns a list of EpisodeInfo sorted by published_at descending (newest first).
    """
    url = f"https://www.youtube.com/channel/{channel_id}/videos"
    ytdlp = _find_ytdlp()

    cmd = [
        ytdlp, "--flat-playlist", "-J", "--no-warnings",
        "--extractor-args", "youtubetab:approximate_date",
        url,
    ]
    logger.info("Listing channel videos: %s", url)

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    if result.returncode != 0:
        raise RuntimeError(
            f"yt-dlp failed (exit {result.returncode}): {result.stderr.strip()}"
        )

    data = json.loads(result.stdout)
    entries = data.get("entries") or []

    episodes: list[EpisodeInfo] = []
    for entry in entries:
        video_id = entry.get("id")
        if not video_id:
            continue

        title = entry.get("title") or "Untitled"
        raw_url = entry.get("url") or entry.get("webpage_url") or f"https://www.youtube.com/watch?v={video_id}"

        published_at = None
        # Try upload_date first (YYYYMMDD string)
        upload_date_str = entry.get("upload_date")
        if upload_date_str and len(upload_date_str) == 8:
            try:
                published_at = datetime(
                    int(upload_date_str[:4]),
                    int(upload_date_str[4:6]),
                    int(upload_date_str[6:8]),
                    tzinfo=timezone.utc,
                )
            except (ValueError, TypeError):
                pass
        # Fallback to timestamp (Unix epoch from approximate_date)
        if published_at is None:
            ts = entry.get("timestamp") or entry.get("release_timestamp")
            if ts is not None:
                try:
                    published_at = datetime.fromtimestamp(int(ts), tz=timezone.utc)
                except (ValueError, TypeError, OSError):
                    pass

        episodes.append(
            EpisodeInfo(
                episode_id=video_id,
                title=title,
                published_at=published_at,
                url=raw_url,
                source="youtube_backfill",
            )
        )

    # Sort newest first
    episodes.sort(key=lambda e: e.published_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
    return episodes
