"""Tests for deterministic story-level translation quality checks."""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from btcedu.core.translation_qa import (
    evaluate_translation_documents,
    extract_numeric_facts,
    load_translation_qa,
    run_translation_qa,
)
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus
from btcedu.prompts.glossary_loader import load_glossary


def _story(
    story_id: str,
    source: str,
    target: str,
    *,
    order: int = 1,
    lead: bool = False,
    segment_ids: list[str] | None = None,
) -> tuple[dict, dict]:
    source_story = {
        "story_id": story_id,
        "order": order,
        "headline_de": story_id,
        "category": "politik",
        "story_type": "meldung",
        "text_de": source,
        "source_text": source,
        "source_segment_ids": segment_ids or [f"seg-{order:04d}"],
        "word_count": len(source.split()),
        "estimated_duration_seconds": 10,
        "is_lead_story": lead,
    }
    target_story = {
        **source_story,
        "text_tr": target,
        "headline_tr": story_id,
    }
    return source_story, target_story


def _documents(stories: list[tuple[dict, dict]]) -> tuple[dict, dict]:
    source_stories = [source for source, _ in stories]
    target_stories = [target for _, target in stories]
    source = {
        "episode_id": "ep-qa",
        "broadcast_date": "2026-07-18",
        "source_attribution": {"source": "test"},
        "total_stories": len(source_stories),
        "total_duration_seconds": 10 * len(source_stories),
        "stories": source_stories,
    }
    target = {**source, "stories": target_stories}
    return source, target


def _evaluate(
    source_text: str,
    target_text: str,
    *,
    glossary: dict | None = None,
):
    source, target = _documents([_story("s01", source_text, target_text, lead=True)])
    return evaluate_translation_documents("ep-qa", source, target, glossary)


def _categories(document) -> list[str]:
    return [finding.category for finding in document.findings]


def test_missing_lead_story_is_critical():
    source, target = _documents(
        [
            _story("s01", "Hauptmeldung.", "Ana haber.", order=1, lead=True),
            _story("s02", "Zweite Meldung.", "İkinci haber.", order=2),
        ]
    )
    target["stories"] = [target["stories"][1]]
    document = evaluate_translation_documents("ep-qa", source, target)
    finding = next(f for f in document.findings if f.category == "missing_story_ids")
    assert finding.severity == "critical"
    assert document.story_coverage["missing_story_ids"] == ["s01"]


def test_duplicate_story_is_reported():
    source, target = _documents([_story("s01", "Meldung.", "Haber.")])
    target["stories"].append(dict(target["stories"][0]))
    document = evaluate_translation_documents("ep-qa", source, target)
    assert "duplicate_story_ids" in _categories(document)


def test_unknown_story_is_reported():
    source, target = _documents([_story("s01", "Meldung.", "Haber.")])
    unknown = dict(target["stories"][0])
    unknown["story_id"] = "s99"
    target["stories"].append(unknown)
    document = evaluate_translation_documents("ep-qa", source, target)
    assert "unknown_story_ids" in _categories(document)


def test_wrong_story_order_is_reported():
    source, target = _documents(
        [
            _story("s01", "Erste Meldung.", "Birinci haber.", order=1),
            _story("s02", "Zweite Meldung.", "İkinci haber.", order=2),
        ]
    )
    target["stories"].reverse()
    document = evaluate_translation_documents("ep-qa", source, target)
    assert "order_mismatch" in _categories(document)


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("Das Treffen ist am 12. Juli.", "Görüşme 12 Temmuz'da."),
        ("Die Sendung beginnt um 20 Uhr.", "Yayın saat 20.00'de başlıyor."),
        ("Das Spiel endete 2 zu 1.", "Maç 2-1 sona erdi."),
        ("Der Anteil beträgt 5 Prozent.", "Oran yüzde 5."),
        ("Die Hilfe beträgt 1,5 Millionen Euro.", "Yardım 1,5 milyon Euro."),
        ("Es werden 20 Grad erwartet.", "20 derece bekleniyor."),
        ("Bei dem Angriff gab es 12 Tote.", "Saldırıda 12 kişi hayatını kaybetti."),
    ],
)
def test_equivalent_numeric_formats_are_accepted(source, target):
    document = _evaluate(source, target)
    assert document.findings == []
    assert document.number_coverage["coverage"] == 1.0


