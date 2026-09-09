import logging
import re
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


def _resolve_title_filter(settings: Settings) -> re.Pattern | None:
    """Compile the ingest title-include regex for the active content profile.

    Returns None when no profile-level filter is configured (default behaviour:
    ingest everything). Used to restrict e.g. the tagesschau channel to the
    20:00 Uhr broadcast only.
    """
    profile_name = getattr(settings, "default_content_profile", None)
    if not profile_name:
        return None
    try:
        from btcedu.profiles import get_registry

        profile = get_registry(settings).get(profile_name)
    except Exception:
        return None

    pattern = profile.title_include_pattern()
    if not pattern:
        return None
    try:
        return re.compile(pattern)
    except re.error:
        logger.warning(
            "Invalid ingest title_include pattern for profile %s: %r", profile_name, pattern
        )
        return None


def _resolve_profile(settings: Settings, profile_name: str | None = None):
    """Load the active content profile, or ``None`` when it cannot be resolved.

    Profile lookup must never be the reason detection fails, so every error path
    degrades to "no profile-specific behaviour".
    """
    name = profile_name or getattr(settings, "default_content_profile", None)
    if not name:
        return None
    try:
        from btcedu.profiles import get_registry

        return get_registry(settings).get(name)
    except Exception:
        logger.debug("cannot resolve content profile %r", name, exc_info=True)
        return None


def _local_recorder_settings(settings: Settings, profile_name: str | None = None) -> dict:
    """The active profile's ``ingest.local_recorder`` config, or ``{}``."""
    profile = _resolve_profile(settings, profile_name)
    if profile is None:
        return {}
    try:
        return profile.local_recorder_config()
    except AttributeError:
        return {}


def _broadcast_day_from_title(title: str) -> date | None:
    """Extract the broadcast date from a tagesschau title.

    The YouTube title carries the broadcast date (``..., 06.08.2026``) while its
    publication timestamp is the upload time, which can fall on the following
    day. Deduplicating against the local recording therefore has to key on the
    date in the title, not on ``published_at``.
    """
    match = re.search(r"(\d{1,2})[.\-/](\d{1,2})[.\-/](\d{4})", title or "")
    if not match:
        return None
    day, month, year = (int(part) for part in match.groups())
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _broadcast_edition_from_title(title: str) -> str | None:
    """Return a stable edition identifier only when the title states one."""
    match = re.search(r"(\d{1,2})[:.](\d{2})\s*Uhr", title or "", re.IGNORECASE)
    if match:
        return f"{int(match.group(1)):02d}{int(match.group(2)):02d}"
    if re.search(r"\b100\s+Sekunden\b", title or "", re.IGNORECASE):
        return "100s"
    return None


def _broadcast_edition_from_slug(slug: str) -> str | None:
    """Recover an explicit HHMM edition from a recorder slug."""
    match = re.search(r"_(\d{4})$", slug or "")
    return match.group(1) if match else None


def _broadcast_key(episode_id: str, title: str) -> tuple[date, str] | None:
    """Identify one edition without guessing from its upload timestamp."""
    day = _broadcast_day_from_slug(episode_id) or _broadcast_day_from_title(title)
    edition = _broadcast_edition_from_slug(episode_id) or _broadcast_edition_from_title(title)
    if day is None or edition is None:
        return None
    return day, edition


def _resolve_channel_id(
    session: Session,
    settings: Settings,
    explicit_channel_id: str | None = None,
    profile_name: str | None = None,
) -> str | None:
    """Resolve the channel_id for new episodes.

    Priority:
    1. Explicitly passed channel_id
    2. Channel configured for this content profile
    3. Look up Channel by youtube_channel_id matching settings
    4. Look up Channel by rss_url matching settings
    5. None (no channel assigned)

    Step 2 exists because steps 3 and 4 read the *global* podcast settings,
    which name exactly one channel. Every episode detected outside the
    per-channel path therefore landed on that one channel regardless of which
    profile produced it -- the local recorder's tagesschau broadcasts were
    filed under the Bitcoin podcast, and the dashboard's channel filter then
    showed one of ten. The profile is the more specific fact whenever it is
    known, so it is asked first.
    """
    if explicit_channel_id:
        return explicit_channel_id

    from btcedu.models.channel import Channel

    if profile_name:
        ch = (
            session.query(Channel)
            .filter(Channel.content_profile == profile_name)
            .order_by(Channel.id)
            .first()
        )
        if ch:
            return ch.channel_id

    if settings.podcast_youtube_channel_id:
        ch = (
            session.query(Channel)
            .filter(Channel.youtube_channel_id == settings.podcast_youtube_channel_id)
            .first()
        )
        if ch:
            return ch.channel_id

    if settings.podcast_rss_url:
        ch = session.query(Channel).filter(Channel.rss_url == settings.podcast_rss_url).first()
        if ch:
            return ch.channel_id

    return None


