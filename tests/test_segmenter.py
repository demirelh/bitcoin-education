"""Tests for the story segmentation module (tagesschau news pipeline)."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus
from btcedu.models.prompt_version import PromptVersion  # noqa: F401 — ensure table created
from btcedu.models.story_schema import (
    StoryCategory,
    StoryDocument,
    StoryType,
)

# ---------------------------------------------------------------------------
# Helpers: sample StoryDocument
# ---------------------------------------------------------------------------

SAMPLE_ATTRIBUTION = {
    "source": "tagesschau",
    "broadcaster": "ARD/Das Erste",
    "broadcast_date": "2026-03-16",
    "broadcast_time": "20:00 CET",
    "original_language": "de",
    "original_url": "",
    "attribution_text_tr": (
        "Kaynak: ARD tagesschau, 16.03.2026 — Türkçe çeviri btcedu tarafından hazırlanmıştır."
    ),
    "attribution_text_de": "Quelle: ARD tagesschau, 16.03.2026",
}


def _make_story_doc(episode_id: str, n_stories: int = 3) -> dict:
    """Build a valid StoryDocument dict."""
    stories = []
    for i in range(1, n_stories + 1):
        stories.append({
            "story_id": f"s{i:02d}",
            "order": i,
            "headline_de": f"Schlagzeile {i}",
            "category": "politik",
            "story_type": "meldung",
            "text_de": f"Dies ist der Text der Geschichte Nummer {i}. Mehr Details folgen.",
            "word_count": 10,
            "estimated_duration_seconds": 5,
            "reporter": None,
            "location": None,
            "is_lead_story": i == 1,
            "headline_tr": None,
            "text_tr": None,
        })
    return {
        "schema_version": "1.0",
        "episode_id": episode_id,
        "broadcast_date": "2026-03-16",
        "source_attribution": SAMPLE_ATTRIBUTION,
        "total_stories": n_stories,
        "total_duration_seconds": 5 * n_stories,
        "stories": stories,
    }


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def corrected_episode(db_session, tmp_path):
    """Episode at CORRECTED status with transcript files."""
    transcript_dir = tmp_path / "transcripts" / "ep_news"
    transcript_dir.mkdir(parents=True)
    corrected_path = transcript_dir / "transcript.corrected.de.txt"
    corrected_path.write_text(
        "Guten Abend, meine Damen und Herren. Hier ist die tagesschau.\n\n"
        "Der Bundestag hat heute über den Haushalt abgestimmt.\n\n"
        "In der Außenpolitik gibt es neue Entwicklungen.",
        encoding="utf-8",
    )

    episode = Episode(
        episode_id="ep_news",
        source="youtube_rss",
        title="tagesschau 20:00 Uhr, 16.03.2026",
        url="https://youtube.com/watch?v=ep_news",
        status=EpisodeStatus.CORRECTED,
        transcript_path=str(transcript_dir / "transcript.clean.de.txt"),
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    db_session.add(episode)
    db_session.commit()
    return episode


@pytest.fixture
def mock_settings(tmp_path):
    """Settings with tmp_path dirs."""
    from btcedu.config import Settings

    return Settings(
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        dry_run=False,
        anthropic_api_key="test-key",
        pipeline_version=2,
        profiles_dir="btcedu/profiles",
    )


@pytest.fixture(autouse=True)
def reset_profile_registry():
    """Reset profile registry singleton between tests."""
    from btcedu.profiles import reset_registry

    reset_registry()
    yield
    reset_registry()


# ---------------------------------------------------------------------------
# Unit tests: StoryDocument validation
# ---------------------------------------------------------------------------


class TestStoryDocumentValidation:
    def test_valid_story_document(self):
        doc = StoryDocument.model_validate(_make_story_doc("ep001", 3))
        assert doc.total_stories == 3
        assert len(doc.stories) == 3
        assert doc.stories[0].is_lead_story is True

    def test_story_document_invalid_total_stories(self):
        from pydantic import ValidationError

        data = _make_story_doc("ep001", 3)
        data["total_stories"] = 5  # Wrong count
        with pytest.raises(ValidationError, match="total_stories"):
            StoryDocument.model_validate(data)

    def test_story_document_duplicate_story_id(self):
        from pydantic import ValidationError

        data = _make_story_doc("ep001", 3)
        data["stories"][1]["story_id"] = "s01"  # Duplicate
        with pytest.raises(ValidationError, match="Duplicate story_id"):
            StoryDocument.model_validate(data)

    def test_story_document_invalid_order(self):
        from pydantic import ValidationError

        data = _make_story_doc("ep001", 3)
        data["stories"][1]["order"] = 5  # Gap: 1, 5, 3
        with pytest.raises(ValidationError, match="sequential"):
            StoryDocument.model_validate(data)

    def test_story_category_enum(self):
        assert StoryCategory.POLITIK == "politik"
        assert StoryCategory.INTERNATIONAL == "international"
        assert StoryCategory.WETTER == "wetter"
        assert StoryCategory.META == "meta"

    def test_story_type_enum(self):
        assert StoryType.MELDUNG == "meldung"
        assert StoryType.INTRO == "intro"
        assert StoryType.OUTRO == "outro"

    def test_optional_fields_default(self):
        doc_data = _make_story_doc("ep001", 1)
        doc = StoryDocument.model_validate(doc_data)
        story = doc.stories[0]
        assert story.reporter is None
        assert story.location is None
        assert story.headline_tr is None
        assert story.text_tr is None
        assert story.is_lead_story is True


# ---------------------------------------------------------------------------
# Segmenter unit tests
# ---------------------------------------------------------------------------


class TestSegmentBroadcast:
    def _make_mock_claude_response(self, episode_id: str, n_stories: int = 3):
        """Build a mock ClaudeResponse with valid StoryDocument JSON."""
        mock_resp = MagicMock()
        mock_resp.text = json.dumps(_make_story_doc(episode_id, n_stories))
        mock_resp.input_tokens = 200
        mock_resp.output_tokens = 500
        mock_resp.cost_usd = 0.01
        return mock_resp

    def test_segment_broadcast_happy_path(self, db_session, corrected_episode, mock_settings):
        """Segment a corrected news episode → stories.json written, status=SEGMENTED."""
        from btcedu.core.segmenter import segment_broadcast

        mock_resp = self._make_mock_claude_response("ep_news", 3)

        with patch("btcedu.core.segmenter.call_claude", return_value=mock_resp):
            result = segment_broadcast(db_session, "ep_news", mock_settings, force=False)

        assert result.episode_id == "ep_news"
        assert result.story_count == 3
        assert result.skipped is False
        assert result.cost_usd == pytest.approx(0.01)
        assert Path(result.stories_path).exists()

        # Episode status updated
        db_session.refresh(corrected_episode)
        assert corrected_episode.status == EpisodeStatus.SEGMENTED

        # stories.json valid
        stories_data = json.loads(Path(result.stories_path).read_text(encoding="utf-8"))
        doc = StoryDocument.model_validate(stories_data)
        assert doc.total_stories == 3

        # Provenance written
        assert Path(result.provenance_path).exists()
        provenance = json.loads(Path(result.provenance_path).read_text(encoding="utf-8"))
        assert provenance["stage"] == "segment"
        assert provenance["story_count"] == 3

        # PipelineRun created
        run = db_session.query(PipelineRun).filter(
            PipelineRun.episode_id == corrected_episode.id,
            PipelineRun.stage == PipelineStage.SEGMENT,
        ).first()
        assert run is not None
        assert run.status == RunStatus.SUCCESS

        # Stale marker written for translation
        stale_path = (
            Path(mock_settings.transcripts_dir) / "ep_news" / "transcript.tr.txt.stale"
        )
        assert stale_path.exists()

    def test_segment_broadcast_idempotent(self, db_session, corrected_episode, mock_settings):
        """Running twice without force returns skipped=True on second run."""
        from btcedu.core.segmenter import segment_broadcast

        mock_resp = self._make_mock_claude_response("ep_news", 3)

        with patch("btcedu.core.segmenter.call_claude", return_value=mock_resp):
            result1 = segment_broadcast(db_session, "ep_news", mock_settings, force=False)
        assert result1.skipped is False

        with patch("btcedu.core.segmenter.call_claude", return_value=mock_resp) as mock_call:
            result2 = segment_broadcast(db_session, "ep_news", mock_settings, force=False)
        # Claude should NOT be called on idempotent run
        mock_call.assert_not_called()
        assert result2.skipped is True

    def test_segment_broadcast_force(self, db_session, corrected_episode, mock_settings):
        """force=True re-runs even when output is current."""
        from btcedu.core.segmenter import segment_broadcast

        mock_resp = self._make_mock_claude_response("ep_news", 3)

        with patch("btcedu.core.segmenter.call_claude", return_value=mock_resp):
            result1 = segment_broadcast(db_session, "ep_news", mock_settings, force=False)
        assert result1.skipped is False

        with patch("btcedu.core.segmenter.call_claude", return_value=mock_resp) as mock_call:
            result2 = segment_broadcast(db_session, "ep_news", mock_settings, force=True)
        # Claude SHOULD be called again
        mock_call.assert_called_once()
        assert result2.skipped is False

    def test_segment_broadcast_wrong_status(self, db_session, tmp_path):
        """Episode not in CORRECTED/SEGMENTED → raises ValueError."""
        from btcedu.core.segmenter import segment_broadcast

        episode = Episode(
            episode_id="ep_wrong",
            source="youtube_rss",
            title="Wrong status episode",
            url="https://youtube.com/watch?v=ep_wrong",
            status=EpisodeStatus.TRANSLATED,
            transcript_path="/tmp/transcript.txt",
            pipeline_version=2,
            content_profile="tagesschau_tr",
        )
        db_session.add(episode)
        db_session.commit()

        from btcedu.config import Settings

        settings = Settings(
            transcripts_dir=str(tmp_path / "transcripts"),
            outputs_dir=str(tmp_path / "outputs"),
            reports_dir=str(tmp_path / "reports"),
            anthropic_api_key="test-key",
            pipeline_version=2,
            profiles_dir="btcedu/profiles",
        )

        with pytest.raises(ValueError, match="expected 'corrected'"):
            segment_broadcast(db_session, "ep_wrong", settings, force=False)

    def test_segment_broadcast_profile_not_enabled(self, db_session, tmp_path):
        """Profile without segment.enabled → skips with skipped=True."""
        from btcedu.config import Settings
        from btcedu.core.segmenter import segment_broadcast

        transcript_dir = tmp_path / "transcripts" / "ep_bitcoin"
        transcript_dir.mkdir(parents=True)
        corrected_path = transcript_dir / "transcript.corrected.de.txt"
        corrected_path.write_text("Bitcoin ist eine digitale Währung.", encoding="utf-8")

        episode = Episode(
            episode_id="ep_bitcoin",
            source="youtube_rss",
            title="Bitcoin Episode",
            url="https://youtube.com/watch?v=ep_bitcoin",
            status=EpisodeStatus.CORRECTED,
            transcript_path=str(transcript_dir / "transcript.clean.de.txt"),
            pipeline_version=2,
            content_profile="bitcoin_podcast",  # no segment enabled
        )
        db_session.add(episode)
        db_session.commit()

        settings = Settings(
            transcripts_dir=str(tmp_path / "transcripts"),
            outputs_dir=str(tmp_path / "outputs"),
            reports_dir=str(tmp_path / "reports"),
            anthropic_api_key="test-key",
            pipeline_version=2,
            profiles_dir="btcedu/profiles",
        )

        result = segment_broadcast(db_session, "ep_bitcoin", settings, force=False)
        assert result.skipped is True

    def test_segment_broadcast_episode_not_found(self, db_session, mock_settings):
        """Non-existent episode → raises ValueError."""
        from btcedu.core.segmenter import segment_broadcast

        with pytest.raises(ValueError, match="Episode not found"):
            segment_broadcast(db_session, "nonexistent", mock_settings)
