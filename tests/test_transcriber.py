"""Tests for transcription pipeline stage."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from btcedu.config import Settings
from btcedu.core.transcriber import load_transcript_document, transcribe_episode
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage
from btcedu.models.transcript_schema import (
    TranscriptDocument,
    TranscriptSegment,
    TranscriptUsage,
)


def _make_settings(tmp_path: Path) -> Settings:
    return Settings(
        whisper_api_key="sk-test-fake",
        transcripts_dir=str(tmp_path / "transcripts"),
        raw_data_dir=str(tmp_path / "raw"),
        outputs_dir=str(tmp_path / "outputs"),
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


def _structured_transcript(episode_id: str, text: str) -> TranscriptDocument:
    return TranscriptDocument(
        episode_id=episode_id,
        provider="openai",
        model="whisper-1",
        language="de",
        text=text,
        segments=[
            TranscriptSegment(
                segment_id="seg-0001",
                start_seconds=0,
                end_seconds=2.5,
                text=text,
                confidence=0.9,
            )
        ],
        usage=TranscriptUsage(audio_seconds=2.5, cost_usd=0.00025),
    )


class TestTranscribeEpisode:
    @patch("btcedu.services.transcription_service.transcribe_audio_structured")
    def test_creates_transcript_files(self, mock_whisper, db_session, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_downloaded_episode(db_session, tmp_path)
        mock_whisper.return_value = _structured_transcript(
            "ep001", "Bitcoin ist eine dezentrale Waehrung."
        )

        path = transcribe_episode(db_session, "ep001", settings)

        transcript_dir = tmp_path / "transcripts" / "ep001"
        assert (transcript_dir / "transcript.de.txt").exists()
        assert (transcript_dir / "transcript.clean.de.txt").exists()
        assert (transcript_dir / "transcript.structured.de.json").exists()
        assert path == str(transcript_dir / "transcript.clean.de.txt")

    @patch("btcedu.services.transcription_service.transcribe_audio_structured")
    def test_updates_status_to_transcribed(self, mock_whisper, db_session, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_downloaded_episode(db_session, tmp_path)
        mock_whisper.return_value = _structured_transcript("ep001", "Test transcript text.")

        transcribe_episode(db_session, "ep001", settings)

        ep = db_session.query(Episode).filter_by(episode_id="ep001").first()
        assert ep.status == EpisodeStatus.TRANSCRIBED
        assert ep.transcript_path is not None
        run = db_session.query(PipelineRun).filter_by(stage=PipelineStage.TRANSCRIBE).one()
        assert run.estimated_cost_usd == 0.00025

    @patch("btcedu.services.transcription_service.transcribe_audio_structured")
    def test_stores_transcript_path_in_db(self, mock_whisper, db_session, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_downloaded_episode(db_session, tmp_path)
        mock_whisper.return_value = _structured_transcript("ep001", "Some text.")

        transcribe_episode(db_session, "ep001", settings)

        ep = db_session.query(Episode).filter_by(episode_id="ep001").first()
        assert "transcript.clean.de.txt" in ep.transcript_path

    @patch("btcedu.services.transcription_service.transcribe_audio_structured")
    def test_skips_if_transcript_exists(self, mock_whisper, db_session, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_downloaded_episode(db_session, tmp_path)
        mock_whisper.return_value = _structured_transcript("ep001", "existing")
        path = transcribe_episode(db_session, "ep001", settings)
        mock_whisper.reset_mock()
        path = transcribe_episode(db_session, "ep001", settings)
        mock_whisper.assert_not_called()
        assert path == str(tmp_path / "transcripts" / "ep001" / "transcript.clean.de.txt")

    @pytest.mark.parametrize("missing_artifact", ["structured", "provenance"])
    @patch("btcedu.services.transcription_service.transcribe_audio_structured")
    def test_incomplete_cache_is_regenerated(
        self, mock_whisper, missing_artifact, db_session, tmp_path
    ):
        settings = _make_settings(tmp_path)
        _seed_downloaded_episode(db_session, tmp_path)
        mock_whisper.return_value = _structured_transcript("ep001", "restored")
        transcribe_episode(db_session, "ep001", settings)

        if missing_artifact == "structured":
            missing_path = tmp_path / "transcripts" / "ep001" / "transcript.structured.de.json"
        else:
            missing_path = (
                tmp_path / "outputs" / "ep001" / "provenance" / "transcribe_provenance.json"
            )
        missing_path.unlink()

        transcribe_episode(db_session, "ep001", settings)

        assert mock_whisper.call_count == 2
        assert missing_path.is_file()

    @pytest.mark.parametrize(
        "invalid_provenance",
        [
            [],
            {"output_files": 1},
            {"output_files": [123]},
        ],
    )
    @patch("btcedu.services.transcription_service.transcribe_audio_structured")
    def test_malformed_provenance_shape_is_regenerated(
        self, mock_whisper, invalid_provenance, db_session, tmp_path
    ):
        settings = _make_settings(tmp_path)
        _seed_downloaded_episode(db_session, tmp_path)
        mock_whisper.return_value = _structured_transcript("ep001", "restored")
        transcribe_episode(db_session, "ep001", settings)

        provenance_path = (
            tmp_path / "outputs" / "ep001" / "provenance" / "transcribe_provenance.json"
        )
        provenance_path.write_text(json.dumps(invalid_provenance), encoding="utf-8")

        transcribe_episode(db_session, "ep001", settings)

        assert mock_whisper.call_count == 2

    @patch("btcedu.services.transcription_service.transcribe_audio_structured")
    def test_force_retranscribes(self, mock_whisper, db_session, tmp_path):
        settings = _make_settings(tmp_path)
        _seed_downloaded_episode(db_session, tmp_path)
        mock_whisper.return_value = _structured_transcript("ep001", "New transcript.")

        # Pre-create transcript
        transcript_dir = tmp_path / "transcripts" / "ep001"
        transcript_dir.mkdir(parents=True)
        (transcript_dir / "transcript.clean.de.txt").write_text("old")

        transcribe_episode(db_session, "ep001", settings, force=True)

        mock_whisper.assert_called_once()
        content = (transcript_dir / "transcript.clean.de.txt").read_text()
        assert content == "New transcript."

    def test_loads_legacy_text_without_structured_artifact(self, tmp_path):
        settings = _make_settings(tmp_path)
        transcript_dir = tmp_path / "transcripts" / "legacy"
        transcript_dir.mkdir(parents=True)
        (transcript_dir / "transcript.clean.de.txt").write_text(
            "Erster Satz. Zweiter Satz!",
            encoding="utf-8",
        )

        document = load_transcript_document(settings, "legacy")

        assert document.provider == "legacy"
        assert document.text == "Erster Satz. Zweiter Satz!"
        assert [segment.segment_id for segment in document.segments] == [
            "seg-0001",
            "seg-0002",
        ]

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

        with pytest.raises(ValueError, match="No OpenAI transcription key"):
            transcribe_episode(db_session, "ep001", settings)