@dataclass
class DetectResult:
    """Summary of a detection run."""

    found: int = 0
    new: int = 0
    total: int = 0


def detect_episodes(
    session: Session,
    settings: Settings,
    *,
    channel_id: str | None = None,
    feed_url: str | None = None,
) -> DetectResult:
    """Fetch feed, parse episodes, insert new ones into DB.

    Idempotent: episodes with existing episode_id are skipped.

    Args:
        session: DB session.
        settings: Application settings.
        channel_id: Optional channel_id to assign. If not given, auto-resolved
                    from Channel table via settings.
        feed_url: Override the feed URL (bypasses settings.rss_url).

    Returns:
        DetectResult with counts.
    """
    from btcedu.services.failover_service import failover_feed_detection_enabled

    if not failover_feed_detection_enabled(settings):
        logger.info("Feed detection skipped on the secondary failover node.")
        return DetectResult(total=session.query(Episode).count())

    feed_url = feed_url or settings.rss_url
    if not feed_url:
        raise ValueError(
            "No feed URL configured. Set PODCAST_YOUTUBE_CHANNEL_ID or PODCAST_RSS_URL."
        )

    from btcedu.core.retention import prune_expired_episodes, retention_cutoff

    retention = prune_expired_episodes(session, settings)
    if retention.deleted or retention.protected or retention.blocked:
        logger.info(
            "Episode retention deleted %d expired episode(s); protected %d episode(s); "
            "blocked %d episode(s)",
            retention.deleted,
            retention.protected,
            retention.blocked,
        )

    profile_name = settings.default_content_profile
    resolved_channel_id = _resolve_channel_id(
        session,
        settings,
        channel_id,
        profile_name=profile_name,
    )

    feed_content = fetch_feed(feed_url)
    episodes = parse_feed(feed_content, settings.source_type)

    cutoff = retention_cutoff(
        settings,
        profile_name=settings.default_content_profile,
    )
    before_retention = len(episodes)
    episodes = [
        ep
        for ep in episodes
        if cutoff is None or ep.published_at is None or ep.published_at >= cutoff
    ]
    skipped_expired = before_retention - len(episodes)
    if skipped_expired:
        logger.info("Retention filter skipped %d expired feed episode(s)", skipped_expired)

    title_filter = _resolve_title_filter(settings)
    if title_filter is not None:
        before = len(episodes)
        episodes = [ep for ep in episodes if title_filter.search(ep.title or "")]
        skipped = before - len(episodes)
        if skipped:
            logger.info("Title filter skipped %d/%d episodes", skipped, before)

    # A re-upload can have a new provider id and upload timestamp. Deduplicate
    # only an explicitly identified edition inside this profile/channel stream.
    # Unknown editions are deliberately retained rather than guessed.
    if title_filter is not None:
        known_keys = _stored_broadcast_keys(
            session,
            profile_name=profile_name,
            channel_id=resolved_channel_id,
            title_filter=title_filter,
        )
        kept: list[EpisodeInfo] = []
        already_known = 0
        for ep in episodes:
            key = _broadcast_key(ep.episode_id, ep.title or "")
            if key is not None and key in known_keys:
                already_known += 1
                continue
            if key is not None:
                known_keys.add(key)
            kept.append(ep)
        episodes = kept
        if already_known:
            logger.info(
                "Skipped %d feed episode(s) whose broadcast edition is already stored",
                already_known,
            )

    result = DetectResult(found=len(episodes))

    existing_ids = {row[0] for row in session.query(Episode.episode_id).all()}

    for ep_info in episodes:
        if ep_info.episode_id in existing_ids:
            continue
        episode = Episode(
            episode_id=ep_info.episode_id,
            channel_id=resolved_channel_id,
            source=ep_info.source,
            title=ep_info.title,
            url=ep_info.url,
            published_at=ep_info.published_at,
            status=EpisodeStatus.NEW,
            content_profile=settings.default_content_profile,
            pipeline_version=settings.pipeline_version,
        )
        session.add(episode)
        result.new += 1

    session.commit()
    result.total = session.query(Episode).count()
    return result


