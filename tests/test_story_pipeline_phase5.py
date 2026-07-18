"""Phase 5 regression tests for story traceability and factual preservation."""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.core.adapter import _adapt_per_story, _adaptation_fidelity_risks
from btcedu.core.segmenter import _normalize_story_inventory
from btcedu.core.translator import (
    _call_story_translation,
    _translation_fidelity_risks,
)
from btcedu.models.story_schema import StoryDocument
from btcedu.models.transcript_schema import (
    CorrectedTranscriptDocument,
    CorrectedTranscriptSegment,
)


def _corrected_document() -> CorrectedTranscriptDocument:
    return CorrectedTranscriptDocument(
        episode_id="ep-story",
        language="de",
        segments=[
            CorrectedTranscriptSegment(
                segment_id="seg-0001",
                start_seconds=10,
                end_seconds=20,
                original_text="Guten Abend.",
                corrected_text="Guten Abend.",
                status="verified",
                severity="none",
            ),
            CorrectedTranscriptSegment(
                segment_id="seg-0002",
                start_seconds=20,
                end_seconds=40,
                original_text="Am 12. Juli beginnt die Fußball-WM.",
                corrected_text="Am 12. Juli beginnt die Fußball-WM.",
                status="uncertain",
                severity="major",
                flags=["unresolved_date"],
                reason="secondary transcript disagrees",
            ),
        ],
        full_text="Guten Abend. Am 12. Juli beginnt die Fußball-WM.",
    )


def _story_document() -> StoryDocument:
    return StoryDocument.model_validate(
        {
            "episode_id": "ep-story",
            "broadcast_date": "2026-07-12",
            "source_attribution": {"source": "tagesschau"},
            "total_stories": 2,
            "total_duration_seconds": 30,
            "stories": [
                {
                    "story_id": "random-intro",
                    "order": 1,
                    "headline_de": "Begrüßung",
                    "category": "meta",
                    "story_type": "intro",
                    "text_de": "ignored",
                    "word_count": 1,
                    "estimated_duration_seconds": 10,
                    "source_segment_ids": ["seg-0001"],
                },
                {
                    "story_id": "random-news",
                    "order": 2,
                    "headline_de": "Fußball-WM",
                    "category": "sport",
                    "story_type": "meldung",
                    "text_de": "ignored",
                    "word_count": 1,
                    "estimated_duration_seconds": 20,
                    "source_segment_ids": ["seg-0002"],
                },
            ],
        }
    )


def test_story_inventory_has_stable_ids_order_and_traceability():
    first = _normalize_story_inventory(_story_document(), _corrected_document())
    second = _normalize_story_inventory(_story_document(), _corrected_document())

    assert [story.story_id for story in first.stories] == ["s01", "s02"]
    assert [story.story_id for story in second.stories] == ["s01", "s02"]
    assert [story.order for story in first.stories] == [1, 2]
    assert first.stories[1].source_segment_ids == ["seg-0002"]
    assert first.stories[1].source_start_seconds == 20
    assert first.stories[1].source_end_seconds == 40
    assert first.stories[1].source_confidence == "medium"
    assert "unresolved_date" in first.stories[1].source_flags
    assert first.stories[0].story_type == "intro"


def test_story_inventory_rejects_missing_news_segment():
    document = _story_document()
    document.stories = document.stories[:1]
    with pytest.raises(ValueError, match="coverage"):
        _normalize_story_inventory(document, _corrected_document())


@pytest.mark.parametrize(
    ("source", "translation", "expected_risk"),
    [
        ("Die Fußball-WM beginnt.", "Futbol Avrupa Şampiyonası başlıyor.", "fussball-wm"),
        ("Das Spiel ist am 12. Juli.", "Maç 13 Temmuz'da.", "numbers"),
        ("Tausend Waffen wurden gefunden.", "Binlerce silah bulundu.", "tausend_as_thousands"),
        ("Titelverteidiger Argentinien gewann.", "Şampiyon Arjantin kazandı.", "titelverteidiger"),
        (
            "Das Tor fiel in der Nachspielzeit.",
            "Gol uzatma devrelerinde geldi.",
            "nachspielzeit_as_extra_time",
        ),
        (
            "In Portugal brach der Satz plötzlich ab.",
            "Portekiz'de birkaç kişi hayatını kaybetti.",
            "invented_casualty",
        ),
    ],
)
def test_translation_regressions_are_rejected(source, translation, expected_risk):
    assert expected_risk in _translation_fidelity_risks(source, translation)


def test_translation_accepts_required_news_terms():
    source = (
        "Titelverteidiger Argentinien spielt am 12. Juli bei der Fußball-WM. "
        "Das Tor fiel in der Nachspielzeit. Tausend Waffen wurden gefunden."
    )
    translation = (
        "Son şampiyon Arjantin, 12 Temmuz'da Futbol Dünya Kupası'nda oynuyor. "
        "Gol hakemin eklediği dakikalarda geldi. Bin silah bulundu."
    )
    assert _translation_fidelity_risks(source, translation) == []


def test_translation_allows_natural_number_reordering():
    source = "Der erste Sieg seit 5 Jahren gelang am 12. Juli mit 2:1."
    translation = "12 Temmuz'da 2:1 skorla, 5 yıl sonra ilk zafer geldi."
    assert _translation_fidelity_risks(source, translation) == []


