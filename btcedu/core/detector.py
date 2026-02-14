import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.schemas import EpisodeInfo
from btcedu.services.feed_service import (
    fetch_channel_videos_ytdlp,
    fetch_feed,
    parse_feed,
)

logger = logging.getLogger(__name__)


@dataclass
class DetectResult:
    """Summary of a detection run."""

    found: int = 0
    new: int = 0
    total: int = 0


def detect_episodes(session: Session, settings: Settings) -> DetectResult:
    """Fetch feed, parse episodes, insert new ones into DB.

    Idempotent: episodes with existing episode_id are skipped.

    Returns:
        DetectResult with counts.
    """
    feed_url = settings.rss_url
    if not feed_url:
        raise ValueError(
            "No feed URL configured. Set PODCAST_YOUTUBE_CHANNEL_ID or PODCAST_RSS_URL."
        )

    feed_content = fetch_feed(feed_url)
    episodes = parse_feed(feed_content, settings.source_type)

    result = DetectResult(found=len(episodes))

    existing_ids = {row[0] for row in session.query(Episode.episode_id).all()}

    for ep_info in episodes:
        if ep_info.episode_id in existing_ids:
            continue
        episode = Episode(
            episode_id=ep_info.episode_id,
            source=ep_info.source,
            title=ep_info.title,
            url=ep_info.url,
            published_at=ep_info.published_at,
            status=EpisodeStatus.NEW,
        )
        session.add(episode)
        result.new += 1

    session.commit()
    result.total = session.query(Episode).count()
    return result


def detect_from_content(session: Session, feed_content: str, source_type: str) -> DetectResult:
    """Detect episodes from already-fetched feed content.

    Useful for testing without network access.
    """
    episodes = parse_feed(feed_content, source_type)
    result = DetectResult(found=len(episodes))

    existing_ids = {row[0] for row in session.query(Episode.episode_id).all()}

    for ep_info in episodes:
        if ep_info.episode_id in existing_ids:
            continue
        episode = Episode(
            episode_id=ep_info.episode_id,
            source=ep_info.source,
            title=ep_info.title,
            url=ep_info.url,
            published_at=ep_info.published_at,
            status=EpisodeStatus.NEW,
        )
        session.add(episode)
        result.new += 1

    session.commit()
    result.total = session.query(Episode).count()
    return result


def backfill_episodes(
    session: Session,
    settings: Settings,
    *,
    max_count: int | None = None,
    since: date | None = None,
    until: date | None = None,
    dry_run: bool = False,
) -> DetectResult:
    """Import full channel history via yt-dlp.

    Idempotent: episodes already in DB are skipped.
    Does not modify existing episode rows.

    Args:
        session: DB session.
        settings: Application settings (needs podcast_youtube_channel_id).
        max_count: Maximum number of new episodes to insert.
        since: Only include videos published on or after this date.
        until: Only include videos published on or before this date.
        dry_run: If True, log what would be inserted but don't commit.

    Returns:
        DetectResult with counts.
    """
    channel_id = settings.podcast_youtube_channel_id
    if not channel_id:
        raise ValueError("No YouTube channel ID configured. Set PODCAST_YOUTUBE_CHANNEL_ID.")

    all_videos = fetch_channel_videos_ytdlp(channel_id)
    result = DetectResult(found=len(all_videos))

    # Apply date filters
    filtered: list[EpisodeInfo] = []
    for ep in all_videos:
        if ep.published_at:
            ep_date = ep.published_at.date()
            if since and ep_date < since:
                continue
            if until and ep_date > until:
                continue
        elif since or until:
            # No date available, skip when date filters are active
            continue
        filtered.append(ep)

    existing_ids = {row[0] for row in session.query(Episode.episode_id).all()}

    inserted = 0
    for ep_info in filtered:
        if ep_info.episode_id in existing_ids:
            continue
        if max_count is not None and inserted >= max_count:
            break

        if dry_run:
            pub = ep_info.published_at.strftime("%Y-%m-%d") if ep_info.published_at else "unknown"
            logger.info(
                "[dry-run] Would insert: %s  %s  (%s)", ep_info.episode_id, ep_info.title, pub
            )
        else:
            episode = Episode(
                episode_id=ep_info.episode_id,
                source=ep_info.source,
                title=ep_info.title,
                url=ep_info.url,
                published_at=ep_info.published_at,
                status=EpisodeStatus.NEW,
            )
            session.add(episode)
        inserted += 1

    if not dry_run:
        session.commit()

    result.new = inserted
    result.total = session.query(Episode).count()
    return result


def download_episode(
    session: Session,
    episode_id: str,
    settings: Settings,
    force: bool = False,
) -> str:
    """Download audio for a specific episode.

    Args:
        session: DB session.
        episode_id: The episode's unique string ID.
        settings: Application settings.
        force: If True, re-download even if file exists.

    Returns:
        Path to the downloaded audio file.

    Raises:
        ValueError: If episode not found in DB.
        RuntimeError: If download fails.
    """
    from btcedu.services.download_service import download_audio

    episode = session.query(Episode).filter(Episode.episode_id == episode_id).first()
    if not episode:
        raise ValueError(f"Episode not found: {episode_id}")

    output_dir = str(Path(settings.raw_data_dir) / episode_id)

    # Check if already downloaded
    if episode.audio_path and not force:
        audio_file = Path(episode.audio_path)
        if audio_file.exists():
            logger.info("Already downloaded: %s", episode.audio_path)
            return episode.audio_path

    audio_path = download_audio(
        url=episode.url,
        output_dir=output_dir,
        audio_format=settings.audio_format,
    )

    episode.audio_path = audio_path
    episode.status = EpisodeStatus.DOWNLOADED
    session.commit()

    return audio_path