@pytest.mark.parametrize(
    ("source", "target"),
    [
        ("Die Hilfe beträgt 1.000 Euro.", "Yardım 1000 Euro."),
        ("Die Hilfe beträgt 1.000,50 Euro.", "Yardım 1000,50 Euro."),
        ("1 Million Menschen waren betroffen.", "1 milyon insan etkilendi."),
        ("Eine Million Menschen waren betroffen: 1.000.000.", "1 milyon insan etkilendi."),
    ],
)
def test_localized_thousands_and_decimals_are_accepted(source, target):
    document = _evaluate(source, target)
    assert not {
        "number_mismatch",
        "money_mismatch",
        "unexpected_number",
    }.intersection(_categories(document))


def test_localized_thousands_difference_is_reported():
    document = _evaluate(
        "Die Hilfe beträgt 1.000 Euro.",
        "Yardım 100 Euro.",
    )
    assert "money_mismatch" in _categories(document)


@pytest.mark.parametrize(
    ("source", "target", "category"),
    [
        ("Das Treffen ist am 12. Juli.", "Görüşme 13 Temmuz'da.", "date_mismatch"),
        ("Die Sendung beginnt um 20 Uhr.", "Yayın saat 21.00'de başlıyor.", "time_mismatch"),
        ("Das Spiel endete 2 zu 1.", "Maç 3-1 sona erdi.", "score_mismatch"),
        (
            "Bei dem Angriff gab es 12 Tote.",
            "Saldırıda 13 kişi hayatını kaybetti.",
            "casualty_mismatch",
        ),
    ],
)
def test_changed_numeric_facts_are_reported(source, target, category):
    document = _evaluate(source, target)
    assert category in _categories(document)


def test_repeated_plain_number_may_be_consolidated_during_adaptation():
    document = _evaluate(
        "Thomas holt Platz 3. Der Podiumsplatz 3 ist gesichert.",
        "Thomas üçüncülüğü garantiledi.",
    )

    assert "number_mismatch" not in _categories(document)


def test_quantity_word_precision_is_preserved():
    correct = _evaluate("Tausend Waffen wurden gefunden.", "Bin silah bulundu.")
    wrong = _evaluate("Tausend Waffen wurden gefunden.", "Binlerce silah bulundu.")
    assert "number_mismatch" not in _categories(correct)
    assert "number_mismatch" in _categories(wrong)


def test_protected_term_accepts_variant_with_turkish_suffix():
    glossary = {"protected_terms": {"Fußball-WM": {"allowed_targets": ["Futbol Dünya Kupası"]}}}
    document = _evaluate(
        "Die Fußball-WM beginnt heute.",
        "Futbol Dünya Kupası'nda bugün ilk maç oynanıyor.",
        glossary=glossary,
    )
    assert "protected_term_violation" not in _categories(document)


def test_protected_term_rejects_wm_as_em():
    glossary = {"protected_terms": {"Fußball-WM": {"allowed_targets": ["Futbol Dünya Kupası"]}}}
    document = _evaluate(
        "Die Fußball-WM beginnt heute.",
        "Avrupa Şampiyonası bugün başlıyor.",
        glossary=glossary,
    )
    assert "protected_term_violation" in _categories(document)


def test_nachspielzeit_is_not_verlaengerung():
    glossary = {"protected_terms": {"Nachspielzeit": {"allowed_targets": ["duraklama dakikaları"]}}}
    document = _evaluate(
        "Das Tor fiel in der Nachspielzeit.",
        "Gol uzatma devrelerinde geldi.",
        glossary=glossary,
    )
    assert "protected_term_violation" in _categories(document)


