"""Phase 5 regression tests for story traceability and factual preservation."""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from btcedu.core.adapter import (
    _adapt_per_story,
    _adaptation_fidelity_risks,
    _needed_adaptation_operations,
)
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


def test_month_term_ignores_person_names_containing_juli():
    # "Julian"/"Julia" contain the substring "juli" but are names, not the month.
    # A faithful translation keeps the name and must NOT be forced to contain
    # "temmuz" — see tagesschau episode G7vylxQQv2I (Julian Nagelsmann / Julia).
    source = "Der Nachfolger von Julian Nagelsmann. Julia Nihari-Kassen fragt warum."
    translation = "Julian Nagelsmann'ın halefi. Julia Nihari-Kassen nedenini soruyor."
    assert "juli" not in _translation_fidelity_risks(source, translation)


def test_faithful_kill_verb_is_not_invented_casualty():
    # Threats/killing in the source ("umbringen", "vernichten", "Mordanschlag")
    # faithfully rendered with the Turkish active kill verb "öldürmek" must NOT
    # be flagged: "öldü" (died) must not match inside "öldür-" (to kill).
    source = (
        "Iran würde im Falle eines Mordanschlags komplett vernichtet werden. "
        "Wenn die wirklich vorhaben, mich umzubringen, werden wir Iran zerstören."
    )
    translation = (
        "Bir suikast girişimi olursa İran tamamen yok edilecek. "
        "Beni gerçekten öldürmek istiyorlarsa İran'ı yok edeceğiz."
    )
    assert "invented_casualty" not in _translation_fidelity_risks(source, translation)


def test_faithful_death_when_source_mentions_killing_is_not_invented():
    # Source reports killings via "getötet"; a Turkish death rendering is faithful.
    source = "Bei dem Angriff wurden Menschen getötet."
    translation = "Saldırıda insanlar hayatını kaybetti."
    assert "invented_casualty" not in _translation_fidelity_risks(source, translation)


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


def test_translation_allows_turkish_thousands_separator():
    source = "Die Zahl stieg auf 5000, fast 350 Mio. US-Dollar wurden freigegeben."
    translation = "Sayı 5.000'e çıktı, yaklaşık 350 milyon ABD doları serbest bırakıldı."

    assert _translation_fidelity_risks(source, translation) == []


def test_translation_allows_turkish_compound_thousands_and_avro():
    source = (
        "Die Digitalsparte beschäftigt 5.500 Mitarbeiter. "
        "Der Umsatz stieg von 11 Milliarden Euro auf 14 Milliarden Euro."
    )
    translation = (
        "Dijital bölümü 5 bin 500 çalışanı istihdam ediyor. "
        "Ciro 11 milyar avrodan 14 milyar avroya yükseldi."
    )

    assert _translation_fidelity_risks(source, translation) == []


def test_translation_allows_turkish_compound_thousands_money():
    source = "Das Projekt kostet 5.500 Euro."
    translation = "Proje 5 bin 500 avroya mal oluyor."

    assert _translation_fidelity_risks(source, translation) == []


def test_translation_does_not_find_death_inside_bolumu():
    source = "Die Digitalsparte beschäftigt neue Mitarbeiter."
    translation = "Şirketin dijital bölümü yeni çalışanları istihdam ediyor."

    assert "invented_casualty" not in _translation_fidelity_risks(source, translation)


def test_translation_allows_written_turkish_list_numbers():
    source = "2. Punkt ist der Wahlkampf. 3. Der Kanzler reagierte. Ich nenne 2 Beispiele."
    translation = "İkincisi seçim kampanyası. Üçüncüsü şansölyenin tepkisi. İki örnek vereyim."

    assert _translation_fidelity_risks(source, translation) == []


def test_translation_precheck_defers_missing_written_number_to_full_qa():
    source = "5 Jahre später wurde er mit 22 Jahren gewählt."
    translation = "Beş yıl sonra yirmi iki yaşında seçildi."

    assert _translation_fidelity_risks(source, translation) == []


def test_translation_precheck_defers_ambiguous_written_cardinals_to_full_qa():
    source = "Zwei Männer stehen neben einem Kinderwagen."
    translation = "İki erkek bir bebek arabasının yanında duruyor."

    assert _translation_fidelity_risks(source, translation) == []


