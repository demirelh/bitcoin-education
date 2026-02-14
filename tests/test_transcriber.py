"""Tests for transcription pipeline stage."""

from pathlib import Path
from unittest.mock import patch

from btcedu.config import Settings
from btcedu.core.transcriber import transcribe_episode
from btcedu.models.episode import Episode, EpisodeStatus


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        whisper_api_key="sk-test-fake",
        transcripts_dir=str(tmp_path / "transcripts"),
        raw_data_dir=str(tmp_path / "raw"),
        audio_format="m4a",
    )


def _seed_downloaded_episode(db_session, tmp_path, episode_id="ep001"):
    """Create an episode in DOWNLOADED state with a fake audio file."""
    audio_dir = tmp_path / "raw" / episode_id
    audio_dir.mkdir(parents=True)
    audio_file = audio_dir / "audio.m4a"
    audio_file.write_bytes(b"fake audio content")

    ep = Episode(
        episode_id=episode_id,
        source="youtube_rss",
        title="Test Episode",
        url=f"https://youtube.com/watch?v={episode_id}",
        status=EpisodeStatus.DOWNLOADED,
        audio_path=str(audio_file),
    )
    db_session.add(ep)
    db_session.commit()
    return ep


class TestTranscribeEpisode:
    @patch("btcedu.services.transcription_service.transcribe_audio")
    def test_creates_transcript_files(self, mock_whisper, db_session, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_downloaded_episode(db_session, tmp_path)
        mock_whisper.return_value = "Bitcoin ist eine dezentrale Waehrung."

        path = transcribe_episode(db_session, "ep001", settings)

        transcript_dir = tmp_path / "transcripts" / "ep001"
        assert (transcript_dir / "transcript.de.txt").exists()
        assert (transcript_dir / "transcript.clean.de.txt").exists()
        assert path == str(transcript_dir / "transcript.clean.de.txt")

    @patch("btcedu.services.transcription_service.transcribe_audio")
    def test_updates_status_to_transcribed(self, mock_whisper, db_session, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_downloaded_episode(db_session, tmp_path)
        mock_whisper.return_value = "Test transcript text."

        transcribe_episode(db_session, "ep001", settings)

        ep = db_session.query(Episode).filter_by(episode_id="ep001").first()
        assert ep.status == EpisodeStatus.TRANSCRIBED
        assert ep.transcript_path is not None

    @patch("btcedu.services.transcription_service.transcribe_audio")
    def test_stores_transcript_path_in_db(self, mock_whisper, db_session, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_downloaded_episode(db_session, tmp_path)
        mock_whisper.return_value = "Some text."

        transcribe_episode(db_session, "ep001", settings)

        ep = db_session.query(Episode).filter_by(episode_id="ep001").first()
        assert "transcript.clean.de.txt" in ep.transcript_path

    @patch("btcedu.services.transcription_service.transcribe_audio")
    def test_skips_if_transcript_exists(self, mock_whisper, db_session, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_downloaded_episode(db_session, tmp_path)

        # Pre-create transcript file
        transcript_dir = tmp_path / "transcripts" / "ep001"
        transcript_dir.mkdir(parents=True)
        (transcript_dir / "transcript.clean.de.txt").write_text("existing")

        path = transcribe_episode(db_session, "ep001", settings)

        mock_whisper.assert_not_called()
        assert path == str(transcript_dir / "transcript.clean.de.txt")

    @patch("btcedu.services.transcription_service.transcribe_audio")
    def test_force_retranscribes(self, mock_whisper, db_session, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_downloaded_episode(db_session, tmp_path)
        mock_whisper.return_value = "New transcript."

        # Pre-create transcript
        transcript_dir = tmp_path / "transcripts" / "ep001"
        transcript_dir.mkdir(parents=True)
        (transcript_dir / "transcript.clean.de.txt").write_text("old")

        transcribe_episode(db_session, "ep001", settings, force=True)

        mock_whisper.assert_called_once()
        content = (transcript_dir / "transcript.clean.de.txt").read_text()
        assert content == "New transcript."

    def test_raises_for_unknown_episode(self, db_session, tmp_path):
        import pytest

        settings = _make_settings(tmp_path)
        with pytest.raises(ValueError, match="Episode not found"):
            transcribe_episode(db_session, "nonexistent", settings)

    def test_raises_for_wrong_status(self, db_session, tmp_path):
        import pytest

        settings = _make_settings(tmp_path)
        ep = Episode(
            episode_id="ep001",
            source="youtube_rss",
            title="Test",
            url="https://youtube.com/watch?v=ep001",
            status=EpisodeStatus.NEW,
        )
        db_session.add(ep)
        db_session.commit()

        with pytest.raises(ValueError, match="expected 'downloaded'"):
            transcribe_episode(db_session, "ep001", settings)

    def test_raises_without_api_key(self, db_session, tmp_path):
        import pytest

        settings = Settings(
            whisper_api_key="",
            openai_api_key="",
            transcripts_dir=str(tmp_path / "transcripts"),
            raw_data_dir=str(tmp_path / "raw"),
        )
        _seed_downloaded_episode(db_session, tmp_path)

        with pytest.raises(ValueError, match="No Whisper API key"):
            transcribe_episode(db_session, "ep001", settings)
