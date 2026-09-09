"""Phase 2 tests: feed parsing, detection (idempotent), download, backfill."""

import hashlib
import json
import re
from datetime import UTC, date, datetime
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
        ep = db_session.query(Episode).filter(Episode.episode_id == "dQw4w9WgXcQ").first()
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


# ── Detection: multi-channel (autostart) ───────────────────────────


class TestDetectAllActiveChannels:
    """detect_all_active_channels iterates active channels with their own
    profile so the tagesschau 20:00 Uhr title filter is applied per-channel."""

    def _settings(self):
        from btcedu.config import Settings

        # rss_url points at the tagesschau channel so the "default feed" branch
        # is a no-op (covered by a channel) and detection is purely per-channel.
        return Settings(
            podcast_rss_url="https://feeds.example/tagesschau",
            default_content_profile="bitcoin_podcast",
            profiles_dir="btcedu/profiles",
        )

    def _make_channel(self, db_session, channel_id, rss_url, profile, active=True):
        from btcedu.models.channel import Channel

        ch = Channel(
            channel_id=channel_id,
            name=channel_id,
            rss_url=rss_url,
            content_profile=profile,
            is_active=active,
        )
        db_session.add(ch)
        db_session.commit()
        return ch

    _TAGESSCHAU_FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <id>yt:video:news2000</id><yt:videoId>news2000</yt:videoId>
    <title>tagesschau 20:00 Uhr, 14.07.2026</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=news2000"/>
    <published>2026-07-14T18:55:00+00:00</published>
  </entry>
  <entry>
    <id>yt:video:shortclip</id><yt:videoId>shortclip</yt:videoId>
    <title>"Birdman" in Nairobi #tagesschau</title>
    <link rel="alternate" href="https://www.youtube.com/watch?v=shortclip"/>
    <published>2026-07-14T09:00:00+00:00</published>
  </entry>
</feed>"""

    def test_applies_tagesschau_title_filter_per_channel(self, db_session):
        from btcedu.core.detector import detect_all_active_channels

        settings = self._settings()
        self._make_channel(
            db_session, "tagesschau", "https://feeds.example/tagesschau", "tagesschau_tr"
        )

        with (
            patch("btcedu.core.detector.fetch_feed", return_value=self._TAGESSCHAU_FEED),
            patch("btcedu.core.retention.retention_cutoff", return_value=None),
        ):
            result = detect_all_active_channels(db_session, settings)

        # Only the 20:00 Uhr broadcast passes the profile title filter.
        assert result.new == 1
        eps = db_session.query(Episode).all()
        assert [e.episode_id for e in eps] == ["news2000"]
        assert eps[0].content_profile == "tagesschau_tr"
        assert eps[0].channel_id == "tagesschau"

    def test_skips_inactive_channels(self, db_session):
        from btcedu.config import Settings
        from btcedu.core.detector import detect_all_active_channels

        # No default feed configured, so only active channels are considered.
        settings = Settings(
            podcast_rss_url="",
            podcast_youtube_channel_id="",
            default_content_profile="bitcoin_podcast",
            profiles_dir="btcedu/profiles",
        )
        self._make_channel(
            db_session,
            "tagesschau",
            "https://feeds.example/tagesschau",
            "tagesschau_tr",
            active=False,
        )

        with patch(
            "btcedu.core.detector.fetch_feed", return_value=self._TAGESSCHAU_FEED
        ) as mock_fetch:
            result = detect_all_active_channels(db_session, settings)

        mock_fetch.assert_not_called()
        assert result.new == 0
        assert db_session.query(Episode).count() == 0


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

    @patch("btcedu.core.detector._profile_requires_video", return_value=True)
    @patch("btcedu.core.detector._try_download_video")
    @patch("btcedu.services.download_service.download_audio")
    def test_downloads_video_when_profile_requires_it(
        self, mock_dl, mock_video, mock_requires, db_session, tmp_path
    ):
        """When the profile needs video-derived frames, download the source video
        even when frame_extraction_enabled is False (profile drives the need)."""
        settings = self._make_settings(tmp_path)
        assert settings.frame_extraction_enabled is False
        self._seed_episode(db_session)
        mock_dl.return_value = str(tmp_path / "raw" / "dQw4w9WgXcQ" / "audio.m4a")

        download_episode(db_session, "dQw4w9WgXcQ", settings)

        mock_video.assert_called_once()

    @patch("btcedu.core.detector._profile_requires_video", return_value=False)
    @patch("btcedu.core.detector._try_download_video")
    @patch("btcedu.services.download_service.download_audio")
    def test_skips_video_when_profile_does_not_require_it(
        self, mock_dl, mock_video, mock_requires, db_session, tmp_path
    ):
        """Profiles that don't need video frames must not fetch the source video."""
        settings = self._make_settings(tmp_path)
        self._seed_episode(db_session)
        mock_dl.return_value = str(tmp_path / "raw" / "dQw4w9WgXcQ" / "audio.m4a")

        download_episode(db_session, "dQw4w9WgXcQ", settings)

        mock_video.assert_not_called()

    def test_profile_requires_video_true_for_gemini_frame_edit(self, db_session, tmp_path):
        """_profile_requires_video keys off imagegen.provider == gemini_frame_edit."""
        from types import SimpleNamespace

        from btcedu.core.detector import _profile_requires_video

        settings = self._make_settings(tmp_path)
        ep_yes = SimpleNamespace(content_profile="x")
        ep_no = SimpleNamespace(content_profile="y")
        with patch("btcedu.profiles.get_registry") as mock_reg:

            def _get(name):
                cfg = "gemini_frame_edit" if name == "x" else "generative"
                return SimpleNamespace(stage_config={"imagegen": {"provider": cfg}})

            mock_reg.return_value.get.side_effect = _get
            assert _profile_requires_video(ep_yes, settings) is True
            assert _profile_requires_video(ep_no, settings) is False