def _feed_broadcast_day(ep_info: EpisodeInfo) -> date | None:
    """The broadcast day a feed entry refers to.

    Prefers the date in the title, because a broadcast uploaded after midnight
    carries the next day's ``published_at`` and would otherwise not match the
    local recording it duplicates.
    """
    day = _broadcast_day_from_title(ep_info.title or "")
    if day is not None:
        return day
    return ep_info.published_at.date() if ep_info.published_at else None


def _stored_broadcast_keys(
    session: Session,
    *,
    profile_name: str | None,
    channel_id: str | None,
    title_filter: re.Pattern | None,
    local: bool | None = None,
) -> set[tuple[date, str]]:
    """Return explicit edition keys belonging to the same ingest stream."""
    from btcedu.services.local_recorder_service import SOURCE_NAME

    query = session.query(
        Episode.episode_id,
        Episode.title,
        Episode.source,
        Episode.content_profile,
        Episode.channel_id,
    )
    if local is True:
        query = query.filter(Episode.source == SOURCE_NAME)
    elif local is False:
        query = query.filter(Episode.source != SOURCE_NAME)

    keys: set[tuple[date, str]] = set()
    for episode_id, title, _source, stored_profile, stored_channel in query.all():
        if channel_id and stored_channel and channel_id != stored_channel:
            continue
        if profile_name and stored_profile and profile_name != stored_profile:
            continue
        same_stream = bool(
            (profile_name and stored_profile == profile_name)
            or (channel_id and stored_channel == channel_id)
        )
        same_programme = bool(title_filter and title_filter.search(title or ""))
        if not same_stream and not same_programme:
            continue
        key = _broadcast_key(episode_id, title or "")
        if key is not None:
            keys.add(key)
    return keys


def _broadcast_day_from_slug(slug: str) -> date | None:
    """Recover the broadcast day from a recorder slug (``..._YYYY-MM-DD_2000``)."""
    match = re.search(r"(\d{4}-\d{2}-\d{2})", slug or "")
    if not match:
        return None
    try:
        return date.fromisoformat(match.group(1))
    except ValueError:
        return None


def _report_incomplete_recording(settings: Settings, recording) -> None:
    """Raise the alarm when the recorder says its cut lost the forecast.

    The recorder checks its own cut against the closing weather forecast and
    writes the verdict to its metadata, but until it is read here nobody sees
    it. This failure is silent by nature: a truncated bulletin still yields a
    coherent transcript, so every downstream stage and every review gate passes
    and the finished video is simply missing its last chapter.

    Only ``truncated`` is reported. An edition that genuinely had no forecast is
    not a defect — the 2026-08-12 playout dropped it — and alarming about it
    would train the reader to ignore the message that matters.

    Never raises: a broken notifier must not cost the ingest.
    """
    weather = getattr(recording, "weather", None)
    if weather is None or not weather.truncated:
        return

    logger.error(
        "recording %s is incomplete: the weather forecast continues past the cut (%s)",
        recording.slug,
        weather.evidence,
    )
    try:
        from btcedu.services.notify_service import send_notification

        send_notification(
            settings,
            "\u26a0\ufe0f Aufnahme unvollständig\n"
            f"Episode: {recording.slug}\n"
            "Der Wetterbericht läuft über den Schnitt hinaus weiter — "
            "die Aufnahme wurde zu früh beendet und das Video würde ohne "
            "Wetterabschnitt enden.\n"
            f"{weather.evidence}",
        )
    except Exception:  # pragma: no cover - defensive
        logger.warning("could not send the incomplete-recording notification", exc_info=True)


def _recording_allowed_for_node(settings: Settings, recording) -> tuple[bool, str]:
    """Whether a local recording may be ingested on this node role."""
    if not getattr(settings, "failover_enabled", False):
        return True, ""
    if str(getattr(settings, "failover_node_role", "") or "").strip().lower() != "secondary":
        return True, ""
    if recording.node_role != "secondary":
        return False, "metadata.node_role must be 'secondary'"
    if recording.source_kind not in {"vod", "mediathek"}:
        return False, "metadata.source_kind must identify a Mediathek VOD"
    provider = str(recording.metadata.get("provider") or "").strip().lower()
    provenance = recording.provenance
    provenance_source = (
        str(provenance.get("source") or "").strip().lower()
        if isinstance(provenance, dict)
        else ""
    )
    if provider != "ard_mediathek" and provenance_source not in {
        "ard_mediathek",
        "ard-mediathek",
    }:
        return False, "metadata must identify ard_mediathek as the VOD source"
    if provenance in (None, "", {}, []):
        return False, "metadata.provenance is required on the secondary node"
    return True, ""


