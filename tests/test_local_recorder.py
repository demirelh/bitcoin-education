"""Local recorder ingest: scanning, precedence over the feed, and fallback.

The behaviour under test is a cost and latency decision, not a formatting one:
the local file must be preferred when it exists, the YouTube path must keep
working when it does not, and the same broadcast must never be processed twice.
"""

import json
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from btcedu.core.detector import detect_local_recordings, download_episode
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.services.local_recorder_service import (
    SOURCE_NAME,
    extract_audio,
    find_recording,
    is_local_source,
    resolve_local_video,
    scan_recordings,
)

PROFILE = "tagesschau_tr"


def make_recording(
    base: Path,
    day: date,
    *,
    done: bool = True,
    metadata: dict | None = None,
    with_raw: bool = True,
    video_bytes: bytes = b"\0" * 32,
) -> Path:
    """Write a recording that follows the recorder's output contract."""
    slug = f"tagesschau_{day.isoformat()}_2000"
    directory = base / day.isoformat()
    directory.mkdir(parents=True, exist_ok=True)

    video = directory / f"{slug}.mp4"
    video.write_bytes(video_bytes)

    if with_raw:
        (directory / f"{slug}.raw.mp4").write_bytes(b"\0" * 64)

    payload = {
        "title": f"tagesschau 20:00 Uhr, {day.strftime('%d.%m.%Y')}",
        "scheduled_start": f"{day.isoformat()}T20:00:00+02:00",
        "duration_seconds": 873.988,
    }
    payload.update(metadata or {})
    (directory / f"{slug}.metadata.json").write_text(json.dumps(payload), encoding="utf-8")

    if done:
        (directory / f"{slug}.DONE").write_text("", encoding="utf-8")
    return video


@pytest.fixture
def recordings_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "recordings"
    directory.mkdir()
    return directory


def make_settings(tmp_path: Path, recordings_dir: Path, **kwargs):
    from btcedu.config import Settings

    return Settings(
        raw_data_dir=str(tmp_path / "raw"),
        audio_format="m4a",
        default_content_profile=PROFILE,
        profiles_dir="btcedu/profiles",
        episode_retention_days=0,
        **kwargs,
    )


# ── Scanning the recorder's output ─────────────────────────────────


class TestScanRecordings:
    def test_finds_a_finished_recording(self, recordings_dir):
        make_recording(recordings_dir, date(2026, 8, 6))

        found = scan_recordings(recordings_dir)

        assert len(found) == 1
        assert found[0].day == date(2026, 8, 6)
        assert found[0].video.name == "tagesschau_2026-08-06_2000.mp4"

    def test_ignores_a_recording_without_the_done_marker(self, recordings_dir):
        make_recording(recordings_dir, date(2026, 8, 6), done=False)

        assert scan_recordings(recordings_dir) == []

    def test_never_returns_the_untrimmed_capture(self, recordings_dir):
        make_recording(recordings_dir, date(2026, 8, 6))

        videos = [r.video.name for r in scan_recordings(recordings_dir)]

        assert not any(name.endswith(".raw.mp4") for name in videos)

    def test_ignores_an_empty_video(self, recordings_dir):
        make_recording(recordings_dir, date(2026, 8, 6), video_bytes=b"")

        assert scan_recordings(recordings_dir) == []

    def test_returns_newest_first(self, recordings_dir):
        make_recording(recordings_dir, date(2026, 8, 4))
        make_recording(recordings_dir, date(2026, 8, 6))
        make_recording(recordings_dir, date(2026, 8, 5))

        days = [r.day for r in scan_recordings(recordings_dir)]

        assert days == [date(2026, 8, 6), date(2026, 8, 5), date(2026, 8, 4)]

    def test_a_missing_directory_is_not_an_error(self, tmp_path):
        """The recorder may not be installed; that is a fallback, not a failure."""
        assert scan_recordings(tmp_path / "does-not-exist") == []

    def test_skips_directories_that_are_not_broadcast_days(self, recordings_dir):
        stray = recordings_dir / "tmp"
        stray.mkdir()
        (stray / "tagesschau_2026-08-06_2000.DONE").write_text("")

        assert scan_recordings(recordings_dir) == []

    def test_respects_the_retention_cutoff(self, recordings_dir):
        make_recording(recordings_dir, date(2026, 7, 1))
        make_recording(recordings_dir, date(2026, 8, 6))

        found = scan_recordings(recordings_dir, since=date(2026, 8, 1))

        assert [r.day for r in found] == [date(2026, 8, 6)]

    def test_uses_the_scheduled_start_not_the_file_time(self, recordings_dir):
        make_recording(recordings_dir, date(2026, 8, 6))

        recording = scan_recordings(recordings_dir)[0]

        assert recording.published_at.date() == date(2026, 8, 6)
        assert recording.published_at.hour == 20

    def test_falls_back_to_a_derived_title(self, recordings_dir):
        make_recording(recordings_dir, date(2026, 8, 6), metadata={"title": ""})

        recording = scan_recordings(recordings_dir)[0]

        assert "tagesschau" in recording.title
        assert "06.08.2026" in recording.title

    def test_find_recording_locates_one_day(self, recordings_dir):
        make_recording(recordings_dir, date(2026, 8, 6))

        assert find_recording(recordings_dir, date(2026, 8, 6)) is not None
        assert find_recording(recordings_dir, date(2026, 8, 5)) is None


