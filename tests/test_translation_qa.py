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


def test_non_sport_ratio_is_not_treated_as_score():
    document = _evaluate(
        "Die Partei gewann die Abstimmung mit 2 zu 1 Stimmen.",
        "Parti oylamayı 2'ye karşı 1 oyla kazandı.",
    )
    assert "score_mismatch" not in _categories(document)


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
                    "overall_score": 10,
                    "summary": "",
                    "stories": [],
                    "missing_content": [],
                    "hallucinations": [],
                    "neutralization_gaps": [],
                    "top_fixes": [],
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
