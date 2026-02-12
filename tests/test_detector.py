"""Phase 2 tests: feed parsing, detection (idempotent), download, backfill."""
import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.core.detector import backfill_episodes, detect_from_content, download_episode
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.schemas import EpisodeInfo
from btcedu.services.feed_service import (
    _make_fallback_id,
    fetch_channel_videos_ytdlp,
    parse_feed,
    parse_rss,
    parse_youtube_rss,
)

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE_FEED = (FIXTURES / "sample_youtube_feed.xml").read_text()


# ── Feed parsing: YouTube RSS ──────────────────────────────────────


class TestParseYoutubeRSS:
    def test_returns_correct_count(self):
        episodes = parse_youtube_rss(SAMPLE_FEED)
        assert len(episodes) == 3

    def test_extracts_video_id(self):
        episodes = parse_youtube_rss(SAMPLE_FEED)
        ids = [ep.episode_id for ep in episodes]
        assert "dQw4w9WgXcQ" in ids
        assert "xYz789AbCdE" in ids
        assert "aBcDeFgHiJk" in ids

    def test_extracts_title(self):
        episodes = parse_youtube_rss(SAMPLE_FEED)
        ep = next(e for e in episodes if e.episode_id == "dQw4w9WgXcQ")
        assert "Bitcoin und die Zukunft des Geldes" in ep.title

    def test_extracts_url(self):
        episodes = parse_youtube_rss(SAMPLE_FEED)
        ep = next(e for e in episodes if e.episode_id == "dQw4w9WgXcQ")
        assert ep.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_extracts_published_date(self):
        episodes = parse_youtube_rss(SAMPLE_FEED)
        ep = next(e for e in episodes if e.episode_id == "dQw4w9WgXcQ")
        assert ep.published_at is not None
        assert ep.published_at.year == 2024
        assert ep.published_at.month == 6
        assert ep.published_at.day == 15

    def test_source_is_youtube_rss(self):
        episodes = parse_youtube_rss(SAMPLE_FEED)
        for ep in episodes:
            assert ep.source == "youtube_rss"

    def test_empty_feed(self):
        empty = '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        episodes = parse_youtube_rss(empty)
        assert episodes == []


# ── Feed parsing: generic RSS ──────────────────────────────────────


GENERIC_RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0">
  <channel>
    <title>Test Podcast</title>
    <item>
      <title>Episode One</title>
      <link>https://example.com/ep1</link>
      <pubDate>Mon, 10 Jun 2024 10:00:00 +0000</pubDate>
    </item>
    <item>
      <title>Episode Two</title>
      <link>https://example.com/ep2</link>
      <pubDate>Mon, 03 Jun 2024 10:00:00 +0000</pubDate>
    </item>
  </channel>