# ── yt-dlp channel listing ────────────────────────────────────────

# Minimal yt-dlp --flat-playlist -J output for testing
YTDLP_PLAYLIST_JSON = json.dumps(
    {
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
    }
)


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
        assert ep1.published_at == datetime(2024, 6, 15, tzinfo=UTC)
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
            EpisodeInfo(
                episode_id="vid001",
                title="Ep 1",
                published_at=datetime(2024, 6, 15, tzinfo=UTC),
                url="https://youtube.com/watch?v=vid001",
                source="youtube_backfill",
            ),
            EpisodeInfo(
                episode_id="vid002",
                title="Ep 2",
                published_at=datetime(2024, 6, 1, tzinfo=UTC),
                url="https://youtube.com/watch?v=vid002",
                source="youtube_backfill",
            ),
        ]
        settings = _make_backfill_settings()
        result = backfill_episodes(db_session, settings)

        assert result.found == 2
        assert result.new == 2
        assert result.total == 2
        assert db_session.query(Episode).count() == 2

    @patch("btcedu.core.detector.fetch_channel_videos_ytdlp")
    def test_sets_profile_and_pipeline_version(self, mock_fetch, db_session):
        mock_fetch.return_value = [
            EpisodeInfo(
                episode_id="news001",
                title="tagesschau 20:00 Uhr, 14.07.2026",
                published_at=datetime(2026, 7, 14, tzinfo=UTC),
                url="https://youtube.com/watch?v=news001",
                source="youtube_backfill",
            )
        ]
        settings = _make_backfill_settings(
            default_content_profile="tagesschau_tr",
            pipeline_version=2,
        )

        backfill_episodes(db_session, settings)

        episode = db_session.query(Episode).one()
        assert episode.content_profile == "tagesschau_tr"
        assert episode.pipeline_version == 2

    @patch("btcedu.core.detector.fetch_channel_videos_ytdlp")
    def test_idempotent(self, mock_fetch, db_session):
        eps = [
            EpisodeInfo(
                episode_id="vid001",
                title="Ep 1",
                published_at=datetime(2024, 6, 15, tzinfo=UTC),
                url="https://youtube.com/watch?v=vid001",
                source="youtube_backfill",
            ),
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
            EpisodeInfo(
                episode_id="new1",
                title="New",
                published_at=datetime(2024, 6, 15, tzinfo=UTC),
                url="https://youtube.com/watch?v=new1",
                source="youtube_backfill",
            ),
            EpisodeInfo(
                episode_id="old1",
                title="Old",
                published_at=datetime(2023, 1, 1, tzinfo=UTC),
                url="https://youtube.com/watch?v=old1",
                source="youtube_backfill",
            ),
        ]
        settings = _make_backfill_settings()
        result = backfill_episodes(db_session, settings, since=date(2024, 1, 1))

        assert result.new == 1
        ep = db_session.query(Episode).first()
        assert ep.episode_id == "new1"

    @patch("btcedu.core.detector.fetch_channel_videos_ytdlp")
    def test_until_filter(self, mock_fetch, db_session):
        mock_fetch.return_value = [
            EpisodeInfo(
                episode_id="new1",
                title="New",
                published_at=datetime(2024, 6, 15, tzinfo=UTC),
                url="https://youtube.com/watch?v=new1",
                source="youtube_backfill",
            ),
            EpisodeInfo(
                episode_id="old1",
                title="Old",
                published_at=datetime(2023, 1, 1, tzinfo=UTC),
                url="https://youtube.com/watch?v=old1",
                source="youtube_backfill",
            ),
        ]
        settings = _make_backfill_settings()
        result = backfill_episodes(db_session, settings, until=date(2023, 12, 31))

        assert result.new == 1
        ep = db_session.query(Episode).first()
        assert ep.episode_id == "old1"

    @patch("btcedu.core.detector.fetch_channel_videos_ytdlp")
    def test_max_count(self, mock_fetch, db_session):
        mock_fetch.return_value = [
            EpisodeInfo(
                episode_id=f"vid{i:03d}",
                title=f"Ep {i}",
                published_at=datetime(2024, 1, i + 1, tzinfo=UTC),
                url=f"https://youtube.com/watch?v=vid{i:03d}",
                source="youtube_backfill",
            )
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
            EpisodeInfo(
                episode_id="vid001",
                title="Ep 1",
                published_at=datetime(2024, 6, 15, tzinfo=UTC),
                url="https://youtube.com/watch?v=vid001",
                source="youtube_backfill",
            ),
        ]
        settings = _make_backfill_settings()
        result = backfill_episodes(db_session, settings, dry_run=True)

        assert result.new == 1  # counted as "would insert"
        assert db_session.query(Episode).count() == 0  # but nothing in DB

    @patch("btcedu.core.detector.fetch_channel_videos_ytdlp")
    def test_does_not_modify_existing(self, mock_fetch, db_session):
        # Pre-seed an episode with a specific title
        existing = Episode(
            episode_id="vid001",
            source="youtube_rss",
            title="Original Title",
            url="https://youtube.com/watch?v=vid001",
            status=EpisodeStatus.PUBLISHED,
        )
        db_session.add(existing)
        db_session.commit()

        mock_fetch.return_value = [
            EpisodeInfo(
                episode_id="vid001",
                title="Different Title From Backfill",
                published_at=datetime(2024, 6, 15, tzinfo=UTC),
                url="https://youtube.com/watch?v=vid001",
                source="youtube_backfill",
            ),
            EpisodeInfo(
                episode_id="vid002",
                title="New Episode",
                published_at=datetime(2024, 6, 1, tzinfo=UTC),
                url="https://youtube.com/watch?v=vid002",
                source="youtube_backfill",
            ),
        ]
        settings = _make_backfill_settings()
        result = backfill_episodes(db_session, settings)

        assert result.new == 1  # only vid002

        # Existing episode unchanged
        ep1 = db_session.query(Episode).filter(Episode.episode_id == "vid001").first()
        assert ep1.title == "Original Title"
        assert ep1.source == "youtube_rss"
        assert ep1.status == EpisodeStatus.PUBLISHED

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
            EpisodeInfo(
                episode_id="dated",
                title="Has Date",
                published_at=datetime(2024, 6, 15, tzinfo=UTC),
                url="https://youtube.com/watch?v=dated",
                source="youtube_backfill",
            ),
            EpisodeInfo(
                episode_id="undated",
                title="No Date",
                published_at=None,
                url="https://youtube.com/watch?v=undated",
                source="youtube_backfill",
            ),
        ]
        settings = _make_backfill_settings()
        result = backfill_episodes(db_session, settings, since=date(2024, 1, 1))

        assert result.new == 1
        ep = db_session.query(Episode).first()
        assert ep.episode_id == "dated"