class TestResolveLocalVideo:
    def test_accepts_a_recording_inside_the_base_directory(self, recordings_dir):
        video = make_recording(recordings_dir, date(2026, 8, 6))

        assert resolve_local_video(str(video), recordings_dir) == video.resolve()

    def test_rejects_a_path_outside_the_base_directory(self, recordings_dir, tmp_path):
        outside = tmp_path / "etc-passwd.mp4"
        outside.write_bytes(b"\0")

        with pytest.raises(ValueError, match="outside"):
            resolve_local_video(str(outside), recordings_dir)

    def test_rejects_the_untrimmed_capture(self, recordings_dir):
        make_recording(recordings_dir, date(2026, 8, 6))
        raw = recordings_dir / "2026-08-06" / "tagesschau_2026-08-06_2000.raw.mp4"

        with pytest.raises(ValueError, match="untrimmed"):
            resolve_local_video(str(raw), recordings_dir)

    def test_reports_a_vanished_recording(self, recordings_dir):
        video = make_recording(recordings_dir, date(2026, 8, 6))
        video.unlink()

        with pytest.raises(FileNotFoundError):
            resolve_local_video(str(video), recordings_dir)


def test_is_local_source_distinguishes_the_recorder():
    assert is_local_source(SOURCE_NAME)
    assert not is_local_source("youtube_rss")
    assert not is_local_source(None)


# ── Ingest: precedence, idempotency and fallback ───────────────────

MINIMAL_PROFILE = """
name: {name}
display_name: "Local test profile"
source_language: de
target_language: tr
domain: news
pipeline_version: 2
ingest:
  title_include: "(?i)tagesschau.*20[:.]?00\\\\s*Uhr"
  local_recorder:
    enabled: {enabled}
    base_dir: {base_dir}
    supersedes_feed: {supersedes}
"""


def write_profile(
    tmp_path: Path,
    recordings_dir: Path,
    *,
    name: str = "local_test",
    enabled: str = "true",
    supersedes: str = "true",
) -> Path:
    """A minimal profile pointing the local recorder at a temporary tree."""
    profiles_dir = tmp_path / "profiles"
    profiles_dir.mkdir(exist_ok=True)
    (profiles_dir / f"{name}.yaml").write_text(
        MINIMAL_PROFILE.format(
            name=name, enabled=enabled, base_dir=recordings_dir, supersedes=supersedes
        ),
        encoding="utf-8",
    )
    return profiles_dir


def local_settings(tmp_path: Path, recordings_dir: Path, **kwargs):
    from btcedu.config import Settings

    profiles_dir = kwargs.pop("profiles_dir", None) or write_profile(tmp_path, recordings_dir)
    return Settings(
        raw_data_dir=str(tmp_path / "raw"),
        audio_format="m4a",
        default_content_profile=kwargs.pop("profile", "local_test"),
        profiles_dir=str(profiles_dir),
        episode_retention_days=0,
        **kwargs,
    )


