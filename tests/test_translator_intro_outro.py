"""Tests for intro/outro moderator cleaning in per-story translation."""

import json
from unittest.mock import MagicMock, patch

import pytest

from btcedu.core.translator import _translate_per_story
from btcedu.models.prompt_version import PromptVersion  # noqa: F401


def _make_stories_json(tmp_path, stories=None):
    """Create a stories.json file for testing."""
    if stories is None:
        stories = [
            {
                "story_id": "s01",
                "order": 1,
                "headline_de": "Guten Abend und willkommen zur tagesschau",
                "category": "meta",
                "story_type": "intro",
                "text_de": (
                    "Guten Abend, meine Damen und Herren, ich bin Jens Riewa. "
                    "Heute mit folgenden Themen: Klimagipfel in Berlin, "
                    "Ukraine-Krise und der Bundeshaushalt."
                ),
                "word_count": 25,
                "estimated_duration_seconds": 15,
                "reporter": None,
                "location": None,
                "is_lead_story": False,
                "headline_tr": None,
                "text_tr": None,
            },
            {
                "story_id": "s02",
                "order": 2,
                "headline_de": "Klimagipfel: Neue Beschlüsse",
                "category": "politik",
                "story_type": "bericht",
                "text_de": (
                    "Bundeskanzler Scholz hat beim Klimagipfel in Berlin neue Maßnahmen "
                    "angekündigt. Die Opposition kritisiert die Pläne als unzureichend."
                ),
                "word_count": 20,
                "estimated_duration_seconds": 90,
                "reporter": "Anna Müller",
                "location": "Berlin",
                "is_lead_story": True,
                "headline_tr": None,
                "text_tr": None,
            },
            {
                "story_id": "s03",
                "order": 3,
                "headline_de": "Verabschiedung",
                "category": "meta",
                "story_type": "outro",
                "text_de": (
                    "Das war's von der tagesschau. Ich wünsche Ihnen noch einen "
                    "schönen Abend. Morgen begrüßt Sie dann Susanne Daubner."
                ),
                "word_count": 20,
                "estimated_duration_seconds": 10,
                "reporter": None,
                "location": None,
                "is_lead_story": False,
                "headline_tr": None,
                "text_tr": None,
            },
        ]

    doc = {
        "schema_version": "1.0",
        "episode_id": "ep_test",
        "broadcast_date": "2025-01-15",
        "source_attribution": {
            "source": "tagesschau",
            "broadcaster": "ARD/Das Erste",
            "broadcast_date": "2025-01-15",
            "broadcast_time": "20:00 CET",
            "original_language": "de",
            "original_url": "",
            "attribution_text_tr": "Kaynak: ARD tagesschau",
            "attribution_text_de": "Quelle: ARD tagesschau",
        },
        "total_stories": len(stories),
        "total_duration_seconds": sum(s["estimated_duration_seconds"] for s in stories),
        "stories": stories,
    }

    stories_path = tmp_path / "outputs" / "ep_test" / "stories.json"
    stories_path.parent.mkdir(parents=True, exist_ok=True)
    stories_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    return stories_path


def _mock_claude_response(story_id, headline, text):
    """Create a mock ClaudeResponse."""
    mock = MagicMock()
    mock.text = json.dumps(
        {
            "story_id": story_id,
            "source_segment_ids": [],
            "translated_headline": headline,
            "translated_text": text,
            "translator_flags": [],
            "omitted_uncertain_details": [],
            "glossary_terms_used": [],
        },
        ensure_ascii=False,
    )
    mock.input_tokens = 100
    mock.output_tokens = 50
    mock.cost_usd = 0.001
    return mock


@pytest.fixture
def settings(tmp_path):
    mock_settings = MagicMock()
    mock_settings.outputs_dir = str(tmp_path / "outputs")
    mock_settings.dry_run = False
    return mock_settings