def detect_local_recordings(
    session: Session,
    settings: Settings,
    *,
    profile_name: str | None = None,
    channel_id: str | None = None,
) -> DetectResult:
    """Ingest finished recordings produced by the local recorder.

    This is the preferred source: the file is ready about twenty minutes after
    the broadcast, well before the same broadcast is uploaded to YouTube. When
    the recorder is not configured, not installed, or has nothing finished yet,
    this is a no-op and the feed path takes over unchanged.

    Idempotent: a recording whose ``episode_id`` is already stored is skipped,
    so the ten-minute timer can call this as often as it likes.
    """
    profile_name = profile_name or getattr(settings, "default_content_profile", None)
    config = _local_recorder_settings(settings, profile_name)
    if not config:
        return DetectResult()

    from btcedu.core.retention import retention_cutoff
    from btcedu.services.local_recorder_service import scan_recordings

    cutoff = retention_cutoff(settings, profile_name=profile_name)
    since = cutoff.date() if cutoff is not None else None

    try:
        recordings = scan_recordings(config["base_dir"], since=since)
    except OSError:
        # An unreadable or unmounted recorder directory must not stop detection;
        # the feed fallback exists precisely for this case.
        logger.warning("cannot scan local recorder directory", exc_info=True)
        return DetectResult()

    filtered_recordings = []
    for recording in recordings:
        allowed, reason = _recording_allowed_for_node(settings, recording)
        if allowed:
            filtered_recordings.append(recording)
        else:
            logger.warning("Skipping local recording %s: %s", recording.slug, reason)
    recordings = filtered_recordings

    episodes = [rec.to_episode_info() for rec in recordings]
    result = DetectResult(found=len(episodes))
    if not episodes:
        logger.debug("no finished local recordings found")
        return result

    title_filter = _resolve_title_filter(
        settings.model_copy(update={"default_content_profile": profile_name})
        if profile_name
        else settings
    )
    if title_filter is not None:
        episodes = [ep for ep in episodes if title_filter.search(ep.title or "")]

    # The mirror image of the feed-side filter: a broadcast already ingested
    # from YouTube must not be picked up again from disk. Without this, every
    # broadcast that the feed happened to deliver first - including all of them
    # from before the recorder existed - would be transcribed, translated,
    # voiced and rendered a second time.
    if config.get("supersedes_feed", True):
        feed_keys = _stored_broadcast_keys(
            session,
            profile_name=profile_name,
            channel_id=_resolve_channel_id(
                session,
                settings,
                channel_id,
                profile_name=profile_name,
            ),
            title_filter=title_filter,
            local=False,
        )
        if feed_keys:
            before = len(episodes)
            episodes = [
                ep
                for ep in episodes
                if (key := _broadcast_key(ep.episode_id, ep.title or "")) is None
                or key not in feed_keys
            ]
            skipped = before - len(episodes)
            if skipped:
                logger.info("Skipped %d local recording(s) already ingested from the feed", skipped)

    resolved_channel_id = _resolve_channel_id(
        session, settings, channel_id, profile_name=profile_name
    )
    existing_ids = {row[0] for row in session.query(Episode.episode_id).all()}
    by_slug = {rec.slug: rec for rec in recordings}

    for ep_info in episodes:
        if ep_info.episode_id in existing_ids:
            continue
        episode = Episode(
            episode_id=ep_info.episode_id,
            channel_id=resolved_channel_id,
            source=ep_info.source,
            title=ep_info.title,
            url=ep_info.url,
            published_at=ep_info.published_at,
            status=EpisodeStatus.NEW,
            content_profile=profile_name or settings.default_content_profile,
            pipeline_version=settings.pipeline_version,
        )
        session.add(episode)
        result.new += 1
        logger.info("Ingested local recording %s (%s)", ep_info.episode_id, ep_info.url)
        recording = by_slug.get(ep_info.episode_id)
        if recording is not None:
            _report_incomplete_recording(settings, recording)

    session.commit()
    result.total = session.query(Episode).count()
    return result