# ── Ingest title filter (per content profile) ──────────────────────


class TestIngestTitleFilter:
    """The tagesschau_tr profile ingests only the 20:00 Uhr broadcast."""

    def _mixed_feed(self):
        return """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <yt:videoId>ts2000</yt:videoId>
    <title>tagesschau 20:00 Uhr, 14.07.2026</title>
    <link href="https://www.youtube.com/watch?v=ts2000"/>
    <published>2026-07-14T20:00:00+00:00</published>
  </entry>
  <entry>
    <yt:videoId>ts100s</yt:videoId>
    <title>tagesschau in 100 Sekunden</title>
    <link href="https://www.youtube.com/watch?v=ts100s"/>
    <published>2026-07-14T12:00:00+00:00</published>
  </entry>
  <entry>
    <yt:videoId>ts1600</yt:videoId>
    <title>tagesschau 16:00 Uhr, 14.07.2026</title>
    <link href="https://www.youtube.com/watch?v=ts1600"/>
    <published>2026-07-14T16:00:00+00:00</published>
  </entry>
</feed>"""

    def _settings(self, profile):
        from btcedu.config import Settings

        return Settings(
            podcast_rss_url="https://example.com/feed.xml",
            default_content_profile=profile,
        )

    @patch("btcedu.core.detector.fetch_feed")
    def test_tagesschau_keeps_2000_and_100s(self, mock_fetch, db_session):
        from btcedu.core.detector import detect_episodes
        from btcedu.profiles import reset_registry

        reset_registry()
        mock_fetch.return_value = self._mixed_feed()
        with patch("btcedu.core.retention.retention_cutoff", return_value=None):
            result = detect_episodes(db_session, self._settings("tagesschau_tr"))

        assert result.new == 2
        ids = sorted(e.episode_id for e in db_session.query(Episode).all())
        assert ids == ["ts100s", "ts2000"]

    @patch("btcedu.core.detector.fetch_feed")
    def test_profile_without_filter_keeps_all(self, mock_fetch, db_session):
        from btcedu.core.detector import detect_episodes
        from btcedu.profiles import reset_registry

        reset_registry()
        mock_fetch.return_value = self._mixed_feed()
        result = detect_episodes(db_session, self._settings("bitcoin_podcast"))

        assert result.new == 3

    @patch("btcedu.core.detector.fetch_channel_videos_ytdlp")
    def test_backfill_applies_filter(self, mock_fetch, db_session):
        from btcedu.profiles import reset_registry

        reset_registry()
        mock_fetch.return_value = [
            EpisodeInfo(
                episode_id="ts2000",
                title="tagesschau 20:00 Uhr, 14.07.2026",
                published_at=datetime(2026, 7, 14, tzinfo=UTC),
                url="https://youtube.com/watch?v=ts2000",
                source="youtube_backfill",
            ),
            EpisodeInfo(
                episode_id="ts100s",
                title="tagesschau in 100 Sekunden",
                published_at=datetime(2026, 7, 14, tzinfo=UTC),
                url="https://youtube.com/watch?v=ts100s",
                source="youtube_backfill",
            ),
            EpisodeInfo(
                episode_id="ts1600",
                title="tagesschau 16:00 Uhr, 14.07.2026",
                published_at=datetime(2026, 7, 14, tzinfo=UTC),
                url="https://youtube.com/watch?v=ts1600",
                source="youtube_backfill",
            ),
        ]
        settings = _make_backfill_settings(default_content_profile="tagesschau_tr")
        with patch("btcedu.core.retention.retention_cutoff", return_value=None):
            result = backfill_episodes(db_session, settings)

        assert result.new == 2
        ids = sorted(e.episode_id for e in db_session.query(Episode).all())
        assert ids == ["ts100s", "ts2000"]