class TestTranslatePerStoryIntroOutro:
    """Test that intro/outro stories get special prompt + regex cleaning."""

    @patch("btcedu.core.translator.call_claude")
    def test_intro_outro_cleaned_when_enabled(self, mock_claude, settings, tmp_path, db_session):
        """Verify moderator names are cleaned from intro/outro stories."""
        stories_path = _make_stories_json(tmp_path)
        translated_path = tmp_path / "transcripts" / "ep_test" / "transcript.tr.txt"
        translated_path.parent.mkdir(parents=True, exist_ok=True)

        # Mock LLM responses — simulate that LLM still leaves some names
        responses = [
            _mock_claude_response(
                "s01",
                "İyi akşamlar, tagesschau'ya hoş geldiniz",
                "İyi akşamlar, ben Jens Riewa. "
                "Gündemdeki konular: Berlin'de iklim zirvesi, Ukrayna krizi ve federal bütçe.",
            ),
            _mock_claude_response(
                "s02",
                "İklim Zirvesi: Yeni Kararlar",
                "Başbakan Scholz, Berlin'deki iklim zirvesinde yeni önlemler açıkladı.",
            ),
            _mock_claude_response(
                "s03",
                "Veda",
                "tagesschau sona erdi. İyi akşamlar. Yarın Susanne Daubner ile görüşmek üzere.",
            ),
        ]
        mock_claude.side_effect = responses

        # Mock intro/outro prompt loading
        with patch(
            "btcedu.core.translator._load_intro_outro_prompt",
            return_value=("Intro/outro prompt", "# Input\n\n{{ transcript }}"),
        ):
            result = _translate_per_story(
                stories_path=stories_path,
                translated_path=translated_path,
                episode_id="ep_test",
                system_prompt="Standard system prompt",
                user_template="# Input\n\n{{ transcript }}",
                settings=settings,
                profile_namespace="tagesschau_tr",
                clean_moderator=True,
                session=db_session,
            )

        translated_text, segments, in_tok, out_tok, cost = result

        # Read the stories_translated.json
        stories_translated = json.loads(
            (stories_path.parent / "stories_translated.json").read_text(encoding="utf-8")
        )

        # Intro story: moderator names should be cleaned
        intro = stories_translated["stories"][0]
        assert "Jens Riewa" not in intro["text_tr"]
        assert "tagesschau" not in intro["headline_tr"]

        # Regular story: should NOT be cleaned (politician names preserved)
        regular = stories_translated["stories"][1]
        assert "Scholz" in regular["text_tr"]

        # Outro story: moderator names and tagesschau should be cleaned
        outro = stories_translated["stories"][2]
        assert "Susanne Daubner" not in outro["text_tr"]
        assert "tagesschau" not in outro["text_tr"]

    @patch("btcedu.core.translator.call_claude")
    def test_no_cleaning_when_disabled(self, mock_claude, settings, tmp_path, db_session):
        """Verify no cleaning happens when clean_moderator is False."""
        stories_path = _make_stories_json(tmp_path)
        translated_path = tmp_path / "transcripts" / "ep_test" / "transcript.tr.txt"
        translated_path.parent.mkdir(parents=True, exist_ok=True)

        def response_for_story(**kwargs):
            story_id = next(
                sid
                for sid in ("s01", "s02", "s03")
                if f'"story_id": "{sid}"' in kwargs["user_message"]
            )
            return _mock_claude_response(
                story_id,
                "tagesschau",
                "Ben Jens Riewa, tagesschau haberlerine hoş geldiniz",
            )

        mock_claude.side_effect = response_for_story

        _translate_per_story(
            stories_path=stories_path,
            translated_path=translated_path,
            episode_id="ep_test",
            system_prompt="System prompt",
            user_template="# Input\n\n{{ transcript }}",
            settings=settings,
            profile_namespace="tagesschau_tr",
            clean_moderator=False,
            session=db_session,
        )

        stories_translated = json.loads(
            (stories_path.parent / "stories_translated.json").read_text(encoding="utf-8")
        )

        # Names should be preserved when cleaning is disabled
        intro = stories_translated["stories"][0]
        assert "Jens Riewa" in intro["text_tr"]

    @patch("btcedu.core.translator.call_claude")
    @patch("btcedu.core.translator._load_intro_outro_prompt")
    def test_intro_uses_different_prompt(
        self, mock_load_prompt, mock_claude, settings, tmp_path, db_session
    ):
        """Verify intro/outro stories use the specialized prompt."""
        stories_path = _make_stories_json(tmp_path)
        translated_path = tmp_path / "transcripts" / "ep_test" / "transcript.tr.txt"
        translated_path.parent.mkdir(parents=True, exist_ok=True)

        mock_load_prompt.return_value = (
            "INTRO_SYSTEM_PROMPT",
            "INTRO_USER: {{ transcript }}",
        )

        def response_for_story(**kwargs):
            story_id = next(
                sid
                for sid in ("s01", "s02", "s03")
                if f'"story_id": "{sid}"' in kwargs["user_message"]
            )
            return _mock_claude_response(story_id, "Başlık", "Günün haberleri")

        mock_claude.side_effect = response_for_story

        _translate_per_story(
            stories_path=stories_path,
            translated_path=translated_path,
            episode_id="ep_test",
            system_prompt="STANDARD_SYSTEM",
            user_template="STANDARD: {{ transcript }}",
            settings=settings,
            profile_namespace="tagesschau_tr",
            clean_moderator=True,
            session=db_session,
        )

        # Check which system_prompt was used for each call
        calls = mock_claude.call_args_list

        assert calls[0].kwargs["system_prompt"] == "INTRO_SYSTEM_PROMPT"
        assert calls[1].kwargs["system_prompt"] == "STANDARD_SYSTEM"
        assert calls[2].kwargs["system_prompt"] == "INTRO_SYSTEM_PROMPT"

    @patch("btcedu.core.translator.call_claude")
    @patch("btcedu.core.translator._load_intro_outro_prompt", return_value=None)
    def test_fallback_to_standard_when_no_intro_prompt(
        self, mock_load_prompt, mock_claude, settings, tmp_path, db_session
    ):
        """When intro/outro prompt doesn't exist, fall back to standard prompt."""
        stories_path = _make_stories_json(tmp_path)
        translated_path = tmp_path / "transcripts" / "ep_test" / "transcript.tr.txt"
        translated_path.parent.mkdir(parents=True, exist_ok=True)

        def response_for_story(**kwargs):
            story_id = next(
                sid
                for sid in ("s01", "s02", "s03")
                if f'"story_id": "{sid}"' in kwargs["user_message"]
            )
            return _mock_claude_response(story_id, "Başlık", "Haberlere hoş geldiniz")

        mock_claude.side_effect = response_for_story

        _translate_per_story(
            stories_path=stories_path,
            translated_path=translated_path,
            episode_id="ep_test",
            system_prompt="STANDARD_SYSTEM",
            user_template="STANDARD: {{ transcript }}",
            settings=settings,
            profile_namespace="tagesschau_tr",
            clean_moderator=True,
            session=db_session,
        )

        # All calls should use standard prompt since intro prompt was not found
        for call in mock_claude.call_args_list:
            assert call.kwargs["system_prompt"] == "STANDARD_SYSTEM"

    @patch("btcedu.core.translator.call_claude")
    def test_followup_program_preview_is_removed_deterministically(
        self, mock_claude, settings, tmp_path, db_session
    ):
        stories = [
            {
                "story_id": "s01",
                "order": 1,
                "headline_de": "Wetter",
                "category": "wetter",
                "story_type": "wetter",
                "text_de": "Morgen ist es im Norden regnerisch.",
                "word_count": 7,
                "estimated_duration_seconds": 10,
                "reporter": None,
                "location": None,
                "is_lead_story": False,
                "headline_tr": None,
                "text_tr": None,
            },
            {
                "story_id": "s02",
                "order": 2,
                "headline_de": "Vorschau auf die Tagesthemen",
                "category": "meta",
                "story_type": "outro",
                "text_de": (
                    "Die Tagesthemen melden sich gegen 21.35 Uhr in der Halbzeitpause "
                    "des DFB-Pokalspiels Halle gegen Schalke. Darin Neustart nach der "
                    "Sommerpause, wie der Kanzler die Reformpakete umsetzen will. "
                    "Außerdem Ungewissheit und Wut bei VW. Einen angenehmen Abend."
                ),
                "word_count": 38,
                "estimated_duration_seconds": 20,
                "reporter": None,
                "location": None,
                "is_lead_story": False,
                "headline_tr": None,
                "text_tr": None,
            },
        ]
        stories_path = _make_stories_json(tmp_path, stories)
        translated_path = tmp_path / "transcripts" / "ep_test" / "transcript.tr.txt"
        translated_path.parent.mkdir(parents=True, exist_ok=True)
        mock_claude.side_effect = [
            _mock_claude_response("s01", "Hava Durumu", "Yarın kuzeyde yağmur bekleniyor."),
            _mock_claude_response(
                "s02",
                "Gündem",
                "Başbakanın reform paketleri ve VW'deki belirsizlik ele alınacak.",
            ),
        ]

        with patch(
            "btcedu.core.translator._load_intro_outro_prompt",
            return_value=("Intro/outro prompt", "# Input\n\n{{ transcript }}"),
        ):
            _translate_per_story(
                stories_path=stories_path,
                translated_path=translated_path,
                episode_id="ep_test",
                system_prompt="Standard system prompt",
                user_template="# Input\n\n{{ transcript }}",
                settings=settings,
                profile_namespace="tagesschau_tr",
                clean_moderator=True,
                session=db_session,
            )

        translated = json.loads(
            (stories_path.parent / "stories_translated.json").read_text(encoding="utf-8")
        )
        preview = translated["stories"][1]
        assert preview["headline_tr"] == ""
        assert preview["text_tr"] == ""
        assert "broadcast_frame_removed" in preview["translator_flags"]
        assert translated_path.read_text(encoding="utf-8") == "Yarın kuzeyde yağmur bekleniyor."