def detect_all_active_channels(session: Session, settings: Settings) -> DetectResult:
    """Detect new episodes from every active channel using its own profile.

    Iterates all ``is_active`` channels, running :func:`detect_episodes` for
    each with the channel's configured ``content_profile`` (which applies that
    profile's title-include filter — e.g. the tagesschau 20:00 Uhr broadcast).
    Also detects from the default ``settings.rss_url`` when it is not already
    covered by a channel. Aggregates the per-channel counts.
    """
    from btcedu.models.channel import Channel

    combined = DetectResult()
    channels = session.query(Channel).filter(Channel.is_active.is_(True)).all()
    for ch in channels:
        if not ch.rss_url:
            continue
        profile = ch.content_profile or settings.default_content_profile
        ch_settings = settings.model_copy(update={"default_content_profile": profile})
        try:
            result = detect_episodes(
                session,
                ch_settings,
                channel_id=ch.channel_id,
                feed_url=ch.rss_url,
            )
            combined.found += result.found
            combined.new += result.new
        except Exception:
            logger.exception("Detect failed for channel %s", ch.name)

    covered_urls = {ch.rss_url for ch in channels if ch.rss_url}
    if settings.rss_url and settings.rss_url not in covered_urls:
        result = detect_episodes(session, settings)
        combined.found += result.found
        combined.new += result.new

    combined.total = session.query(Episode).count()
    return combined