def test_news_glossary_covers_fraktion_bewaehrung_and_title_defense():
    glossary = load_glossary("tagesschau_tr")

    assert glossary is not None
    assert glossary["terms"]["Fraktion"] == "meclis grubu"
    assert "denetimli serbestlik" in glossary["protected_terms"]["Bewährung"]["allowed_targets"]
    assert (
        "şampiyonluğu savunma"
        in glossary["protected_terms"]["Titelverteidigung"]["allowed_targets"]
    )
    assert (
        "şampiyonluk savunması"
        in glossary["protected_terms"]["Titelverteidigung"]["allowed_targets"]
    )
    assert (
        "kuvvetli suç şüphesi"
        in glossary["protected_terms"]["dringender Tatverdacht"]["allowed_targets"]
    )
    assert glossary["phrases"]["Das war es für [Person/Rolle]"].endswith(
        "süreç veya turnuva sona erdi"
    )


def test_entity_with_turkish_suffix_is_accepted():
    document = _evaluate(
        "Bundeskanzlerin Angela Merkel besuchte Berlin.",
        "Angela Merkel'in Berlin ziyareti gerçekleşti.",
    )
    assert "entity_suspicion" not in _categories(document)


def test_negation_difference_creates_suspicion():
    document = _evaluate(
        "Die Regierung bestätigte den Bericht nicht.",
        "Hükümet haberi doğruladı.",
    )
    assert "negation_suspicion" in _categories(document)


@pytest.mark.parametrize(
    "target",
    [
        "Hükümet haberi doğrulamadı.",
        "Hükümet haberi teyit etmedi.",
        "Haber doğru değildir.",
    ],
)
def test_correct_turkish_negation_does_not_create_finding(target):
    document = _evaluate(
        "Die Regierung bestätigte den Bericht nicht.",
        target,
    )
    assert "negation_suspicion" not in _categories(document)


def test_question_word_ne_does_not_hide_missing_negation():
    document = _evaluate(
        "Die Regierung bestätigte den Bericht nicht.",
        "Hükümet raporu doğruladı ve ne zaman yayımlanacağını açıkladı.",
    )
    assert "negation_suspicion" in _categories(document)


def test_turkish_neither_pair_preserves_negation():
    document = _evaluate(
        "Weder die Regierung noch der Minister bestätigten den Bericht.",
        "Ne hükümet ne de bakan raporu doğruladı.",
    )
    assert "negation_suspicion" not in _categories(document)


def test_two_question_words_do_not_count_as_neither_pair():
    document = _evaluate(
        "Die Regierung bestätigte den Bericht nicht.",
        "Hükümet raporu doğruladı; ne zaman yayımlanacağını ve ne yapacağını açıkladı.",
    )
    assert "negation_suspicion" in _categories(document)


def test_non_sport_ratio_is_not_treated_as_score():
    document = _evaluate(
        "Die Partei gewann die Abstimmung mit 2 zu 1 Stimmen.",
        "Parti oylamayı 2'ye karşı 1 oyla kazandı.",
    )
    assert "score_mismatch" not in _categories(document)


def test_turkish_progressive_verbal_negation_is_accepted():
    document = _evaluate(
        "Seine Zukunft soll nicht auf dem Fußballplatz liegen.",
        "Geleceğinin futbol sahasında olması düşünülmüyor.",
    )

    assert "negation_suspicion" not in _categories(document)


def test_chronology_difference_creates_suspicion():
    document = _evaluate(
        "Zuvor traf der Minister die Länder, danach sprach er im Bundestag.",
        "Bakan eyaletlerle görüştü.",
    )
    assert "chronology_suspicion" in _categories(document)


def test_weather_regions_keep_their_conditions():
    source = "Zwischen Ostsee und Erzgebirge gibt es Schauer. Im Südwesten gibt es Sonnenschein."
    correct = _evaluate(
        source,
        "Baltık Denizi ile Erzgebirge arasında sağanaklar var. Güneybatıda güneş görülüyor.",
    )
    wrong = _evaluate(
        source,
        "Baltık Denizi ile Erzgebirge arasında güneş görülüyor. Güneybatıda sağanaklar var.",
    )
    assert "weather_region_mismatch" not in _categories(correct)
    assert "weather_region_mismatch" in _categories(wrong)