def test_translation_precheck_accepts_source_word_as_target_digit():
    assert (
        _translation_fidelity_risks(
            "Es gab zwölf Einsätze.",
            "12 görev yapıldı.",
        )
        == []
    )


def test_translation_matches_german_and_turkish_ordinals():
    source = "Seit 2025 war er der zweitwichtigste Mann."
    translation = "2025'ten beri en önemli ikinci isimdi."

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


def test_translation_uses_independent_adjudicator_replacement(tmp_path):
    story = _normalize_story_inventory(_story_document(), _corrected_document()).stories[1]
    story.source_text = "Bei dem Unfall wurden 5 Menschen verletzt."
    invalid_payload = {
        "story_id": story.story_id,
        "source_segment_ids": story.source_segment_ids,
        "translated_headline": "Kaza",
        "translated_text": "Kazada 6 kişi yaralandı.",
    }
    corrected_payload = {
        **invalid_payload,
        "translated_text": "Kazada 5 kişi yaralandı.",
    }
    producer = MagicMock(
        text=json.dumps(invalid_payload),
        input_tokens=10,
        output_tokens=10,
        cost_usd=0,
        model="producer",
    )
    adjudicator = MagicMock(
        text=json.dumps(
            {
                "decision": "use_replacement",
                "reason": "The casualty value must remain five.",
                "replacement": corrected_payload,
            }
        ),
        input_tokens=10,
        output_tokens=10,
        cost_usd=0,
        model="copilot/gpt-5.6-sol",
    )
    audit_path = tmp_path / "translate_adjudication.json"

    with (
        patch(
            "btcedu.core.translator.call_claude",
            side_effect=[producer, producer, adjudicator],
        ) as mock_call,
        patch(
            "btcedu.core.translator._translation_fidelity_risks",
            return_value=["numbers"],
        ),
    ):
        output, responses = _call_story_translation(
            story,
            "system",
            "user",
            MagicMock(),
            None,
            adjudication_route={"provider": "copilot_cli", "model": "gpt-5.6-sol"},
            adjudication_path=audit_path,
        )

    assert output.translated_text == "Kazada 5 kişi yaralandı."
    assert len(responses) == 3
    assert mock_call.call_args.kwargs["provider_override"] == "copilot_cli"
    assert mock_call.call_args.kwargs["model_override"] == "gpt-5.6-sol"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["decision"] == "use_replacement"
    assert audit["selected"]["translated_text"] == "Kazada 5 kişi yaralandı."
    assert audit["residual_deterministic_risks"] == ["numbers"]


def test_translation_adjudicator_can_accept_false_positive(tmp_path):
    story = _normalize_story_inventory(_story_document(), _corrected_document()).stories[1]
    payload = {
        "story_id": story.story_id,
        "source_segment_ids": story.source_segment_ids,
        "translated_headline": "Dijital bölüm",
        "translated_text": "Şirketin dijital bölümü büyüyor.",
    }
    producer = MagicMock(
        text=json.dumps(payload),
        input_tokens=10,
        output_tokens=10,
        cost_usd=0,
        model="producer",
    )
    adjudicator = MagicMock(
        text=json.dumps(
            {
                "decision": "accept_candidate",
                "reason": "The flagged substring is part of bölüm and does not denote death.",
            }
        ),
        input_tokens=10,
        output_tokens=10,
        cost_usd=0,
        model="copilot/gpt-5.6-sol",
    )

    with (
        patch(
            "btcedu.core.translator.call_claude",
            side_effect=[producer, producer, adjudicator],
        ),
        patch(
            "btcedu.core.translator._translation_fidelity_risks",
            return_value=["invented_casualty"],
        ),
    ):
        output, responses = _call_story_translation(
            story,
            "system",
            "user",
            MagicMock(),
            None,
            adjudication_route={"provider": "copilot_cli", "model": "gpt-5.6-sol"},
            adjudication_path=tmp_path / "accept.json",
        )

    assert output.translated_text == "Şirketin dijital bölümü büyüyor."
    assert len(responses) == 3


