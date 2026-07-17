"""Tests for the independent QA second-opinion module."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from btcedu.core.qa_reviewer import (
    _count_issues,
    _render_markdown,
    generate_qa_review,
    load_qa_review,
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


def test_generate_qa_review_writes_artifacts(db_session, adapted_episode, qa_settings):
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _qa_json_response(VALID_QA)
        result = generate_qa_review(db_session, "ep_qa", qa_settings)

    assert not result.skipped
    assert result.overall_score == 9.1
    assert result.model == "gpt-5.6-sol"
    assert result.issue_count == 4  # 1 halluc + 1 neut + 2 story issues

    json_path = Path(result.json_path)
    md_path = Path(result.markdown_path)
    assert json_path.exists() and md_path.exists()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["overall_score"] == 9.1
    assert "getötete Ermittler erfunden" in md_path.read_text(encoding="utf-8")

    # PipelineRun recorded as REVIEW/SUCCESS
    run = (
        db_session.query(PipelineRun)
        .filter(PipelineRun.stage == PipelineStage.REVIEW)
        .first()
    )
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
        mock_call.return_value = _qa_json_response(VALID_QA)
        generate_qa_review(db_session, "ep_qa", qa_settings)
        result2 = generate_qa_review(db_session, "ep_qa", qa_settings)
        # Second run should not call the model again (fingerprint unchanged)
        assert mock_call.call_count == 1
    assert result2.skipped
    assert result2.overall_score == 9.1


def test_generate_qa_review_invalid_json(db_session, adapted_episode, qa_settings):
    with patch("btcedu.core.qa_reviewer.call_claude") as mock_call:
        mock_call.return_value = _qa_json_response("Entschuldigung, hier ist kein JSON.")
        result = generate_qa_review(db_session, "ep_qa", qa_settings)
    assert not result.skipped
    assert result.overall_score is None
    data = load_qa_review(qa_settings, "ep_qa")
    assert data is not None
    assert "raw_response" in data


def test_load_qa_review_absent(qa_settings):
    assert load_qa_review(qa_settings, "does_not_exist") is None


def test_render_markdown_and_count():
    data = json.loads(VALID_QA)
    md = _render_markdown(data, "gpt-5.6-sol")
    assert "Gesamtscore: 9.1/10" in md
    assert "Wettersatz korrigieren" in md
    assert _count_issues(data) == 4
