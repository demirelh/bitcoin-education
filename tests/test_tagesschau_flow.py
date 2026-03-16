"""Integration tests for the tagesschau news pipeline flow."""

import json
from unittest.mock import MagicMock, patch

import pytest

from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.prompt_version import PromptVersion  # noqa: F401 — ensure table created
from btcedu.models.story_schema import StoryDocument

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def reset_profile_registry():
    """Reset profile registry singleton between tests."""
    from btcedu.profiles import reset_registry

    reset_registry()
    yield
    reset_registry()


@pytest.fixture
def settings_with_profiles(tmp_path):
    """Settings pointing to real profiles dir."""
    from btcedu.config import Settings

    return Settings(
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        anthropic_api_key="test-key",
        pipeline_version=2,
        profiles_dir="btcedu/profiles",
    )


@pytest.fixture
def bitcoin_episode(db_session):
    """Episode with bitcoin_podcast profile at ADAPTED status."""
    episode = Episode(
        episode_id="ep_btc",
        source="youtube_rss",
        title="Bitcoin Episode",
        url="https://youtube.com/watch?v=ep_btc",
        status=EpisodeStatus.ADAPTED,
        transcript_path="/tmp/transcript.txt",
        pipeline_version=2,
        content_profile="bitcoin_podcast",
    )
    db_session.add(episode)
    db_session.commit()
    return episode