def test_identical_neutral_content_has_no_findings():
    document = _evaluate("Bitcoin ist ein digitales Gut.", "Bitcoin ist ein digitales Gut.")
    assert document.findings == []
    assert document.status == "green"


def test_numeric_extractor_classifies_all_required_types():
    facts = extract_numeric_facts(
        "Am 12. Juli um 20 Uhr waren es 5 Prozent, 1,5 Millionen Euro, "
        "20 Grad und das Spiel endete 2 zu 1."
    )
    kinds = {fact.kind for fact in facts}
    assert {"date", "time", "percent", "money", "temperature", "score"} <= kinds


def test_numeric_extractor_normalizes_turkish_scaled_thousands():
    facts = extract_numeric_facts("Resmi can kaybı 5 bine yükseldi.")

    assert [(fact.kind, fact.value) for fact in facts] == [("casualty", "5000")]


def test_numeric_extractor_does_not_split_turkish_compound_number():
    facts = extract_numeric_facts("Yirmi iki yaşında seçildi.")

    assert [(fact.kind, fact.value) for fact in facts] == [("number", "22")]


def test_numeric_extractor_matches_german_and_turkish_cardinals():
    source = extract_numeric_facts("Zwei Männer schoben einen Kinderwagen.")
    target = extract_numeric_facts("İki erkek bir bebek arabasını itti.")

    assert [(fact.kind, fact.value) for fact in source] == [("number", "2")]
    assert [(fact.kind, fact.value) for fact in target] == [("number", "2")]


def test_numeric_extractor_expands_spoken_compound_score():
    facts = extract_numeric_facts("Der Rechtsaußen sorgte im Spiel für das 3 und 4 zu 0.")

    assert [(fact.kind, fact.value) for fact in facts] == [
        ("score", "3-0"),
        ("score", "4-0"),
    ]


def test_numeric_extractor_normalizes_percent_ranges():
    source = extract_numeric_facts("Die Ware ist 30-70% günstiger.")
    target = extract_numeric_facts("Ürünler yüzde 30-70 daha ucuz.")

    assert [(fact.kind, fact.value) for fact in source] == [
        ("percent", "30"),
        ("percent", "70"),
    ]
    assert [(fact.kind, fact.value) for fact in target] == [
        ("percent", "30"),
        ("percent", "70"),
    ]


def test_numeric_extractor_normalizes_spoken_turkish_percent_ranges():
    source = extract_numeric_facts("Die Ware ist 30-70% günstiger.")
    target = extract_numeric_facts("Ürünler yüzde 30 ila 70 daha ucuz.")

    assert [(fact.kind, fact.value) for fact in source] == [
        ("percent", "30"),
        ("percent", "70"),
    ]
    assert [(fact.kind, fact.value) for fact in target] == [
        ("percent", "30"),
        ("percent", "70"),
    ]


def test_numeric_extractor_normalizes_temperature_ranges():
    source = extract_numeric_facts("In der Nacht 16 bis 6°.")
    target = extract_numeric_facts("Gece 6 ila 16 derece.")

    assert {(fact.kind, fact.value) for fact in source} == {
        ("temperature", "6"),
        ("temperature", "16"),
    }
    assert {(fact.kind, fact.value) for fact in target} == {
        ("temperature", "6"),
        ("temperature", "16"),
    }


def test_numeric_extractor_normalizes_turkish_compounds_and_inflections():
    facts = extract_numeric_facts("On iki desteğin dördü ayakta kaldı. İkincilik ve üçüncülük.")

    assert [(fact.kind, fact.value) for fact in facts] == [
        ("number", "12"),
        ("number", "4"),
        ("number", "2"),
        ("number", "3"),
    ]