def test_intro_outro_may_remove_program_time_during_cleaning():
    story = _normalize_story_inventory(_story_document(), _corrected_document()).stories[0]
    story.text_de = "Die Tagesthemen beginnen um 22 Uhr."
    story.source_text = story.text_de
    response = MagicMock(
        text=json.dumps(
            {
                "story_id": story.story_id,
                "source_segment_ids": story.source_segment_ids,
                "translated_headline": "",
                "translated_text": "",
            }
        ),
        input_tokens=1,
        output_tokens=1,
        cost_usd=0,
    )

    with patch("btcedu.core.translator.call_claude", return_value=response):
        output, responses = _call_story_translation(
            story,
            "system",
            "user",
            MagicMock(),
            None,
        )

    assert output.translated_text == ""
    assert len(responses) == 1


def test_needed_adaptation_detects_german_first_person_with_implicit_turkish_person():
    story = SimpleNamespace(
        story_type="bericht",
        reporter=None,
        source_text=(
            "Dem Ergebnis der laufenden Abstimmung möchte ich nicht vorgreifen. "
            "Unser Ziel ist es, dass der Bundestag den Gesetzentwurf berät."
        ),
        text_de="",
    )
    text_tr = (
        "Devam eden koordinasyonun sonucunu şimdiden öngörmek istemiyorum. "
        "Hedefimiz, Bundestag'ın yasa tasarısını görüşmesidir."
    )

    assert _needed_adaptation_operations(
        story,
        text_tr,
        ["anchor_unify", "institution_explanation", "local_relevance", "register_polish"],
    ) == ["anchor_unify", "institution_explanation"]


def test_conditional_story_adaptation_allows_detected_german_first_person(tmp_path):
    source_de = (
        "Dem Ergebnis der laufenden Abstimmung möchte ich nicht vorgreifen. "
        "Unser Ziel ist es, dass der Bundestag den Gesetzentwurf berät."
    )
    source_tr = (
        "Devam eden koordinasyonun sonucunu şimdiden öngörmek istemiyorum. "
        "Hedefimiz, Bundestag'ın yasa tasarısını görüşmesidir."
    )
    adapted_tr = (
        "Adalet Bakanlığı devam eden koordinasyonun sonucuna ilişkin açıklama yapmadı. "
        "Bakanlığın hedefi, Bundestag (Almanya Federal Meclisi)'ın yasa tasarısını "
        "görüşmesidir."
    )
    story_doc = StoryDocument.model_validate(
        {
            "episode_id": "ep-german-first-person",
            "broadcast_date": "2026-08-23",
            "source_attribution": {"source": "tagesschau"},
            "total_stories": 1,
            "total_duration_seconds": 20,
            "stories": [
                {
                    "story_id": "s03",
                    "order": 1,
                    "headline_de": "Gesetzentwurf",
                    "category": "politik",
                    "story_type": "bericht",
                    "text_de": source_de,
                    "word_count": 18,
                    "estimated_duration_seconds": 20,
                    "source_segment_ids": ["seg-0100"],
                    "text_tr": source_tr,
                }
            ],
        }
    )
    outputs = tmp_path / "outputs" / "ep-german-first-person"
    outputs.mkdir(parents=True)
    translated_path = outputs / "stories_translated.json"
    adapted_path = outputs / "stories_adapted.json"
    translated_path.write_text(story_doc.model_dump_json(), encoding="utf-8")
    response = MagicMock(
        text=json.dumps(
            {
                "story_id": "s03",
                "adapted_text": adapted_tr,
                "operations_applied": ["anchor_unify", "institution_explanation"],
            },
            ensure_ascii=False,
        ),
        input_tokens=20,
        output_tokens=20,
        cost_usd=0.001,
    )
    settings = MagicMock(outputs_dir=str(tmp_path / "outputs"), dry_run=False)

    with patch("btcedu.core.adapter.call_claude", return_value=response):
        _, diff, *_ = _adapt_per_story(
            stories_translated_path=translated_path,
            stories_adapted_path=adapted_path,
            episode_id="ep-german-first-person",
            system_prompt="system",
            user_template="{{ translation }}\n{{ original_german }}",
            settings=settings,
            mode="conditional",
            allowed_operations=[
                "anchor_unify",
                "institution_explanation",
                "local_relevance",
                "register_polish",
            ],
        )

    adapted = json.loads(adapted_path.read_text(encoding="utf-8"))["stories"][0]
    assert adapted["text_adapted_tr"] == adapted_tr
    assert adapted["adaptation_operations"] == ["anchor_unify", "institution_explanation"]
    assert [item["category"] for item in diff["adaptations"]] == [
        "anchor_unify",
        "institution_explanation",
    ]