class TestDetectLocalRecordings:
    def test_ingests_a_finished_recording(self, db_session, tmp_path, recordings_dir):
        make_recording(recordings_dir, date(2026, 8, 6))
        settings = local_settings(tmp_path, recordings_dir)

        result = detect_local_recordings(db_session, settings)

        assert result.new == 1
        episode = db_session.query(Episode).one()
        assert episode.episode_id == "tagesschau_2026-08-06_2000"
        assert episode.source == SOURCE_NAME
        assert episode.url.endswith("tagesschau_2026-08-06_2000.mp4")
        assert episode.status == EpisodeStatus.NEW

    def test_is_idempotent_across_repeated_timer_runs(
        self, db_session, tmp_path, recordings_dir
    ):
        """The timer fires every ten minutes; only the first run may insert."""
        make_recording(recordings_dir, date(2026, 8, 6))
        settings = local_settings(tmp_path, recordings_dir)

        first = detect_local_recordings(db_session, settings)
        second = detect_local_recordings(db_session, settings)

        assert (first.new, second.new) == (1, 0)
        assert db_session.query(Episode).count() == 1

    def test_ignores_a_recording_still_in_progress(
        self, db_session, tmp_path, recordings_dir
    ):
        make_recording(recordings_dir, date(2026, 8, 6), done=False)
        settings = local_settings(tmp_path, recordings_dir)

        assert detect_local_recordings(db_session, settings).new == 0
        assert db_session.query(Episode).count() == 0

    def test_does_nothing_when_the_profile_has_no_local_recorder(
        self, db_session, tmp_path, recordings_dir
    ):
        make_recording(recordings_dir, date(2026, 8, 6))
        profiles_dir = write_profile(tmp_path, recordings_dir, enabled="false")
        settings = local_settings(tmp_path, recordings_dir, profiles_dir=profiles_dir)

        assert detect_local_recordings(db_session, settings).new == 0

    def test_applies_the_profile_title_filter(self, db_session, tmp_path, recordings_dir):
        make_recording(
            recordings_dir, date(2026, 8, 6), metadata={"title": "tagesthemen 22:15 Uhr"}
        )
        settings = local_settings(tmp_path, recordings_dir)

        assert detect_local_recordings(db_session, settings).new == 0

    def test_an_unmounted_recorder_directory_is_survivable(
        self, db_session, tmp_path, recordings_dir
    ):
        settings = local_settings(tmp_path, recordings_dir)
        recordings_dir.rmdir()

        assert detect_local_recordings(db_session, settings).new == 0


class TestFeedIsSupersededByLocalRecording:
    """The YouTube upload of an already-recorded broadcast must be ignored.

    It arrives one to two hours after the local file. Ingesting it as well would
    repeat transcription, translation, TTS and image generation for a broadcast
    that is already in flight - a full duplicate run at full API cost.
    """

    FEED = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015">
  <entry>
    <yt:videoId>ytUpload123</yt:videoId>
    <title>tagesschau 20:00 Uhr, 06.08.2026</title>
    <link href="https://www.youtube.com/watch?v=ytUpload123"/>
    <published>2026-08-06T22:10:00+00:00</published>
  </entry>