class TestFeedReDeliversAnOldBroadcast:
    """A broadcast already stored must not be bought a second time.

    On 2026-08-08 the feed served the 4 and 5 August editions again under fresh
    video ids. Nothing stopped them: deduplication only ever compared the feed
    against the *local recorder*, never against the feed's own history, and the
    id check cannot help because a re-upload genuinely has a new id. Both ran
    the full paid pipeline on transcripts that were word-for-word identical to
    the originals.
    """

    def _settings(self, profile="tagesschau_tr"):
        from btcedu.config import Settings

        return Settings(
            podcast_rss_url="https://example.com/feed.xml",
            default_content_profile=profile,
        )

    def _feed(self, entries):
        items = "".join(
            f"""
  <entry>
    <yt:videoId>{vid}</yt:videoId>
    <title>{title}</title>
    <link href="https://www.youtube.com/watch?v={vid}"/>
    <published>{published}</published>
  </entry>"""
            for vid, title, published in entries
        )
        return (
            '<?xml version="1.0"?>\n<feed xmlns="http://www.w3.org/2005/Atom" '
            'xmlns:yt="http://www.youtube.com/xml/schemas/2015">'
            f"{items}\n</feed>"
        )

    def _store(self, db_session, episode_id, title, published_at, source="youtube_rss"):
        db_session.add(
            Episode(
                episode_id=episode_id,
                source=source,
                title=title,
                url=f"https://www.youtube.com/watch?v={episode_id}",
                published_at=published_at,
                status=EpisodeStatus.APPROVED,
                content_profile="tagesschau_tr",
            )
        )
        db_session.commit()

    @patch("btcedu.core.detector.fetch_feed")
    def test_reupload_under_a_new_id_is_rejected(self, mock_fetch, db_session):
        from btcedu.core.detector import detect_episodes
        from btcedu.profiles import reset_registry

        reset_registry()
        self._store(
            db_session,
            "hrzrn0Wutak",
            "tagesschau 20:00 Uhr, 04.08.2026",
            datetime(2026, 8, 4, 18, 28, tzinfo=UTC),
        )
        # The re-upload: new id, current timestamp, same broadcast in the title.
        mock_fetch.return_value = self._feed(
            [("mRlfWLZ-EJs", "tagesschau 20:00 Uhr, 04.08.2026", "2026-08-08T17:43:00+00:00")]
        )
        with patch("btcedu.core.retention.retention_cutoff", return_value=None):
            result = detect_episodes(db_session, self._settings())

        assert result.new == 0
        assert db_session.query(Episode).count() == 1

    @patch("btcedu.core.detector.fetch_feed")
    def test_upload_timestamp_alone_would_not_have_caught_it(self, mock_fetch, db_session):
        """The published_at of the re-upload is four days off the broadcast."""
        from btcedu.core.detector import detect_episodes
        from btcedu.profiles import reset_registry

        reset_registry()
        self._store(
            db_session,
            "o4Y4qK2OEk8",
            "tagesschau 20:00 Uhr, 05.08.2026",
            datetime(2026, 8, 5, 18, 46, tzinfo=UTC),
        )
        mock_fetch.return_value = self._feed(
            [("l0sb85WhaTk", "tagesschau 20:00 Uhr, 05.08.2026", "2026-08-08T18:54:00+00:00")]
        )
        with patch("btcedu.core.retention.retention_cutoff", return_value=None):
            result = detect_episodes(db_session, self._settings())

        assert result.new == 0

    @patch("btcedu.core.detector.fetch_feed")
    def test_todays_broadcast_still_gets_through(self, mock_fetch, db_session):
        from btcedu.core.detector import detect_episodes
        from btcedu.profiles import reset_registry

        reset_registry()
        self._store(
            db_session,
            "hrzrn0Wutak",
            "tagesschau 20:00 Uhr, 04.08.2026",
            datetime(2026, 8, 4, 18, 28, tzinfo=UTC),
        )
        mock_fetch.return_value = self._feed(
            [("newone", "tagesschau 20:00 Uhr, 09.08.2026", "2026-08-09T18:30:00+00:00")]
        )
        with patch("btcedu.core.retention.retention_cutoff", return_value=None):
            result = detect_episodes(db_session, self._settings())

        assert result.new == 1

    @patch("btcedu.core.detector.fetch_feed")
    def test_recorder_slug_counts_as_the_same_broadcast(self, mock_fetch, db_session):
        """The stored copy may carry its date in the slug rather than the title."""
        from btcedu.core.detector import detect_episodes
        from btcedu.profiles import reset_registry

        reset_registry()
        self._store(
            db_session,
            "tagesschau_2026-08-06_2000",
            "Tagesschau",
            datetime(2026, 8, 6, 20, 0, tzinfo=UTC),
            source="local_recorder",
        )
        mock_fetch.return_value = self._feed(
            [("hlf-1rgnPSw", "tagesschau 20:00 Uhr, 06.08.2026", "2026-08-06T18:31:00+00:00")]
        )
        with patch("btcedu.core.retention.retention_cutoff", return_value=None):
            result = detect_episodes(db_session, self._settings())

        assert result.new == 0

    @patch("btcedu.core.detector.fetch_feed")
    def test_one_batch_cannot_carry_the_duplicate_itself(self, mock_fetch, db_session):
        from btcedu.core.detector import detect_episodes
        from btcedu.profiles import reset_registry

        reset_registry()
        mock_fetch.return_value = self._feed(
            [
                ("first", "tagesschau 20:00 Uhr, 07.08.2026", "2026-08-07T18:30:00+00:00"),
                ("again", "tagesschau 20:00 Uhr, 07.08.2026", "2026-08-08T18:30:00+00:00"),
            ]
        )
        with patch("btcedu.core.retention.retention_cutoff", return_value=None):
            result = detect_episodes(db_session, self._settings())

        assert result.new == 1
        assert [e.episode_id for e in db_session.query(Episode).all()] == ["first"]

    @patch("btcedu.core.detector.fetch_feed")
    def test_a_dateless_feed_is_left_alone(self, mock_fetch, db_session):
        """Two episodes on one day are normal for an ordinary podcast.

        The rule keys on the date in the title precisely so that a feed without
        such dates keeps every entry — deduplicating those by upload day would
        silently drop the second episode of any busy day.
        """
        from btcedu.core.detector import detect_episodes
        from btcedu.profiles import reset_registry

        reset_registry()
        mock_fetch.return_value = self._feed(
            [
                ("aaa", "Bitcoin Basics Folge 1", "2026-08-07T09:00:00+00:00"),
                ("bbb", "Bitcoin Basics Folge 2", "2026-08-07T17:00:00+00:00"),
            ]
        )
        with patch("btcedu.core.retention.retention_cutoff", return_value=None):
            result = detect_episodes(db_session, self._settings("bitcoin_podcast"))

        assert result.new == 2

    @patch("btcedu.core.detector.fetch_feed")
    def test_dated_foreign_profile_does_not_claim_the_broadcast(
        self, mock_fetch, db_session
    ):
        from btcedu.core.detector import detect_episodes
        from btcedu.profiles import reset_registry

        reset_registry()
        db_session.add(
            Episode(
                episode_id="podcast",
                source="youtube_rss",
                title="Podcast 08.09.2026 um 20:00 Uhr",
                url="https://example.com/podcast",
                published_at=datetime(2026, 9, 8, tzinfo=UTC),
                content_profile="bitcoin_podcast",
            )
        )
        db_session.commit()
        mock_fetch.return_value = self._feed(
            [("news", "tagesschau 20:00 Uhr, 08.09.2026", "2026-09-08T18:30:00+00:00")]
        )

        with patch("btcedu.core.retention.retention_cutoff", return_value=None):
            result = detect_episodes(db_session, self._settings())

        assert result.new == 1
        assert {episode.episode_id for episode in db_session.query(Episode)} == {
            "podcast",
            "news",
        }

    @pytest.mark.parametrize("reverse", [False, True])
    @patch("btcedu.core.detector.fetch_feed")
    def test_two_explicit_editions_on_one_day_are_kept(
        self, mock_fetch, db_session, reverse
    ):
        from btcedu.core.detector import detect_episodes
        from btcedu.profiles import reset_registry

        reset_registry()
        entries = [
            (
                "short",
                "tagesschau in 100 Sekunden, 08.09.2026",
                "2026-09-08T12:00:00+00:00",
            ),
            (
                "main",
                "tagesschau 20:00 Uhr, 08.09.2026",
                "2026-09-08T18:30:00+00:00",
            ),
        ]
        mock_fetch.return_value = self._feed(list(reversed(entries)) if reverse else entries)

        with patch("btcedu.core.retention.retention_cutoff", return_value=None):
            result = detect_episodes(db_session, self._settings())

        assert result.new == 2
        assert {episode.episode_id for episode in db_session.query(Episode)} == {
            "short",
            "main",
        }

    @patch("btcedu.core.detector.fetch_feed")
    def test_unknown_editions_are_not_merged_by_date(self, mock_fetch, db_session):
        from btcedu.core.detector import detect_episodes
        from btcedu.profiles import reset_registry

        reset_registry()
        mock_fetch.return_value = self._feed(
            [
                (
                    "unknown-a",
                    "tagesschau Sondersendung, 08.09.2026",
                    "2026-09-08T12:00:00+00:00",
                ),
                (
                    "unknown-b",
                    "tagesschau Sondersendung, 08.09.2026",
                    "2026-09-08T18:30:00+00:00",
                ),
            ]
        )

        with (
            patch(
                "btcedu.core.detector._resolve_title_filter",
                return_value=re.compile("tagesschau"),
            ),
            patch("btcedu.core.retention.retention_cutoff", return_value=None),
        ):
            result = detect_episodes(db_session, self._settings())

        assert result.new == 2


class TestDetectDoesNotRewriteExistingChannels:
    @patch("btcedu.core.detector.fetch_feed")
    def test_empty_feed_leaves_unassigned_foreign_episode_unchanged(
        self, mock_fetch, db_session
    ):
        from btcedu.config import Settings
        from btcedu.core.detector import detect_episodes
        from btcedu.models.channel import Channel

        db_session.add_all(
            [
                Channel(
                    channel_id="tagesschau",
                    name="Tagesschau",
                    content_profile="tagesschau_tr",
                    rss_url="https://example.com/news.xml",
                ),
                Episode(
                    episode_id="podcast-orphan",
                    channel_id=None,
                    source="youtube_rss",
                    title="Bitcoin Podcast",
                    url="https://example.com/podcast",
                    content_profile="bitcoin_podcast",
                ),
            ]
        )
        db_session.commit()
        mock_fetch.return_value = (
            '<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        )
        settings = Settings(
            podcast_rss_url="https://example.com/news.xml",
            default_content_profile="tagesschau_tr",
        )

        with patch("btcedu.core.retention.retention_cutoff", return_value=None):
            detect_episodes(db_session, settings)

        assert db_session.query(Episode).one().channel_id is None