</rss>"""


class TestParseGenericRSS:
    def test_returns_correct_count(self):
        episodes = parse_rss(GENERIC_RSS)
        assert len(episodes) == 2

    def test_uses_sha1_fallback_id(self):
        episodes = parse_rss(GENERIC_RSS)
        expected_id = hashlib.sha1(b"https://example.com/ep1").hexdigest()[:12]
        assert episodes[0].episode_id == expected_id

    def test_source_is_rss(self):
        episodes = parse_rss(GENERIC_RSS)
        for ep in episodes:
            assert ep.source == "rss"

    def test_extracts_title(self):
        episodes = parse_rss(GENERIC_RSS)
        assert episodes[0].title == "Episode One"


# ── parse_feed dispatcher ──────────────────────────────────────────


class TestParseFeed:
    def test_dispatches_youtube_rss(self):
        episodes = parse_feed(SAMPLE_FEED, "youtube_rss")
        assert len(episodes) == 3
        assert episodes[0].source == "youtube_rss"

    def test_dispatches_generic_rss(self):
        episodes = parse_feed(GENERIC_RSS, "rss")
        assert len(episodes) == 2
        assert episodes[0].source == "rss"


# ── Fallback ID helper ─────────────────────────────────────────────


class TestFallbackId:
    def test_deterministic(self):
        id1 = _make_fallback_id("https://example.com/ep1")
        id2 = _make_fallback_id("https://example.com/ep1")
        assert id1 == id2

    def test_length_12(self):
        fid = _make_fallback_id("https://example.com/ep1")
        assert len(fid) == 12

    def test_different_urls_different_ids(self):
        id1 = _make_fallback_id("https://example.com/ep1")
        id2 = _make_fallback_id("https://example.com/ep2")
        assert id1 != id2


# ── Detection: idempotent DB inserts ───────────────────────────────


class TestDetectFromContent:
    def test_inserts_new_episodes(self, db_session):
        result = detect_from_content(db_session, SAMPLE_FEED, "youtube_rss")
        assert result.found == 3
        assert result.new == 3
        assert result.total == 3
        assert db_session.query(Episode).count() == 3

    def test_idempotent_second_run(self, db_session):
        detect_from_content(db_session, SAMPLE_FEED, "youtube_rss")
        result = detect_from_content(db_session, SAMPLE_FEED, "youtube_rss")
        assert result.found == 3
        assert result.new == 0
        assert result.total == 3
        assert db_session.query(Episode).count() == 3

    def test_new_episodes_have_status_new(self, db_session):
        detect_from_content(db_session, SAMPLE_FEED, "youtube_rss")
        episodes = db_session.query(Episode).all()
        for ep in episodes:
            assert ep.status == EpisodeStatus.NEW

    def test_stores_correct_fields(self, db_session):
        detect_from_content(db_session, SAMPLE_FEED, "youtube_rss")
        ep = (
            db_session.query(Episode)
            .filter(Episode.episode_id == "dQw4w9WgXcQ")
            .first()
        )
        assert ep is not None
        assert "Bitcoin und die Zukunft des Geldes" in ep.title
        assert ep.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        assert ep.source == "youtube_rss"
        assert ep.published_at is not None

    def test_incremental_detection(self, db_session):
        """Detect 3, then add 1 new entry — only the new one is inserted."""
        detect_from_content(db_session, SAMPLE_FEED, "youtube_rss")

        # Feed with one extra episode
        extra_feed = SAMPLE_FEED.replace(
            "</feed>",
            """
  <entry>
    <id>yt:video:newEpisode01</id>
    <yt:videoId>newEpisode01</yt:videoId>
    <title>Neue Episode</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=newEpisode01"/>
    <published>2024-06-22T10:00:00+00:00</published>
  </entry>