</feed>"""

    def _detect_with_feed(self, db_session, settings):
        from btcedu.core.detector import detect_episodes

        with patch("btcedu.core.detector.fetch_feed", return_value=self.FEED):
            return detect_episodes(
                db_session, settings, feed_url="https://feeds.example/tagesschau"
            )

    def test_the_feed_entry_is_skipped_once_recorded_locally(
        self, db_session, tmp_path, recordings_dir
    ):
        make_recording(recordings_dir, date(2026, 8, 6))
        settings = local_settings(tmp_path, recordings_dir)
        detect_local_recordings(db_session, settings)

        self._detect_with_feed(db_session, settings)

        episodes = db_session.query(Episode).all()
        assert len(episodes) == 1, "the same broadcast must not be processed twice"
        assert episodes[0].source == SOURCE_NAME

    def test_the_feed_still_works_without_a_local_recording(
        self, db_session, tmp_path, recordings_dir
    ):
        """The YouTube branch is the backup and must remain fully functional."""
        settings = local_settings(tmp_path, recordings_dir)

        result = self._detect_with_feed(db_session, settings)

        assert result.new == 1
        episode = db_session.query(Episode).one()
        assert episode.episode_id == "ytUpload123"
        assert episode.source == "youtube_rss"

    def test_a_different_day_is_not_suppressed(self, db_session, tmp_path, recordings_dir):
        make_recording(recordings_dir, date(2026, 8, 5))
        settings = local_settings(tmp_path, recordings_dir)
        detect_local_recordings(db_session, settings)

        self._detect_with_feed(db_session, settings)

        ids = {ep.episode_id for ep in db_session.query(Episode).all()}
        assert ids == {"tagesschau_2026-08-05_2000", "ytUpload123"}

    def test_the_broadcast_day_comes_from_the_title_not_the_upload_time(
        self, db_session, tmp_path, recordings_dir
    ):
        """An upload after midnight carries the next day's published_at."""
        from btcedu.core.detector import _feed_broadcast_day
        from btcedu.models.schemas import EpisodeInfo

        info = EpisodeInfo(
            episode_id="x",
            title="tagesschau 20:00 Uhr, 06.08.2026",
            published_at=datetime(2026, 8, 7, 0, 30, tzinfo=UTC),
            url="https://www.youtube.com/watch?v=x",
        )

        assert _feed_broadcast_day(info) == date(2026, 8, 6)


class TestDownloadUsesTheLocalFile:
    def _seed(self, db_session, video: Path, profile: str = "local_test") -> Episode:
        episode = Episode(
            episode_id="tagesschau_2026-08-06_2000",
            source=SOURCE_NAME,
            title="tagesschau 20:00 Uhr, 06.08.2026",
            url=str(video),
            status=EpisodeStatus.NEW,
            content_profile=profile,
        )
        db_session.add(episode)
        db_session.commit()
        return episode

    def test_never_invokes_ytdlp(self, db_session, tmp_path, recordings_dir):
        video = make_recording(recordings_dir, date(2026, 8, 6))
        settings = local_settings(tmp_path, recordings_dir)
        self._seed(db_session, video)

        with (
            patch("btcedu.services.download_service.download_audio") as ytdlp_audio,
            patch("btcedu.core.detector._try_download_video") as ytdlp_video,
            patch(
                "btcedu.services.local_recorder_service.extract_audio",
                side_effect=lambda src, dest, **kw: (
                    Path(dest).write_bytes(b"audio"),
                    Path(dest),
                )[1],
            ),
        ):
            audio_path = download_episode(db_session, "tagesschau_2026-08-06_2000", settings)

        ytdlp_audio.assert_not_called()
        ytdlp_video.assert_not_called()
        assert Path(audio_path).exists()

    def test_links_the_video_for_frame_extraction(self, db_session, tmp_path, recordings_dir):
        video = make_recording(recordings_dir, date(2026, 8, 6), video_bytes=b"\0" * 128)
        settings = local_settings(tmp_path, recordings_dir)
        self._seed(db_session, video)

        with patch(
            "btcedu.services.local_recorder_service.extract_audio",
            side_effect=lambda src, dest, **kw: (
                Path(dest).write_bytes(b"audio"),
                Path(dest),
            )[1],
        ):
            download_episode(db_session, "tagesschau_2026-08-06_2000", settings)

        out_dir = Path(settings.raw_data_dir) / "tagesschau_2026-08-06_2000"
        linked = out_dir / "video.mp4"
        assert linked.exists()
        assert linked.read_bytes() == b"\0" * 128
        meta = json.loads((out_dir / "video_meta.json").read_text())
        assert meta["source"] == "local_recorder"
        assert meta["video_path"] == str(linked)

    def test_marks_the_episode_downloaded(self, db_session, tmp_path, recordings_dir):
        video = make_recording(recordings_dir, date(2026, 8, 6))
        settings = local_settings(tmp_path, recordings_dir)
        self._seed(db_session, video)

        with patch(
            "btcedu.services.local_recorder_service.extract_audio",
            side_effect=lambda src, dest, **kw: (
                Path(dest).write_bytes(b"audio"),
                Path(dest),
            )[1],
        ):
            download_episode(db_session, "tagesschau_2026-08-06_2000", settings)

        episode = db_session.query(Episode).one()
        assert episode.status == EpisodeStatus.DOWNLOADED
        assert episode.audio_path is not None