def test_numeric_extractor_matches_german_both_to_turkish_two():
    source = extract_numeric_facts("Die Hymnen der beiden Teams. Diese beiden feiern.")
    target = extract_numeric_facts("İki takımın marşları. Bu ikisi kutlama yapıyor.")

    assert [(fact.kind, fact.value) for fact in source] == [
        ("number", "2"),
        ("number", "2"),
    ]
    assert [(fact.kind, fact.value) for fact in target] == [
        ("number", "2"),
        ("number", "2"),
    ]


def test_weather_clause_classifies_all_values_as_temperatures():
    facts = extract_numeric_facts("Gündüz sıcaklıklar Harz'da 16, Hochrhein'da 27 derece.")

    assert [(fact.kind, fact.value) for fact in facts] == [
        ("temperature", "27"),
        ("temperature", "16"),
    ]


def test_numeric_range_bis_is_not_treated_as_chronology():
    document = _evaluate(
        "In der Nacht 16 bis 6°.",
        "Gece sıcaklıklar 6 ila 16 derece arasında.",
    )

    assert "chronology_suspicion" not in _categories(document)


def test_absolute_date_can_preserve_tomorrow_reference():
    document = _evaluate(
        "Die Vorhersage für morgen, Montag, den 20. Juli.",
        "20 Temmuz Pazartesi günü için hava tahmini.",
    )

    assert "chronology_suspicion" not in _categories(document)


def test_numeric_context_does_not_label_elapsed_time_as_casualties():
    source = (
        "Mehr als 3 Wochen sind seit dem Erdbeben vergangen. "
        "Die Zahl der Toten ist auf mehr als 5000 gestiegen."
    )
    target = "Depremden bu yana 3 haftadan fazla zaman geçti. Ölü sayısı 5 binden fazlaya yükseldi."

    document = _evaluate(source, target)

    assert not {
        "casualty_mismatch",
        "unexpected_casualty",
        "number_mismatch",
        "unexpected_number",
    } & set(_categories(document))


def test_degree_symbol_and_turkish_degree_word_are_equivalent():
    document = _evaluate(
        "Höchstwerte von 18° bis 26°.",
        "En yüksek sıcaklıklar 18 derece ile 26 derece arasında.",
    )

    assert "temperature_mismatch" not in _categories(document)
    assert "unexpected_temperature" not in _categories(document)


def test_turkish_temperature_suffix_is_accepted():
    document = _evaluate("Bis zu 9°.", "Sıcaklık 9 dereceye kadar düşecek.")

    assert "temperature_mismatch" not in _categories(document)
    assert "unexpected_number" not in _categories(document)


def test_intentionally_removed_intro_is_not_quality_checked():
    source_story, target_story = _story(
        "s01",
        "Heute im Studio Torsten Schröder um 20 Uhr.",
        "",
    )
    source_story["story_type"] = "intro"
    target_story["story_type"] = "intro"
    source, target = _documents([(source_story, target_story)])

    document = evaluate_translation_documents("ep-qa", source, target)

    assert document.findings == []