def detect_from_content(
    session: Session, feed_content: str, source_type: str, *, channel_id: str | None = None
) -> DetectResult:
    """Detect episodes from already-fetched feed content.

    Useful for testing without network access.

    Args:
        session: DB session.
        feed_content: Raw feed XML content.
        source_type: Feed source type.
        channel_id: Optional channel_id to assign to new episodes.
    """
    episodes = parse_feed(feed_content, source_type)
    result = DetectResult(found=len(episodes))

    existing_ids = {row[0] for row in session.query(Episode.episode_id).all()}

    for ep_info in episodes:
        if ep_info.episode_id in existing_ids:
            continue
        episode = Episode(
            episode_id=ep_info.episode_id,
            channel_id=channel_id,
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
    channel_id: str | None = None,
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
        channel_id: Optional channel_id to assign. If not given, auto-resolved.

    Returns:
        DetectResult with counts.
    """
    yt_channel_id = settings.podcast_youtube_channel_id
    if not yt_channel_id:
        raise ValueError("No YouTube channel ID configured. Set PODCAST_YOUTUBE_CHANNEL_ID.")

    profile_name = settings.default_content_profile
    resolved_channel_id = _resolve_channel_id(
        session,
        settings,
        channel_id,
        profile_name=profile_name,
    )

    all_videos = fetch_channel_videos_ytdlp(yt_channel_id)
    result = DetectResult(found=len(all_videos))

    title_filter = _resolve_title_filter(settings)
    if title_filter is not None:
        all_videos = [ep for ep in all_videos if title_filter.search(ep.title or "")]

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
                channel_id=resolved_channel_id,
                source=ep_info.source,
                title=ep_info.title,
                url=ep_info.url,
                published_at=ep_info.published_at,
                status=EpisodeStatus.NEW,
                content_profile=profile_name,
                pipeline_version=settings.pipeline_version,
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

    from btcedu.services.local_recorder_service import is_local_source

    if is_local_source(episode.source):
        # The file is already on disk. Running yt-dlp against a filesystem path
        # would fail, and re-downloading the broadcast from YouTube would throw
        # away the very time this source exists to save.
        audio_path = _ingest_local_recording(episode, output_dir, settings, force=force)
    else:
        audio_path = download_audio(
            url=episode.url,
            output_dir=output_dir,
            audio_format=settings.audio_format,
        )

    episode.audio_path = audio_path
    episode.status = EpisodeStatus.DOWNLOADED
    session.commit()

    # Optionally download the video source for frame extraction.
    # Trigger when globally enabled OR when the episode's content profile needs
    # video-derived images (imagegen provider == gemini_frame_edit, e.g.
    # tagesschau_tr), so news episodes always get their source video.
    # Locally recorded episodes already have their video in place, linked by
    # _ingest_local_recording; downloading it again from YouTube would defeat
    # the purpose of using the local source.
    needs_video = settings.frame_extraction_enabled or _profile_requires_video(episode, settings)
    if needs_video and not is_local_source(episode.source):
        _try_download_video(episode.url, output_dir, settings)

    return audio_path


def _ingest_local_recording(
    episode: Episode, output_dir: str, settings: Settings, *, force: bool = False
) -> str:
    """Prepare a locally recorded broadcast for the pipeline.

    Extracts the audio track and makes the video available under the name the
    downstream stages expect, so everything after ``download`` is identical for
    locally recorded and downloaded episodes.

    The video is hard-linked rather than copied: a broadcast is roughly 350 MB
    and the recorder's copy is retained anyway, so copying would double the
    storage for no benefit. A hard link also cannot go stale the way a symlink
    would if the pipeline later moved the file.

    ``force`` re-does both. It has to: a recording that was published with a bad
    cut is corrected by writing a *new* file in its place, and the existing hard
    link still points at the old inode — the bytes of the bad cut, kept alive by
    that very link. Reusing either one would re-run the whole pipeline against
    the material the re-run exists to replace.
    """
    import json
    from datetime import UTC, datetime

    from btcedu.services.local_recorder_service import (
        extract_audio,
        metadata_for_video,
        resolve_local_video,
    )

    config = _local_recorder_settings(settings, getattr(episode, "content_profile", None))
    base_dir = config.get("base_dir")
    if not base_dir:
        raise RuntimeError(
            f"episode {episode.episode_id} came from the local recorder, but the "
            "profile no longer configures ingest.local_recorder.base_dir"
        )

    source_video = resolve_local_video(episode.url, base_dir)
    recorder_metadata = metadata_for_video(source_video)
    out_path = Path(output_dir)
    out_path.mkdir(parents=True, exist_ok=True)

    audio_path = extract_audio(
        source_video,
        out_path / f"audio.{settings.audio_format}",
        audio_format=settings.audio_format,
        force=force,
    )

    video_path = out_path / "video.mp4"
    if force:
        video_path.unlink(missing_ok=True)
    if not video_path.exists():
        try:
            video_path.hardlink_to(source_video)
        except OSError:
            # Different filesystems (the recorder writes to a separate disk)
            # cannot be hard-linked; a symlink keeps this working without
            # duplicating hundreds of megabytes.
            video_path.symlink_to(source_video)

    meta_path = out_path / "video_meta.json"
    meta_path.write_text(
        json.dumps(
            {
                "video_path": str(video_path),
                "source": "local_recorder",
                "source_video": str(source_video),
                "node_role": recorder_metadata.get("node_role"),
                "source_kind": recorder_metadata.get("source_kind"),
                "provenance": recorder_metadata.get("provenance"),
                "downloaded_at": datetime.now(UTC).isoformat(),
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    logger.info("Prepared local recording %s -> %s", source_video, audio_path)
    return str(audio_path)


def _profile_requires_video(episode: Episode, settings: Settings) -> bool:
    """True if the episode's content profile needs the source video for frames."""
    try:
        from btcedu.profiles import get_registry

        profile = get_registry(settings).get(getattr(episode, "content_profile", "bitcoin_podcast"))
        imagegen_cfg = profile.stage_config.get("imagegen", {}) or {}
        return imagegen_cfg.get("provider") == "gemini_frame_edit"
    except Exception:
        return False


def _try_download_video(url: str, output_dir: str, settings: Settings) -> None:
    """Best-effort video download for frame extraction (non-fatal)."""
    try:
        from btcedu.services.download_service import download_video

        video_path = download_video(
            url=url,
            output_dir=output_dir,
            max_height=settings.frame_extract_video_height,
        )
        # Write metadata file so frame_extractor can locate the video later
        import json
        from datetime import UTC, datetime

        meta_path = Path(output_dir) / "video_meta.json"
        meta_path.write_text(
            json.dumps(
                {"video_path": video_path, "downloaded_at": datetime.now(UTC).isoformat()},
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.warning("Video download failed for %s: %s (writing failure marker)", url, exc)
        # Persist a marker so downstream stages can detect the missing video
        # instead of silently producing empty manifests.
        try:
            import json
            from datetime import UTC, datetime

            marker = Path(output_dir) / "video_download_failed.json"
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps(
                    {
                        "url": url,
                        "error": str(exc),
                        "failed_at": datetime.now(UTC).isoformat(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        except Exception:
            logger.exception("Could not write video_download_failed.json for %s", url)