class TestLocalRecordingIsSupersededByFeed:
    """The reverse direction: YouTube first, recorder second.

    This is the case that actually occurred in production. Every broadcast from
    before the recorder existed - and any evening the recorder misses - is in
    the database as a YouTube episode. Ingesting the local file afterwards would
    re-transcribe, re-translate, re-voice and re-render finished work.
    """

    def _seed_feed_episode(self, db_session, day: date, status=EpisodeStatus.APPROVED):
        episode = Episode(
            episode_id="ytUpload123",
            source="youtube_rss",
            title=f"tagesschau 20:00 Uhr, {day.strftime('%d.%m.%Y')}",
            url="https://www.youtube.com/watch?v=ytUpload123",
            published_at=datetime(day.year, day.month, day.day, 22, 10, tzinfo=UTC),
            status=status,
        )
        db_session.add(episode)
        db_session.commit()
        return episode

    def test_skips_a_broadcast_already_ingested_from_youtube(
        self, db_session, tmp_path, recordings_dir
    ):
        self._seed_feed_episode(db_session, date(2026, 8, 6))
        make_recording(recordings_dir, date(2026, 8, 6))
        settings = local_settings(tmp_path, recordings_dir)

        result = detect_local_recordings(db_session, settings)

        assert result.new == 0, "an already-processed broadcast must not be redone"
        assert db_session.query(Episode).count() == 1

    def test_skips_it_even_while_the_feed_episode_is_mid_pipeline(
        self, db_session, tmp_path, recordings_dir
    ):
        self._seed_feed_episode(db_session, date(2026, 8, 6), status=EpisodeStatus.TRANSCRIBED)
        make_recording(recordings_dir, date(2026, 8, 6))
        settings = local_settings(tmp_path, recordings_dir)

        assert detect_local_recordings(db_session, settings).new == 0

    def test_matches_on_the_broadcast_day_not_the_upload_day(
        self, db_session, tmp_path, recordings_dir
    ):
        """An upload past midnight still refers to the previous broadcast."""
        episode = self._seed_feed_episode(db_session, date(2026, 8, 6))
        episode.published_at = datetime(2026, 8, 7, 0, 30, tzinfo=UTC)
        db_session.commit()
        make_recording(recordings_dir, date(2026, 8, 6))
        settings = local_settings(tmp_path, recordings_dir)

        assert detect_local_recordings(db_session, settings).new == 0

    def test_still_ingests_a_day_youtube_does_not_have(
        self, db_session, tmp_path, recordings_dir
    ):
        self._seed_feed_episode(db_session, date(2026, 8, 5))
        make_recording(recordings_dir, date(2026, 8, 6))
        settings = local_settings(tmp_path, recordings_dir)

        result = detect_local_recordings(db_session, settings)

        assert result.new == 1
        ids = {ep.episode_id for ep in db_session.query(Episode).all()}
        assert ids == {"ytUpload123", "tagesschau_2026-08-06_2000"}

    def test_unrelated_feed_episodes_do_not_block_the_recorder(
        self, db_session, tmp_path, recordings_dir
    ):
        """A podcast episode with no date in its title must not suppress anything.

        ``published_at`` is set to the day of the recording on purpose: this is
        what happened on 2026-08-13. A Bitcoin podcast episode published that
        afternoon carried no date in its title, the upload timestamp was used as
        a stand-in for the broadcast day, and the tagesschau recording made that
        evening was discarded as a duplicate of it. The recording never reached
        the pipeline and nothing reported an error.
        """
        db_session.add(
            Episode(
                episode_id="podcastXYZ",
                source="youtube_rss",
                title="Diese 7 Fehler kosten dich deine Bitcoin",
                url="https://www.youtube.com/watch?v=podcastXYZ",
                published_at=datetime(2026, 8, 6, 14, 0, tzinfo=UTC),
                status=EpisodeStatus.NEW,
            )
        )
        db_session.commit()
        make_recording(recordings_dir, date(2026, 8, 6))
        settings = local_settings(tmp_path, recordings_dir)

        assert detect_local_recordings(db_session, settings).new == 1

    def test_a_feed_upload_timestamp_never_claims_a_broadcast_day(
        self, db_session, tmp_path, recordings_dir
    ):
        """An upload time says nothing about which broadcast an entry contains.

        The feed re-serves old editions with a current timestamp, so trusting it
        here costs a recording outright — the broadcast simply never runs. Only
        a date in the title or in a recorder slug names the broadcast itself.
        """
        db_session.add(
            Episode(
                episode_id="reupload42",
                source="youtube_rss",
                title="tagesschau 20:00 Uhr",  # no date anywhere
                url="https://www.youtube.com/watch?v=reupload42",
                published_at=datetime(2026, 8, 6, 21, 0, tzinfo=UTC),
                status=EpisodeStatus.NEW,
            )
        )
        db_session.commit()
        make_recording(recordings_dir, date(2026, 8, 6))
        settings = local_settings(tmp_path, recordings_dir)

        assert detect_local_recordings(db_session, settings).new == 1

    def test_another_programme_on_the_same_day_is_not_a_duplicate(
        self, db_session, tmp_path, recordings_dir
    ):
        """Two shows broadcast on one day are not duplicates of each other."""
        db_session.add(
            Episode(
                episode_id="tagesthemen1",
                source="youtube_rss",
                title="tagesthemen 22:15 Uhr, 06.08.2026",
                url="https://www.youtube.com/watch?v=tagesthemen1",
                published_at=datetime(2026, 8, 6, 22, 45, tzinfo=UTC),
                status=EpisodeStatus.NEW,
            )
        )
        db_session.commit()
        make_recording(recordings_dir, date(2026, 8, 6))
        settings = local_settings(tmp_path, recordings_dir)

        assert detect_local_recordings(db_session, settings).new == 1