@pytest.fixture
def translation_qa_episode(db_session, tmp_path):
    episode = Episode(
        episode_id="ep-qa",
        source="youtube_rss",
        title="Translation QA",
        url="https://example.com/ep-qa",
        status=EpisodeStatus.TRANSLATED,
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    db_session.add(episode)
    db_session.commit()
    return episode


@pytest.fixture
def qa_settings(tmp_path):
    from btcedu.config import Settings

    return Settings(
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        profiles_dir="btcedu/profiles",
        pipeline_version=2,
        dry_run=False,
    )


def _write_story_artifacts(settings):
    source, target = _documents(
        [_story("s01", "Das Treffen ist am 12. Juli.", "Görüşme 12 Temmuz'da.", lead=True)]
    )
    output_dir = Path(settings.outputs_dir) / "ep-qa"
    output_dir.mkdir(parents=True)
    (output_dir / "stories.json").write_text(json.dumps(source), encoding="utf-8")
    (output_dir / "stories_translated.json").write_text(json.dumps(target), encoding="utf-8")


def test_stage_writes_zero_cost_artifact_and_is_idempotent(
    db_session,
    translation_qa_episode,
    qa_settings,
):
    _write_story_artifacts(qa_settings)
    first = run_translation_qa(db_session, "ep-qa", qa_settings)
    second = run_translation_qa(db_session, "ep-qa", qa_settings)
    document = load_translation_qa(qa_settings, "ep-qa")

    assert first.skipped is False
    assert second.skipped is True
    assert document["cost_usd"] == 0
    runs = (
        db_session.query(PipelineRun)
        .filter(PipelineRun.stage == PipelineStage.TRANSLATION_QA)
        .all()
    )
    assert len(runs) == 1
    assert runs[0].status == RunStatus.SUCCESS
    assert runs[0].estimated_cost_usd == 0


def test_force_updates_content_artifact_without_duplicates(
    db_session,
    translation_qa_episode,
    qa_settings,
):
    _write_story_artifacts(qa_settings)
    run_translation_qa(db_session, "ep-qa", qa_settings)
    run_translation_qa(db_session, "ep-qa", qa_settings, force=True)
    artifacts = (
        db_session.query(ContentArtifact)
        .filter_by(episode_id="ep-qa", artifact_type="translation_qa")
        .all()
    )
    assert len(artifacts) == 1


def test_corrupt_current_artifact_is_regenerated(
    db_session,
    translation_qa_episode,
    qa_settings,
):
    _write_story_artifacts(qa_settings)
    first = run_translation_qa(db_session, "ep-qa", qa_settings)
    Path(first.qa_path).write_text("{broken", encoding="utf-8")

    second = run_translation_qa(db_session, "ep-qa", qa_settings)

    assert second.skipped is False
    assert load_translation_qa(qa_settings, "ep-qa") is not None


def test_translation_qa_cli(
    db_session,
    translation_qa_episode,
    qa_settings,
):
    from btcedu.cli import cli

    _write_story_artifacts(qa_settings)
    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["translation-qa", "--episode-id", "ep-qa"],
        obj={"settings": qa_settings, "session_factory": lambda: db_session},
    )
    assert result.exit_code == 0
    assert "GREEN" in result.output
    assert "cost=$0.0000" in result.output


def test_llm_qa_runs_deterministic_check_first(
    db_session,
    tmp_path,
):
    from btcedu.config import Settings
    from btcedu.core.qa_reviewer import generate_qa_review
    from btcedu.services.claude_service import ClaudeResponse

    transcript_dir = tmp_path / "transcripts" / "ep-order"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "transcript.corrected.de.txt").write_text("Quelle.", encoding="utf-8")
    output_dir = tmp_path / "outputs" / "ep-order"
    output_dir.mkdir(parents=True)
    (output_dir / "script.adapted.tr.md").write_text("Hedef.", encoding="utf-8")
    episode = Episode(
        episode_id="ep-order",
        source="youtube_rss",
        title="Order",
        url="https://example.com/order",
        status=EpisodeStatus.ADAPTED,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()
    settings = Settings(
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        qa_review_enabled=True,
        qa_model="gpt-5.6-sol",
        dry_run=False,
    )
    order: list[str] = []

    def deterministic(*args, **kwargs):
        order.append("deterministic")
        return SimpleNamespace(
            skipped=True,
            qa_path="",
            reason="story-based source or target artifact missing",
        )

    def llm(*args, **kwargs):
        order.append("llm")
        return ClaudeResponse(
            text=json.dumps(
                {
                    "assessment": "No findings.",
                    "findings": [],
                    "disputed_deterministic_categories": [],
                }
            ),
            input_tokens=1,
            output_tokens=1,
            cost_usd=0,
            model="gpt-5.6-sol",
        )

    with (
        patch("btcedu.core.translation_qa.run_translation_qa", side_effect=deterministic),
        patch("btcedu.core.qa_reviewer.call_claude", side_effect=llm),
    ):
        generate_qa_review(db_session, "ep-order", settings)

    assert order == ["deterministic", "llm"]