def test_translation_retries_missing_news_headline():
    story = _normalize_story_inventory(_story_document(), _corrected_document()).stories[1]
    invalid = MagicMock(
        text=json.dumps(
            {
                "story_id": story.story_id,
                "source_segment_ids": story.source_segment_ids,
                "translated_text": "12 Temmuz'da Futbol Dünya Kupası başlıyor.",
            }
        ),
        input_tokens=1,
        output_tokens=1,
        cost_usd=0,
    )
    valid = MagicMock(
        text=json.dumps(
            {
                "story_id": story.story_id,
                "source_segment_ids": story.source_segment_ids,
                "translated_headline": "Futbol Dünya Kupası",
                "translated_text": "12 Temmuz'da Futbol Dünya Kupası başlıyor.",
            }
        ),
        input_tokens=1,
        output_tokens=1,
        cost_usd=0,
    )
    settings = MagicMock()
    with patch("btcedu.core.translator.call_claude", side_effect=[invalid, valid]):
        output, responses = _call_story_translation(
            story,
            "system",
            "user",
            settings,
            None,
        )
    assert output.translated_headline == "Futbol Dünya Kupası"
    assert len(responses) == 2


@pytest.mark.parametrize(
    ("adapted", "expected"),
    [
        ("Başbakan Scholz, 13 Temmuz'da Berlin'e gitti.", "numbers_dates_or_scores"),
        ("Başbakan, 12 Temmuz'da Berlin'e gitti.", "names:Scholz"),
    ],
)
def test_adaptation_rejects_changed_dates_and_names(adapted, expected):
    source = "Başbakan Scholz, 12 Temmuz'da Berlin'e gitti."
    assert expected in _adaptation_fidelity_risks(source, adapted)


def test_anchor_unify_may_remove_reporter_handoff_name():
    source = "Muhabirimiz son gelişmeleri aktardı. Teşekkürler Jens."
    adapted = "Son gelişmeler aktarıldı."
    risks = _adaptation_fidelity_risks(
        source,
        adapted,
        allow_anchor_unify=True,
        removable_names=["Jens"],
    )
    assert risks == []


def test_story_adaptation_preserves_story_id_and_prepares_narration_hash(tmp_path):
    story_doc = StoryDocument.model_validate(
        {
            "episode_id": "ep-story",
            "broadcast_date": "2026-07-12",
            "source_attribution": {"source": "tagesschau"},
            "total_stories": 1,
            "total_duration_seconds": 20,
            "stories": [
                {
                    "story_id": "s01",
                    "order": 1,
                    "headline_de": "Bundestag",
                    "category": "politik",
                    "story_type": "meldung",
                    "text_de": "Der Bundestag beschloss das Gesetz.",
                    "word_count": 5,
                    "estimated_duration_seconds": 20,
                    "source_segment_ids": ["seg-0001"],
                    "text_tr": "Bundestag yasayı kabul etti.",
                }
            ],
        }
    )
    outputs = tmp_path / "outputs" / "ep-story"
    outputs.mkdir(parents=True)
    translated_path = outputs / "stories_translated.json"
    adapted_path = outputs / "stories_adapted.json"
    translated_path.write_text(story_doc.model_dump_json(), encoding="utf-8")

    response = MagicMock()
    response.text = json.dumps(
        {
            "story_id": "s01",
            "adapted_text": "Bundestag (Almanya Federal Meclisi) yasayı kabul etti.",
            "operations_applied": ["institution_explanation"],
        },
        ensure_ascii=False,
    )
    response.input_tokens = 20
    response.output_tokens = 20
    response.cost_usd = 0.001
    settings = MagicMock(
        outputs_dir=str(tmp_path / "outputs"),
        dry_run=False,
    )

    with patch("btcedu.core.adapter.call_claude", return_value=response):
        text, diff, *_ = _adapt_per_story(
            stories_translated_path=translated_path,
            stories_adapted_path=adapted_path,
            episode_id="ep-story",
            system_prompt="system",
            user_template="{{ translation }}\n{{ original_german }}",
            settings=settings,
            mode="conditional",
            allowed_operations=["institution_explanation"],
        )

    adapted = json.loads(adapted_path.read_text(encoding="utf-8"))["stories"][0]
    assert adapted["story_id"] == "s01"
    assert adapted["source_segment_ids"] == ["seg-0001"]
    assert adapted["text_adapted_tr"] in text
    assert (
        adapted["narration_sha256"]
        == hashlib.sha256(adapted["text_adapted_tr"].encode()).hexdigest()
    )
    assert diff["adaptations"][0]["story_id"] == "s01"


def test_legacy_story_document_remains_readable():
    legacy = _story_document().model_dump(mode="json")
    for story in legacy["stories"]:
        for field in (
            "source_segment_ids",
            "source_text",
            "source_start_seconds",
            "source_end_seconds",
            "source_confidence",
            "source_flags",
            "translator_flags",
            "omitted_uncertain_details",
            "glossary_terms_used",
            "text_adapted_tr",
            "adaptation_operations",
            "narration_sha256",
        ):
            story.pop(field, None)
    path = Path("stories.json")
    assert path.name == "stories.json"
    assert StoryDocument.model_validate(legacy).stories[0].source_text == "ignored"