class TestExtractAudio:
    """Audio extraction replaces the yt-dlp download for local episodes."""

    def _silent_video(self, path: Path, seconds: int = 1) -> Path:
        """A tiny real MP4 with an AAC track, matching the recorder's format."""
        import shutil
        import subprocess

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            pytest.skip("ffmpeg not available")
        subprocess.run(
            [
                ffmpeg, "-nostdin", "-v", "error", "-y",
                "-f", "lavfi", "-i", f"color=c=black:s=64x64:d={seconds}",
                "-f", "lavfi", "-i", f"anullsrc=r=44100:cl=stereo:d={seconds}",
                "-c:v", "libx264", "-c:a", "aac", "-shortest", str(path),
            ],
            check=True,
            capture_output=True,
        )
        return path

    def test_extracts_an_audio_only_file(self, tmp_path):
        video = self._silent_video(tmp_path / "in.mp4")

        out = extract_audio(video, tmp_path / "audio.m4a")

        assert out.exists() and out.stat().st_size > 0

    def test_reuses_an_existing_extraction(self, tmp_path):
        """The timer re-runs often; re-extracting every time would be wasteful."""
        target = tmp_path / "audio.m4a"
        target.write_bytes(b"already here")

        out = extract_audio(tmp_path / "missing.mp4", target)

        assert out.read_bytes() == b"already here"

    def test_leaves_no_partial_file_behind_on_failure(self, tmp_path):
        broken = tmp_path / "broken.mp4"
        broken.write_bytes(b"not a video")

        with pytest.raises(RuntimeError, match="ffmpeg failed"):
            extract_audio(broken, tmp_path / "audio.m4a")

        assert not (tmp_path / "audio.m4a").exists()
        assert not (tmp_path / "audio.m4a.partial").exists()