@pytest.fixture
def tagesschau_episode(db_session):
    """Episode with tagesschau_tr profile at CORRECTED status."""
    episode = Episode(
        episode_id="ep_ts",
        source="youtube_rss",
        title="tagesschau 20:00 Uhr",
        url="https://youtube.com/watch?v=ep_ts",
        status=EpisodeStatus.CORRECTED,
        transcript_path="/tmp/transcript.txt",
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    db_session.add(episode)
    db_session.commit()
    return episode


# ---------------------------------------------------------------------------
# _get_stages profile-aware tests
# ---------------------------------------------------------------------------


class TestGetStages:
    def test_get_stages_bitcoin_podcast(self, bitcoin_episode, settings_with_profiles):
        """bitcoin_podcast episode: no segment, has adapt and review_gate_2."""
        from btcedu.core.pipeline import _get_stages

        stages = _get_stages(settings_with_profiles, bitcoin_episode)
        stage_names = [n for n, _ in stages]

        assert "segment" not in stage_names
        assert "adapt" in stage_names
        assert "review_gate_2" in stage_names
        assert "chapterize" in stage_names

        # translate requires CORRECTED (not SEGMENTED) for bitcoin
        translate_stage = next((s, r) for s, r in stages if s == "translate")
        assert translate_stage[1] == EpisodeStatus.CORRECTED

    def test_get_stages_tagesschau_tr(self, tagesschau_episode, settings_with_profiles):
        """tagesschau_tr episode: has segment, no adapt, no review_gate_2, chapterize=TRANSLATED."""
        from btcedu.core.pipeline import _get_stages

        stages = _get_stages(settings_with_profiles, tagesschau_episode)
        stage_names = [n for n, _ in stages]

        assert "segment" in stage_names
        assert "adapt" not in stage_names
        assert "review_gate_2" not in stage_names
        assert "chapterize" in stage_names

        # segment requires CORRECTED
        segment_stage = next((s, r) for s, r in stages if s == "segment")
        assert segment_stage[1] == EpisodeStatus.CORRECTED

        # translate requires SEGMENTED (not CORRECTED) for tagesschau
        translate_stage = next((s, r) for s, r in stages if s == "translate")
        assert translate_stage[1] == EpisodeStatus.SEGMENTED

        # chapterize requires TRANSLATED (not ADAPTED) for tagesschau
        chapterize_stage = next((s, r) for s, r in stages if s == "chapterize")
        assert chapterize_stage[1] == EpisodeStatus.TRANSLATED

    def test_get_stages_no_episode(self, settings_with_profiles):
        """With no episode, returns default v2 stages."""
        from btcedu.core.pipeline import _V2_STAGES, _get_stages

        stages = _get_stages(settings_with_profiles, None)
        assert stages == _V2_STAGES


# ---------------------------------------------------------------------------
# Translator per-story mode tests
# ---------------------------------------------------------------------------


class TestTranslatorPerStoryMode:
    def _make_story_doc(self, episode_id: str, n: int = 2) -> dict:
        stories = []
        for i in range(1, n + 1):
            stories.append({
                "story_id": f"s{i:02d}",
                "order": i,
                "headline_de": f"Schlagzeile {i}",
                "category": "politik",
                "story_type": "meldung",
                "text_de": f"Der Bundestag hat Geschichte {i} beschlossen.",
                "word_count": 8,
                "estimated_duration_seconds": 4,
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
            "source_attribution": {"source": "tagesschau"},
            "total_stories": n,
            "total_duration_seconds": 4 * n,
            "stories": stories,
        }

    def test_translator_per_story_mode(
        self, db_session, tagesschau_episode, settings_with_profiles, tmp_path
    ):
        """When stories.json exists + per_story profile, translator produces stories_translated.json."""  # noqa: E501
        # Setup: update episode to SEGMENTED, create corrected transcript + stories.json
        tagesschau_episode.status = EpisodeStatus.SEGMENTED
        db_session.commit()

        transcript_dir = tmp_path / "transcripts" / "ep_ts"
        transcript_dir.mkdir(parents=True)
        corrected_path = transcript_dir / "transcript.corrected.de.txt"
        corrected_path.write_text("Der Bundestag hat heute abgestimmt.", encoding="utf-8")

        outputs_dir = tmp_path / "outputs" / "ep_ts"
        outputs_dir.mkdir(parents=True)
        stories_json_path = outputs_dir / "stories.json"
        stories_json_path.write_text(
            json.dumps(self._make_story_doc("ep_ts", 2), ensure_ascii=False), encoding="utf-8"
        )

        # Mock approved review so translate can proceed
        from btcedu.models.review import ReviewStatus, ReviewTask

        review = ReviewTask(
            episode_id="ep_ts",
            stage="correct",
            artifact_paths=json.dumps([]),
            status=ReviewStatus.APPROVED.value,
        )
        db_session.add(review)
        db_session.commit()

        mock_resp = MagicMock()
        mock_resp.text = "Çeviri metni"
        mock_resp.input_tokens = 50
        mock_resp.output_tokens = 20
        mock_resp.cost_usd = 0.001

        from btcedu.core.translator import translate_transcript

        with patch("btcedu.core.translator.call_claude", return_value=mock_resp):
            result = translate_transcript(
                db_session, "ep_ts", settings_with_profiles, force=False
            )

        assert result.skipped is False

        # Check stories_translated.json was produced
        stories_translated_path = outputs_dir / "stories_translated.json"
        assert stories_translated_path.exists(), "stories_translated.json should be created"

        stories_translated = json.loads(
            stories_translated_path.read_text(encoding="utf-8")
        )
        doc = StoryDocument.model_validate(stories_translated)
        assert doc.total_stories == 2
        for story in doc.stories:
            assert story.text_tr is not None
            assert story.headline_tr is not None

        # Concatenated transcript also written
        tr_path = tmp_path / "transcripts" / "ep_ts" / "transcript.tr.txt"
        assert tr_path.exists()

        # Provenance records per_story_mode=True
        provenance_path = outputs_dir / "provenance" / "translate_provenance.json"
        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        assert provenance.get("per_story_mode") is True


# ---------------------------------------------------------------------------
# Chapterizer story mode test
# ---------------------------------------------------------------------------


class TestChastorizerStoryMode:
    def _make_stories_translated(self, episode_id: str) -> dict:
        return {
            "schema_version": "1.0",
            "episode_id": episode_id,
            "broadcast_date": "2026-03-16",
            "source_attribution": {"source": "tagesschau"},
            "total_stories": 2,
            "total_duration_seconds": 60,
            "stories": [
                {
                    "story_id": "s01",
                    "order": 1,
                    "headline_de": "Bundestag stimmt ab",
                    "category": "politik",
                    "story_type": "meldung",
                    "text_de": "Der Bundestag hat heute abgestimmt.",
                    "word_count": 6,
                    "estimated_duration_seconds": 30,
                    "reporter": None,
                    "location": None,
                    "is_lead_story": True,
                    "headline_tr": "Bundestag oylama yaptı",
                    "text_tr": "Bundestag (Almanya Federal Meclisi) bugün oylama yaptı.",
                },
                {
                    "story_id": "s02",
                    "order": 2,
                    "headline_de": "Wirtschaftsnachrichten",
                    "category": "wirtschaft",
                    "story_type": "bericht",
                    "text_de": "Die Wirtschaft wächst um zwei Prozent.",
                    "word_count": 6,
                    "estimated_duration_seconds": 30,
                    "reporter": None,
                    "location": None,
                    "is_lead_story": False,
                    "headline_tr": "Ekonomi haberleri",
                    "text_tr": "Ekonomi yüzde iki büyüdü.",
                },
            ],
        }

    def _make_chapter_response(self, episode_id: str) -> str:
        return json.dumps({
            "schema_version": "1.0",
            "episode_id": episode_id,
            "title": "tagesschau 20:00 Uhr",
            "total_chapters": 2,
            "estimated_duration_seconds": 60,
            "chapters": [
                {
                    "chapter_id": "ch01",
                    "title": "Bundestag oylama yaptı",
                    "order": 1,
                    "narration": {
                        "text": "Bundestag (Almanya Federal Meclisi) bugün oylama yaptı.",
                        "word_count": 8,
                        "estimated_duration_seconds": 3,
                    },
                    "visual": {
                        "type": "b_roll",
                        "description": "Bundestag building",
                        "image_prompt": "German parliament building exterior",
                    },
                    "overlays": [
                        {
                            "type": "lower_third",
                            "text": "Kaynak: ARD tagesschau — btcedu Türkçe",
                            "start_offset_seconds": 1.0,
                            "duration_seconds": 5.0,
                        }
                    ],
                    "transitions": {"in": "fade", "out": "cut"},
                    "notes": None,
                },
                {
                    "chapter_id": "ch02",
                    "title": "Ekonomi haberleri",
                    "order": 2,
                    "narration": {
                        "text": "Ekonomi yüzde iki büyüdü.",
                        "word_count": 4,
                        "estimated_duration_seconds": 2,
                    },
                    "visual": {
                        "type": "b_roll",
                        "description": "Economy chart",
                        "image_prompt": "German economy growth chart",
                    },
                    "overlays": [
                        {
                            "type": "lower_third",
                            "text": "Kaynak: ARD tagesschau — btcedu Türkçe",
                            "start_offset_seconds": 1.0,
                            "duration_seconds": 5.0,
                        }
                    ],
                    "transitions": {"in": "fade", "out": "cut"},
                    "notes": None,
                },
            ],
        })

    def test_chapterizer_story_mode(
        self, db_session, tagesschau_episode, settings_with_profiles, tmp_path
    ):
        """Chapterizer uses stories_translated.json when available (tagesschau profile)."""
        # Setup: episode at TRANSLATED status with stories_translated.json
        tagesschau_episode.status = EpisodeStatus.TRANSLATED
        db_session.commit()

        outputs_dir = tmp_path / "outputs" / "ep_ts"
        outputs_dir.mkdir(parents=True)
        stories_translated_path = outputs_dir / "stories_translated.json"
        stories_translated_path.write_text(
            json.dumps(self._make_stories_translated("ep_ts"), ensure_ascii=False),
            encoding="utf-8",
        )

        mock_resp = MagicMock()
        mock_resp.text = self._make_chapter_response("ep_ts")
        mock_resp.input_tokens = 300
        mock_resp.output_tokens = 800
        mock_resp.cost_usd = 0.02

        from btcedu.core.chapterizer import chapterize_script

        with patch("btcedu.core.chapterizer.call_claude", return_value=mock_resp):
            result = chapterize_script(
                db_session, "ep_ts", settings_with_profiles, force=False
            )

        assert result.chapter_count == 2
        assert result.skipped is False

        chapters_path = outputs_dir / "chapters.json"
        assert chapters_path.exists()

        # Episode status updated
        db_session.refresh(tagesschau_episode)
        assert tagesschau_episode.status == EpisodeStatus.CHAPTERIZED


# ---------------------------------------------------------------------------
# Corrector profile prompt test
# ---------------------------------------------------------------------------


class TestCorrectorProfilePrompt:
    def test_corrector_uses_profile_prompt(
        self, db_session, settings_with_profiles, tmp_path
    ):
        """Corrector resolves profile-namespaced prompt for tagesschau_tr episodes."""
        from btcedu.core.prompt_registry import TEMPLATES_DIR, PromptRegistry

        # Verify the tagesschau_tr correct_transcript.md exists
        profile_template = TEMPLATES_DIR / "tagesschau_tr" / "correct_transcript.md"
        assert profile_template.exists(), (
            f"Expected {profile_template} to exist"
        )

        # Verify the registry resolves to the profile-namespaced template
        registry = PromptRegistry(db_session)
        resolved = registry.resolve_template_path(
            "correct_transcript.md", profile="tagesschau_tr"
        )
        assert resolved == profile_template

    def test_corrector_fallback_to_base_for_bitcoin(
        self, db_session, settings_with_profiles
    ):
        """Corrector falls back to base prompt for bitcoin_podcast (no profile template)."""
        from btcedu.core.prompt_registry import TEMPLATES_DIR, PromptRegistry

        # bitcoin_podcast has no namespace, so should use base
        registry = PromptRegistry(db_session)
        resolved = registry.resolve_template_path(
            "correct_transcript.md", profile=None
        )
        assert resolved == TEMPLATES_DIR / "correct_transcript.md"
