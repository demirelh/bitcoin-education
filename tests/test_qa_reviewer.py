"""Tests for the independent QA second-opinion module."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from btcedu.core.qa_reviewer import (
    _count_issues,
    _render_markdown,
    format_qa_feedback,
    generate_qa_review,
    load_qa_review,
    load_quality_gate,
)
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus
from btcedu.services.claude_service import ClaudeResponse


@pytest.fixture
def adapted_episode(db_session, tmp_path):
    """Episode at ADAPTED status with German source + adapted Turkish script."""
    transcript_dir = tmp_path / "transcripts" / "ep_qa"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "transcript.corrected.de.txt").write_text(
        "Steuerbetrug: Die Regierung stellt einen Aktionsplan vor.\n\n"
        "Wetter: Am Montag Schauer, im Südwesten Sonne.",
        encoding="utf-8",
    )

    outputs_dir = tmp_path / "outputs" / "ep_qa"
    outputs_dir.mkdir(parents=True)
    (outputs_dir / "script.adapted.tr.md").write_text(
        "Vergi dolandırıcılığı: Hükümet bir eylem planı sundu.\n\n"
        "Hava durumu: Pazartesi sağanak, güneybatıda güneş.",
        encoding="utf-8",
    )

    episode = Episode(
        episode_id="ep_qa",
        source="youtube_rss",
        title="Test QA",
        url="https://youtube.com/watch?v=ep_qa",
        status=EpisodeStatus.ADAPTED,
        pipeline_version=2,
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
        dry_run=False,
        anthropic_api_key="test-key",
        qa_review_enabled=True,
        qa_model="gpt-5.6-sol",
        pipeline_version=2,
    )


def _qa_json_response(text: str) -> ClaudeResponse:
    return ClaudeResponse(
        text=text, input_tokens=100, output_tokens=200, cost_usd=0.0, model="gpt-5.6-sol"
    )


VALID_QA = json.dumps(
    {
        "overall_score": 9.1,
        "summary": "Insgesamt gut, kleine Fehler.",
        "stories": [
            {"title": "Steuerbetrug", "score": 8.5, "issues": ["inszenierte Vorführung"]},
            {"title": "Wetter", "score": 6.0, "issues": ["Aussage umgekehrt"]},
        ],
        "missing_content": [],
        "hallucinations": ["getötete Ermittler erfunden"],
        "neutralization_gaps": ["Moderatorname noch vorhanden"],
        "top_fixes": ["Wettersatz korrigieren"],
    },
    ensure_ascii=False,
)

# Phase 7 structured findings output (single minor finding -> GREEN within limits).
STRUCTURED_QA_MINOR = json.dumps(
    {
        "assessment": "Kleinere Neutralisierungslücke.",
        "findings": [
            {
                "story_id": None,
                "category": "neutralization_gap",
                "severity": "minor",
                "source_excerpt": "Guten Abend",
                "target_excerpt": "İyi akşamlar",
                "explanation": "Moderatorgruß noch vorhanden",
                "required_action": "Gruß entfernen",
            }
        ],
        "disputed_deterministic_categories": [],
    },
    ensure_ascii=False,
)


def test_generate_qa_review_writes_artifacts(db_session, adapted_episode, qa_settings):
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _qa_json_response(STRUCTURED_QA_MINOR)
        result = generate_qa_review(db_session, "ep_qa", qa_settings)

    assert not result.skipped
    # One minor finding, within the default max_minor_findings -> GREEN.
    assert result.decision == "green"
    assert result.blocked is False
    assert result.narration_sha256 is not None  # GREEN records the narration hash

    gate = load_quality_gate(qa_settings, "ep_qa")
    assert gate is not None
    assert gate["decision"] == "green"
    assert gate["summary"]["minor_count"] == 1
    assert gate["narration_approved"] is True

    # Backward-compatible legacy artifacts still written.
    assert Path(result.json_path).exists()
    assert Path(result.markdown_path).exists()

    # PipelineRun recorded as REVIEW/SUCCESS with structured cost.
    run = db_session.query(PipelineRun).filter(PipelineRun.stage == PipelineStage.REVIEW).first()
    assert run is not None and run.status == RunStatus.SUCCESS


def test_model_override_passed(db_session, adapted_episode, qa_settings):
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _qa_json_response(VALID_QA)
        generate_qa_review(db_session, "ep_qa", qa_settings)
    assert mock_call.call_args.kwargs["model_override"] == "gpt-5.6-sol"
    assert mock_call.call_args.kwargs["json_mode"] is True


def test_generate_qa_review_disabled(db_session, adapted_episode, qa_settings):
    qa_settings.qa_review_enabled = False
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        result = generate_qa_review(db_session, "ep_qa", qa_settings)
    assert result.skipped
    mock_call.assert_not_called()


def test_generate_qa_review_dry_run_skips(db_session, adapted_episode, qa_settings):
    qa_settings.dry_run = True
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        result = generate_qa_review(db_session, "ep_qa", qa_settings)
    assert result.skipped
    mock_call.assert_not_called()


def test_generate_qa_review_missing_adapted(db_session, qa_settings, tmp_path):
    transcript_dir = tmp_path / "transcripts" / "ep_none"
    transcript_dir.mkdir(parents=True)
    (transcript_dir / "transcript.corrected.de.txt").write_text("x", encoding="utf-8")
    episode = Episode(
        episode_id="ep_none",
        source="youtube_rss",
        title="No adapt",
        url="https://youtube.com/watch?v=ep_none",
        status=EpisodeStatus.ADAPTED,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        result = generate_qa_review(db_session, "ep_none", qa_settings)
    assert result.skipped
    mock_call.assert_not_called()


def test_generate_qa_review_idempotent(db_session, adapted_episode, qa_settings):
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _qa_json_response(STRUCTURED_QA_MINOR)
        generate_qa_review(db_session, "ep_qa", qa_settings)
        result2 = generate_qa_review(db_session, "ep_qa", qa_settings)
        # Second run should not call the model again (fingerprint unchanged)
        assert mock_call.call_count == 1
    assert result2.skipped
    assert result2.decision == "green"


def test_generate_qa_review_invalid_json(db_session, adapted_episode, qa_settings):
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _qa_json_response("Entschuldigung, hier ist kein JSON.")
        result = generate_qa_review(db_session, "ep_qa", qa_settings)
    assert not result.skipped
    # Invalid model output surfaces as a visible major finding (never silent GREEN).
    assert result.decision == "yellow"
    gate = load_quality_gate(qa_settings, "ep_qa")
    assert gate is not None
    assert any(f["category"] == "qa_model_error" for f in gate["findings"])


def test_load_qa_review_absent(qa_settings):
    assert load_qa_review(qa_settings, "does_not_exist") is None


def test_render_markdown_and_count():
    data = json.loads(VALID_QA)
    md = _render_markdown(data, "gpt-5.6-sol")
    assert "Gesamtscore: 9.1/10" in md
    assert "Wettersatz korrigieren" in md
    assert _count_issues(data) == 4


def _write_qa_json(settings, episode_id, payload):
    base = Path(settings.outputs_dir) / episode_id
    base.mkdir(parents=True, exist_ok=True)
    (base / "qa_review.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_format_qa_feedback_absent(qa_settings):
    assert format_qa_feedback(qa_settings, "no_qa_here") is None


def test_format_qa_feedback_builds_block(qa_settings):
    _write_qa_json(qa_settings, "ep_qa", json.loads(VALID_QA))
    block = format_qa_feedback(qa_settings, "ep_qa")
    assert block is not None
    assert "QA-Zweitmeinung des vorherigen Laufs" in block
    assert "gpt-5.6-sol" in block
    assert "WICHTIGSTE KORREKTUREN" in block
    assert "Wettersatz korrigieren" in block
    assert "ERFUNDENE FAKTEN" in block
    assert "getötete Ermittler erfunden" in block
    assert "NEUTRALISIERUNG" in block
    assert "[Wetter]" in block


def test_format_qa_feedback_caps_story_issues(qa_settings):
    payload = {
        "overall_score": 7.0,
        "summary": "",
        "stories": [
            {"title": "A", "score": 7, "issues": ["i1", "i2", "i3", "i4"]},
        ],
        "missing_content": [],
        "hallucinations": [],
        "neutralization_gaps": [],
        "top_fixes": [],
    }
    _write_qa_json(qa_settings, "ep_qa", payload)
    block = format_qa_feedback(qa_settings, "ep_qa", max_story_issues=2)
    assert "i1" in block and "i2" in block
    assert "i3" not in block and "i4" not in block


def test_format_qa_feedback_empty_when_no_issues(qa_settings):
    payload = {
        "overall_score": 10.0,
        "summary": "perfekt",
        "stories": [],
        "missing_content": [],
        "hallucinations": [],
        "neutralization_gaps": [],
        "top_fixes": [],
    }
    _write_qa_json(qa_settings, "ep_qa", payload)
    assert format_qa_feedback(qa_settings, "ep_qa") is None