class TestWeatherVerdictFromTheRecorder:
    """The recorder checks its own cut; this is where a person gets told.

    The forecast is the closing chapter of the finished video. A cut that ate
    it fails silently: the shortened transcript is perfectly coherent, so every
    downstream stage and every review gate passes, and the video is simply
    missing its ending. Nothing but the recorder can tell.
    """

    def _recording(self, recordings_dir, extra: dict | None):
        from btcedu.services.local_recorder_service import scan_recordings

        make_recording(
            recordings_dir,
            date(2026, 8, 6),
            metadata={"extra": extra} if extra is not None else {},
        )
        return scan_recordings(str(recordings_dir))[0]

    def test_reads_a_confirmed_forecast(self, recordings_dir):
        rec = self._recording(
            recordings_dir,
            {"weather_verified": "true", "weather_evidence": "Wetter @ 932.9s (9 Treffer)"},
        )

        assert rec.weather.present is True
        assert rec.weather.truncated is False
        assert "9 Treffer" in rec.weather.evidence

    def test_reads_a_truncated_forecast(self, recordings_dir):
        rec = self._recording(
            recordings_dir,
            {
                "weather_verified": "false",
                "weather_truncated": "true",
                "weather_evidence": "Schauer @ 1002.0s",
            },
        )

        assert rec.weather.present is False
        assert rec.weather.truncated is True

    def test_an_edition_without_a_forecast_is_not_truncated(self, recordings_dir):
        rec = self._recording(recordings_dir, {"weather_verified": "false"})

        assert rec.weather.present is False
        assert rec.weather.truncated is False

    def test_an_older_recorder_says_nothing_rather_than_no(self, recordings_dir):
        """Metadata written before the check existed must not read as a defect."""
        rec = self._recording(recordings_dir, None)

        assert rec.weather.present is None
        assert rec.weather.truncated is False


class TestIncompleteRecordingIsReported:
    TRUNCATED = {
        "weather_verified": "false",
        "weather_truncated": "true",
        "weather_evidence": "Schauer @ 1002.0s … Sonnenschein @ 1032.0s (4 Treffer)",
    }

    def _detect(self, db_session, tmp_path, recordings_dir, extra):
        make_recording(recordings_dir, date(2026, 8, 6), metadata={"extra": extra})
        settings = local_settings(tmp_path, recordings_dir)
        with patch("btcedu.services.notify_service.send_notification") as notify:
            notify.return_value = True
            result = detect_local_recordings(db_session, settings)
        return result, notify

    def test_a_truncated_forecast_reaches_whatsapp(
        self, db_session, tmp_path, recordings_dir
    ):
        result, notify = self._detect(db_session, tmp_path, recordings_dir, self.TRUNCATED)

        assert result.new == 1, "the recording is still ingested; this reports, it does not block"
        notify.assert_called_once()
        message = notify.call_args.args[1]
        assert "tagesschau_2026-08-06_2000" in message
        assert "Schauer @ 1002.0s" in message

    def test_a_confirmed_forecast_is_not_worth_a_message(
        self, db_session, tmp_path, recordings_dir
    ):
        _, notify = self._detect(
            db_session, tmp_path, recordings_dir, {"weather_verified": "true"}
        )

        notify.assert_not_called()

    def test_an_edition_without_a_forecast_is_not_alarmed_about(
        self, db_session, tmp_path, recordings_dir
    ):
        """2026-08-12 had no forecast at all — the playout dropped it.

        Nothing about the recording can fix that, and alarming about it would
        train the reader to ignore the message that does matter.
        """
        _, notify = self._detect(
            db_session, tmp_path, recordings_dir, {"weather_verified": "false"}
        )

        notify.assert_not_called()

    def test_the_message_is_sent_once_not_on_every_timer_run(
        self, db_session, tmp_path, recordings_dir
    ):
        """The timer fires every ten minutes; only the ingest may report."""
        make_recording(recordings_dir, date(2026, 8, 6), metadata={"extra": self.TRUNCATED})
        settings = local_settings(tmp_path, recordings_dir)

        with patch("btcedu.services.notify_service.send_notification") as notify:
            notify.return_value = True
            detect_local_recordings(db_session, settings)
            detect_local_recordings(db_session, settings)

        assert notify.call_count == 1

    def test_a_broken_notifier_does_not_cost_the_ingest(
        self, db_session, tmp_path, recordings_dir
    ):
        make_recording(recordings_dir, date(2026, 8, 6), metadata={"extra": self.TRUNCATED})
        settings = local_settings(tmp_path, recordings_dir)

        with patch(
            "btcedu.services.notify_service.send_notification",
            side_effect=RuntimeError("whatsapp service is down"),
        ):
            result = detect_local_recordings(db_session, settings)

        assert result.new == 1
        assert db_session.query(Episode).count() == 1