def test_needed_adaptation_does_not_flag_plain_german_narration():
    story = SimpleNamespace(
        story_type="bericht",
        reporter=None,
        source_text="Die Bundesregierung kündigte einen Gesetzentwurf an.",
        text_de="",
    )

    assert (
        _needed_adaptation_operations(
            story, "Federal Hükümet bir tasarı duyurdu.", ["anchor_unify"]
        )
        == []
    )


def test_adaptation_rejects_changed_dates():
    source = "Başbakan Scholz, 12 Temmuz'da Berlin'e gitti."
    adapted = "Başbakan Scholz, 13 Temmuz'da Berlin'e gitti."

    assert "numbers_dates_or_scores" in _adaptation_fidelity_risks(source, adapted)


def test_adaptation_defers_name_semantics_to_translation_qa():
    source = "AfD açıklama yaptı."
    adapted = "Almanya için Alternatif açıklama yaptı."

    assert _adaptation_fidelity_risks(source, adapted) == []


def test_adaptation_accepts_turkish_apostrophe_suffix_variants():
    source = "Açıklama Berlin'den geldi."
    adapted = "Açıklama Berlin’den geldi."

    assert _adaptation_fidelity_risks(source, adapted) == []


def test_adaptation_ignores_ambiguous_sentence_initial_capitalization():
    source = "Şimdi ürünlerin fiyatları düşebilir. Nihatschabu bunu bekliyor."
    adapted = "Ürünlerin fiyatları şimdi düşebilir. Nihatschabu bunu bekliyor."

    assert _adaptation_fidelity_risks(source, adapted) == []


def test_adaptation_ignores_sentence_initial_word_after_quote():
    source = 'Bu karlı olabilir." Şimdi üreticilerden talepleri bekliyor.'
    adapted = "Bu karlı olabilir. Üreticilerden talepleri şimdi bekliyor."

    assert _adaptation_fidelity_risks(source, adapted) == []


def test_adaptation_may_remove_parenthetical_office_translation():
    source = "Federal Şansölye (Bundeskanzler) açıklama yaptı."
    adapted = "Federal Şansölye açıklama yaptı."

    assert _adaptation_fidelity_risks(source, adapted) == []


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


def test_explicit_reporter_handoff_name_is_inherently_removable():
    source = "ABD'de muhabirimiz Gudrun Engel var. Teşekkürler Gudrun Engel."
    adapted = "ABD'deki gelişmeler aktarıldı."

    assert _adaptation_fidelity_risks(source, adapted) == []


def test_reporter_name_remains_removable_when_addressed_later():
    source = (
        "ABD'de muhabirimiz Gudrun Engel var. Final nasıl olacak Gudrun? Teşekkürler Gudrun Engel."
    )
    adapted = "ABD'deki final öncesi atmosfer aktarıldı."

    assert _adaptation_fidelity_risks(source, adapted) == []


def test_adaptation_allows_omitting_name_during_compression():
    source = "Başbakan Scholz, 12 Temmuz'da Berlin'e gitti."
    adapted = "Başbakan 12 Temmuz'da Berlin'e gitti."

    assert _adaptation_fidelity_risks(source, adapted) == []


def test_adaptation_accepts_equivalent_score_punctuation():
    source = "İngiltere maçı 6-4 kazandı."
    adapted = "İngiltere karşılaşmayı 6:4 kazandı."

    assert _adaptation_fidelity_risks(source, adapted) == []


def test_adaptation_allows_consolidating_repeated_plain_number():
    source = "Thomas üçüncülüğü aldı. Podyumda üçüncülüğü garantiledi."
    adapted = "Thomas üçüncülüğü garantiledi."

    assert _adaptation_fidelity_risks(source, adapted) == []