</feed>""",
        )
        result = detect_from_content(db_session, extra_feed, "youtube_rss")
        assert result.found == 4
        assert result.new == 1
        assert result.total == 4


# ── Download: correct path + force flag ────────────────────────────


class TestDownloadEpisode:
    def _make_settings(self, tmp_path):
        from btcedu.config import Settings

        return Settings(
            raw_data_dir=str(tmp_path / "raw"),
            audio_format="m4a",
        )

    def _seed_episode(self, db_session, episode_id="dQw4w9WgXcQ"):
        ep = Episode(
            episode_id=episode_id,
            source="youtube_rss",
            title="Test Episode",
            url=f"https://www.youtube.com/watch?v={episode_id}",
            status=EpisodeStatus.NEW,
        )
        db_session.add(ep)
        db_session.commit()
        return ep

    @patch("btcedu.services.download_service.download_audio")
    def test_creates_correct_path(self, mock_dl, db_session, tmp_path):
        settings = self._make_settings(tmp_path)
        self._seed_episode(db_session)

        expected_dir = str(tmp_path / "raw" / "dQw4w9WgXcQ")
        mock_dl.return_value = f"{expected_dir}/audio.m4a"

        path = download_episode(db_session, "dQw4w9WgXcQ", settings)

        mock_dl.assert_called_once_with(
            url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            output_dir=expected_dir,
            audio_format="m4a",
        )
        assert path == f"{expected_dir}/audio.m4a"

    @patch("btcedu.services.download_service.download_audio")
    def test_updates_status_to_downloaded(self, mock_dl, db_session, tmp_path):
        settings = self._make_settings(tmp_path)
        self._seed_episode(db_session)
        mock_dl.return_value = "/some/path/audio.m4a"

        download_episode(db_session, "dQw4w9WgXcQ", settings)

        ep = db_session.query(Episode).filter(Episode.episode_id == "dQw4w9WgXcQ").first()
        assert ep.status == EpisodeStatus.DOWNLOADED
        assert ep.audio_path == "/some/path/audio.m4a"

    @patch("btcedu.services.download_service.download_audio")
    def test_skips_if_already_downloaded(self, mock_dl, db_session, tmp_path):
        settings = self._make_settings(tmp_path)
        ep = self._seed_episode(db_session)

        # Simulate already downloaded
        audio_file = tmp_path / "raw" / "dQw4w9WgXcQ" / "audio.m4a"
        audio_file.parent.mkdir(parents=True)
        audio_file.write_text("fake audio")
        ep.audio_path = str(audio_file)
        ep.status = EpisodeStatus.DOWNLOADED
        db_session.commit()

        path = download_episode(db_session, "dQw4w9WgXcQ", settings)

        mock_dl.assert_not_called()
        assert path == str(audio_file)

    @patch("btcedu.services.download_service.download_audio")
    def test_force_redownloads(self, mock_dl, db_session, tmp_path):
        settings = self._make_settings(tmp_path)
        ep = self._seed_episode(db_session)

        # Simulate already downloaded
        audio_file = tmp_path / "raw" / "dQw4w9WgXcQ" / "audio.m4a"
        audio_file.parent.mkdir(parents=True)
        audio_file.write_text("fake audio")
        ep.audio_path = str(audio_file)
        ep.status = EpisodeStatus.DOWNLOADED
        db_session.commit()

        mock_dl.return_value = str(audio_file)
        download_episode(db_session, "dQw4w9WgXcQ", settings, force=True)

        mock_dl.assert_called_once()

    def test_raises_for_unknown_episode(self, db_session, tmp_path):
        settings = self._make_settings(tmp_path)
        import pytest

        with pytest.raises(ValueError, match="Episode not found"):
            download_episode(db_session, "nonexistent", settings)


# ── yt-dlp channel listing ────────────────────────────────────────

# Minimal yt-dlp --flat-playlist -J output for testing
YTDLP_PLAYLIST_JSON = json.dumps({
    "entries": [
        {
            "id": "vid001",
            "title": "Bitcoin Grundlagen",
            "upload_date": "20240615",
            "url": "https://www.youtube.com/watch?v=vid001",
        },
        {
            "id": "vid002",
            "title": "Lightning Network erklärt",
            "timestamp": 1717200000,  # 2024-06-01 UTC (approximate_date)
            "url": "https://www.youtube.com/watch?v=vid002",
        },
        {
            "id": "vid003",
            "title": "Mining Deep Dive",
            "upload_date": "20231215",
            "url": "https://www.youtube.com/watch?v=vid003",
        },
        {
            "id": "vid004",
            "title": "Sehr altes Video",
            "url": "https://www.youtube.com/watch?v=vid004",
            # no upload_date, no timestamp
        },
    ]
})


def _make_subprocess_result(stdout="", stderr="", returncode=0):
    """Create a mock subprocess.CompletedProcess."""
    result = MagicMock()
    result.stdout = stdout
    result.stderr = stderr
    result.returncode = returncode
    return result


class TestFetchChannelVideosYtdlp:
    @patch("btcedu.services.feed_service.subprocess.run")
    def test_parses_ytdlp_json(self, mock_run):
        mock_run.return_value = _make_subprocess_result(stdout=YTDLP_PLAYLIST_JSON)

        episodes = fetch_channel_videos_ytdlp("UC_test_channel")

        assert len(episodes) == 4
        ids = [e.episode_id for e in episodes]
        assert "vid001" in ids
        assert "vid002" in ids
        assert "vid003" in ids
        assert "vid004" in ids

        # Check first episode (sorted newest first, vid001 has latest date)
        ep1 = next(e for e in episodes if e.episode_id == "vid001")
        assert ep1.title == "Bitcoin Grundlagen"
        assert ep1.published_at == datetime(2024, 6, 15, tzinfo=timezone.utc)
        assert ep1.source == "youtube_backfill"
        assert "vid001" in ep1.url

    @patch("btcedu.services.feed_service.subprocess.run")
    def test_handles_missing_upload_date(self, mock_run):
        mock_run.return_value = _make_subprocess_result(stdout=YTDLP_PLAYLIST_JSON)

        episodes = fetch_channel_videos_ytdlp("UC_test")
        ep4 = next(e for e in episodes if e.episode_id == "vid004")
        assert ep4.published_at is None

    @patch("btcedu.services.feed_service.subprocess.run")
    def test_raises_on_ytdlp_failure(self, mock_run):
        mock_run.return_value = _make_subprocess_result(
            returncode=1, stderr="ERROR: channel not found"
        )

        with pytest.raises(RuntimeError, match="yt-dlp failed"):
            fetch_channel_videos_ytdlp("UC_bad_channel")

    @patch("btcedu.services.feed_service.subprocess.run")
    def test_falls_back_to_timestamp(self, mock_run):
        """vid002 has no upload_date but has timestamp — should still get a date."""
        mock_run.return_value = _make_subprocess_result(stdout=YTDLP_PLAYLIST_JSON)

        episodes = fetch_channel_videos_ytdlp("UC_test")
        ep2 = next(e for e in episodes if e.episode_id == "vid002")
        assert ep2.published_at is not None
        assert ep2.published_at.year == 2024
        assert ep2.published_at.month == 6

    @patch("btcedu.services.feed_service.subprocess.run")
    def test_sorted_newest_first(self, mock_run):
        mock_run.return_value = _make_subprocess_result(stdout=YTDLP_PLAYLIST_JSON)

        episodes = fetch_channel_videos_ytdlp("UC_test")
        # vid001 (2024-06-15) should come before vid002 (2024-06-01) before vid003 (2023-12-15)
        dated = [e for e in episodes if e.published_at is not None]
        dates = [e.published_at for e in dated]
        assert dates == sorted(dates, reverse=True)


# ── Backfill episodes ─────────────────────────────────────────────


def _make_backfill_settings(**kwargs):
    from btcedu.config import Settings
    return Settings(podcast_youtube_channel_id="UC_test_channel", **kwargs)


class TestBackfillEpisodes:
    @patch("btcedu.core.detector.fetch_channel_videos_ytdlp")
    def test_inserts_all_videos(self, mock_fetch, db_session):
        mock_fetch.return_value = [
            EpisodeInfo(episode_id="vid001", title="Ep 1",
                        published_at=datetime(2024, 6, 15, tzinfo=timezone.utc),
                        url="https://youtube.com/watch?v=vid001", source="youtube_backfill"),
            EpisodeInfo(episode_id="vid002", title="Ep 2",
                        published_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
                        url="https://youtube.com/watch?v=vid002", source="youtube_backfill"),
        ]
        settings = _make_backfill_settings()
        result = backfill_episodes(db_session, settings)

        assert result.found == 2
        assert result.new == 2
        assert result.total == 2
        assert db_session.query(Episode).count() == 2

    @patch("btcedu.core.detector.fetch_channel_videos_ytdlp")
    def test_idempotent(self, mock_fetch, db_session):
        eps = [
            EpisodeInfo(episode_id="vid001", title="Ep 1",
                        published_at=datetime(2024, 6, 15, tzinfo=timezone.utc),
                        url="https://youtube.com/watch?v=vid001", source="youtube_backfill"),
        ]
        mock_fetch.return_value = eps
        settings = _make_backfill_settings()

        backfill_episodes(db_session, settings)
        result = backfill_episodes(db_session, settings)

        assert result.new == 0
        assert result.total == 1

    @patch("btcedu.core.detector.fetch_channel_videos_ytdlp")
    def test_since_filter(self, mock_fetch, db_session):
        mock_fetch.return_value = [
            EpisodeInfo(episode_id="new1", title="New",
                        published_at=datetime(2024, 6, 15, tzinfo=timezone.utc),
                        url="https://youtube.com/watch?v=new1", source="youtube_backfill"),
            EpisodeInfo(episode_id="old1", title="Old",
                        published_at=datetime(2023, 1, 1, tzinfo=timezone.utc),
                        url="https://youtube.com/watch?v=old1", source="youtube_backfill"),
        ]
        settings = _make_backfill_settings()
        result = backfill_episodes(db_session, settings, since=date(2024, 1, 1))

        assert result.new == 1
        ep = db_session.query(Episode).first()
        assert ep.episode_id == "new1"

    @patch("btcedu.core.detector.fetch_channel_videos_ytdlp")
    def test_until_filter(self, mock_fetch, db_session):
        mock_fetch.return_value = [
            EpisodeInfo(episode_id="new1", title="New",
                        published_at=datetime(2024, 6, 15, tzinfo=timezone.utc),
                        url="https://youtube.com/watch?v=new1", source="youtube_backfill"),
            EpisodeInfo(episode_id="old1", title="Old",
                        published_at=datetime(2023, 1, 1, tzinfo=timezone.utc),
                        url="https://youtube.com/watch?v=old1", source="youtube_backfill"),
        ]
        settings = _make_backfill_settings()
        result = backfill_episodes(db_session, settings, until=date(2023, 12, 31))

        assert result.new == 1
        ep = db_session.query(Episode).first()
        assert ep.episode_id == "old1"

    @patch("btcedu.core.detector.fetch_channel_videos_ytdlp")
    def test_max_count(self, mock_fetch, db_session):
        mock_fetch.return_value = [
            EpisodeInfo(episode_id=f"vid{i:03d}", title=f"Ep {i}",
                        published_at=datetime(2024, 1, i + 1, tzinfo=timezone.utc),
                        url=f"https://youtube.com/watch?v=vid{i:03d}", source="youtube_backfill")
            for i in range(10)
        ]
        settings = _make_backfill_settings()
        result = backfill_episodes(db_session, settings, max_count=3)

        assert result.found == 10
        assert result.new == 3
        assert db_session.query(Episode).count() == 3

    @patch("btcedu.core.detector.fetch_channel_videos_ytdlp")
    def test_dry_run_no_commit(self, mock_fetch, db_session):
        mock_fetch.return_value = [
            EpisodeInfo(episode_id="vid001", title="Ep 1",
                        published_at=datetime(2024, 6, 15, tzinfo=timezone.utc),
                        url="https://youtube.com/watch?v=vid001", source="youtube_backfill"),
        ]
        settings = _make_backfill_settings()
        result = backfill_episodes(db_session, settings, dry_run=True)

        assert result.new == 1  # counted as "would insert"
        assert db_session.query(Episode).count() == 0  # but nothing in DB

    @patch("btcedu.core.detector.fetch_channel_videos_ytdlp")
    def test_does_not_modify_existing(self, mock_fetch, db_session):
        # Pre-seed an episode with a specific title
        existing = Episode(
            episode_id="vid001", source="youtube_rss",
            title="Original Title",
            url="https://youtube.com/watch?v=vid001",
            status=EpisodeStatus.GENERATED,
        )
        db_session.add(existing)
        db_session.commit()

        mock_fetch.return_value = [
            EpisodeInfo(episode_id="vid001", title="Different Title From Backfill",
                        published_at=datetime(2024, 6, 15, tzinfo=timezone.utc),
                        url="https://youtube.com/watch?v=vid001", source="youtube_backfill"),
            EpisodeInfo(episode_id="vid002", title="New Episode",
                        published_at=datetime(2024, 6, 1, tzinfo=timezone.utc),
                        url="https://youtube.com/watch?v=vid002", source="youtube_backfill"),
        ]
        settings = _make_backfill_settings()
        result = backfill_episodes(db_session, settings)

        assert result.new == 1  # only vid002

        # Existing episode unchanged
        ep1 = db_session.query(Episode).filter(Episode.episode_id == "vid001").first()
        assert ep1.title == "Original Title"
        assert ep1.source == "youtube_rss"
        assert ep1.status == EpisodeStatus.GENERATED

    @patch("btcedu.core.detector.fetch_channel_videos_ytdlp")
    def test_no_channel_id_raises(self, mock_fetch, db_session):
        from btcedu.config import Settings
        settings = Settings(podcast_youtube_channel_id="")

        with pytest.raises(ValueError, match="No YouTube channel ID"):
            backfill_episodes(db_session, settings)

        mock_fetch.assert_not_called()

    @patch("btcedu.core.detector.fetch_channel_videos_ytdlp")
    def test_date_filter_skips_undated(self, mock_fetch, db_session):
        """Episodes without upload_date are skipped when date filters are active."""
        mock_fetch.return_value = [
            EpisodeInfo(episode_id="dated", title="Has Date",
                        published_at=datetime(2024, 6, 15, tzinfo=timezone.utc),
                        url="https://youtube.com/watch?v=dated", source="youtube_backfill"),
            EpisodeInfo(episode_id="undated", title="No Date",
                        published_at=None,
                        url="https://youtube.com/watch?v=undated", source="youtube_backfill"),
        ]
        settings = _make_backfill_settings()
        result = backfill_episodes(db_session, settings, since=date(2024, 1, 1))

        assert result.new == 1
        ep = db_session.query(Episode).first()
        assert ep.episode_id == "dated"