def test_reporter_name_after_comma_in_thank_you_is_removable():
    source = "ABD'de Gudrun Engel bekliyor. Teşekkürler, Gudrun Engel."
    adapted = "ABD'deki gelişmeler aktarıldı."

    assert _adaptation_fidelity_risks(source, adapted) == []


def test_reporter_name_after_location_in_thank_you_is_removable():
    source = (
        "East Rutherford stadyumu önünde Gudrun Engel bekliyor. "
        "Teşekkürler, East Rutherford'dan Gudrun Engel."
    )
    adapted = "East Rutherford stadyumu önündeki atmosfer aktarıldı."

    assert _adaptation_fidelity_risks(source, adapted) == []


def test_adaptation_does_not_treat_turkish_demonym_plural_as_name():
    source = "Sayıca Arjantinliler artık üstün durumda."
    adapted = "Arjantin taraftarları artık sayıca üstün."

    assert _adaptation_fidelity_risks(source, adapted) == []


def test_adaptation_does_not_treat_inflected_turkish_demonym_plural_as_name():
    source = "Amerikalıların bu gösteriye ilgisi yüksek."
    adapted = "ABD halkı bu gösteriye büyük ilgi duyuyor."

    assert _adaptation_fidelity_risks(source, adapted) == []


def test_adaptation_does_not_treat_inflected_turkish_common_noun_as_name():
    source = "Bu görüntü hafızalarda kalıyor ve Birliğin güvenilirliğine zarar veriyor."
    adapted = "Bu görüntü kalıcı oluyor ve siyasi ittifakın güvenilirliğini zedeliyor."

    assert _adaptation_fidelity_risks(source, adapted) == []


def test_adaptation_allows_omitting_given_name_when_surname_is_preserved():
    source = "Biliyoruz ki Friedrich Merz'in pek çok güvendiği kişisi yok."
    adapted = "Merz'in güvendiği kişi sayısının az olduğu biliniyor."

    assert _adaptation_fidelity_risks(source, adapted) == []


def test_adaptation_ignores_capitalized_clause_after_colon():
    source = "Benim için önemli olan şu: Politikaya güven yeniden kazandırılmalı."
    adapted = "Siyasete duyulan güven yeniden sağlanmalı."

    assert _adaptation_fidelity_risks(source, adapted) == []


def test_adaptation_does_not_protect_acronyms_or_inflected_titles_as_people():
    source = "ARD röportajında Parti Başkanının açıklaması yayınlandı."
    adapted = "Röportajda parti liderinin açıklaması aktarıldı."

    assert _adaptation_fidelity_risks(source, adapted) == []


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


def test_story_adaptation_falls_back_when_protected_facts_change(tmp_path):
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
                    "text_de": "Der Bundestag beschloss 5 Maßnahmen.",
                    "word_count": 5,
                    "estimated_duration_seconds": 20,
                    "source_segment_ids": ["seg-0001"],
                    "text_tr": "Bundestag 5 önlemi kabul etti.",
                }
            ],
        }
    )
    outputs = tmp_path / "outputs" / "ep-story"
    outputs.mkdir(parents=True)
    translated_path = outputs / "stories_translated.json"
    adapted_path = outputs / "stories_adapted.json"
    translated_path.write_text(story_doc.model_dump_json(), encoding="utf-8")
    response = MagicMock(
        text=json.dumps(
            {
                "story_id": "s01",
                "adapted_text": "Bundestag (Almanya Federal Meclisi) 6 önlemi kabul etti.",
                "operations_applied": ["institution_explanation"],
            },
            ensure_ascii=False,
        ),
        input_tokens=20,
        output_tokens=20,
        cost_usd=0.001,
    )
    settings = MagicMock(outputs_dir=str(tmp_path / "outputs"), dry_run=False)

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
    assert adapted["text_adapted_tr"] == "Bundestag 5 önlemi kabul etti."
    assert adapted["adaptation_operations"] == []
    assert "Bundestag 5 önlemi kabul etti." in text
    assert diff["adaptations"] == []
    audit = json.loads(
        (outputs / "provenance" / "adapt_validation_s01.json").read_text(encoding="utf-8")
    )
    assert audit["decision"] == "fallback_to_verified_translation"
    assert audit["risks"] == ["numbers_dates_or_scores"]


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
