"""Tests for safe corrected transcripts and the transcript QA gate."""

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from btcedu.core.pipeline import _run_stage
from btcedu.core.reviewer import approve_review, get_review_detail
from btcedu.core.transcript_qa import evaluate_transcript_qa, load_transcript_qa
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus
from btcedu.models.review import ReviewStatus, ReviewTask
from btcedu.models.transcript_schema import (
    CorrectedTranscriptDocument,
    CorrectedTranscriptSegment,
)


@pytest.fixture
def qa_settings(tmp_path):
    from btcedu.config import Settings

    return Settings(
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        pipeline_version=2,
        dry_run=True,
    )


@pytest.fixture
def corrected_episode(db_session, qa_settings):
    transcript_path = Path(qa_settings.transcripts_dir) / "ep_qa" / "transcript.clean.de.txt"
    transcript_path.parent.mkdir(parents=True, exist_ok=True)
    transcript_path.write_text("Es gab zwölf Opfer.", encoding="utf-8")
    episode = Episode(
        episode_id="ep_qa",
        source="youtube_rss",
        title="Transcript QA",
        url="https://example.com/ep_qa",
        status=EpisodeStatus.CORRECTED,
        pipeline_version=2,
        content_profile="bitcoin_podcast",
        transcript_path=str(transcript_path),
    )
    db_session.add(episode)
    db_session.commit()
    return episode


def _write_corrected(qa_settings, *, flags=None, status="verified", severity="none"):
    flags = flags or []
    reason = "Unsicherheit ist ungeklärt." if status in {"uncertain", "unresolved"} else None
    document = CorrectedTranscriptDocument(
        episode_id="ep_qa",
        language="de",
        segments=[
            CorrectedTranscriptSegment(
                segment_id="seg-0001",
                start_seconds=10,
                end_seconds=14,
                original_text="Es gab zwölf Opfer.",
                corrected_text="Es gab zwölf Opfer.",
                status=status,
                severity=severity,
                flags=flags,
                reason=reason,
            )
        ],
        full_text="Es gab zwölf Opfer.",
    )
    path = Path(qa_settings.transcripts_dir) / "ep_qa" / "transcript.corrected.structured.de.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(document.model_dump(mode="json"), ensure_ascii=False),
        encoding="utf-8",
    )
    path.with_name("transcript.corrected.de.txt").write_text(
        document.full_text,
        encoding="utf-8",
    )
    return path


def test_corrected_schema_requires_reason_for_unresolved():
    with pytest.raises(ValueError, match="require a reason"):
        CorrectedTranscriptSegment(
            segment_id="seg-0001",
            start_seconds=0,
            end_seconds=1,
            original_text="Fragment",
            corrected_text="Fragment",
            status="unresolved",
            severity="major",
        )


def test_critical_casualty_uncertainty_blocks(db_session, corrected_episode, qa_settings):
    _write_corrected(
        qa_settings,
        flags=["casualty_disagreement"],
        status="unresolved",
        severity="critical",
    )

    result = evaluate_transcript_qa(db_session, "ep_qa", qa_settings)
    document = load_transcript_qa(qa_settings, "ep_qa")

    assert result.blocked is True
    assert document["status"] == "red"
    assert document["findings"][0]["category"] == "casualty_uncertainty"
    assert document["findings"][0]["blocking"] is True


def test_unresolved_negation_blocks(db_session, corrected_episode, qa_settings):
    _write_corrected(
        qa_settings,
        flags=["negation_disagreement"],
        status="unresolved",
        severity="critical",
    )

    result = evaluate_transcript_qa(db_session, "ep_qa", qa_settings)

    assert result.blocked is True
    assert load_transcript_qa(qa_settings, "ep_qa")["findings"][0]["category"] == (
        "unresolved_negation"
    )


def test_possible_free_reconstruction_does_not_pass_green(
    db_session, corrected_episode, qa_settings
):
    _write_corrected(
        qa_settings,
        flags=["possible_free_reconstruction"],
        status="unresolved",
        severity="major",
    )

    result = evaluate_transcript_qa(db_session, "ep_qa", qa_settings)

    assert result.status == "yellow"
    assert load_transcript_qa(qa_settings, "ep_qa")["findings"][0]["category"] == (
        "incomplete_sentence"
    )


def test_punctuation_only_does_not_block(db_session, corrected_episode, qa_settings):
    _write_corrected(qa_settings)

    result = evaluate_transcript_qa(db_session, "ep_qa", qa_settings)

    assert result.blocked is False
    assert result.status == "green"
    assert result.finding_count == 0


def test_disabled_auto_continue_does_not_block_clean_transcript(
    db_session,
    corrected_episode,
    qa_settings,
    monkeypatch,
):
    _write_corrected(qa_settings)
    monkeypatch.setattr(
        "btcedu.core.transcript_qa._load_config",
        lambda settings, episode: {
            "enabled": True,
            "block_on_critical": True,
            "max_major_findings": 3,
            "auto_continue_below_threshold": False,
        },
    )

    result = evaluate_transcript_qa(db_session, "ep_qa", qa_settings)

    assert result.blocked is False
    assert result.status == "green"


def test_qa_is_idempotent(db_session, corrected_episode, qa_settings):
    _write_corrected(qa_settings)
    evaluate_transcript_qa(db_session, "ep_qa", qa_settings)

    second = evaluate_transcript_qa(db_session, "ep_qa", qa_settings)
    runs = (
        db_session.query(PipelineRun).filter(PipelineRun.stage == PipelineStage.TRANSCRIPT_QA).all()
    )

    assert second.skipped is True
    assert len(runs) == 1
    assert runs[0].status == RunStatus.SUCCESS


def test_blocking_gate_creates_review_and_resumes_after_approval(
    db_session, corrected_episode, qa_settings
):
    _write_corrected(
        qa_settings,
        flags=["casualty_disagreement"],
        status="unresolved",
        severity="critical",
    )
    evaluate_transcript_qa(db_session, "ep_qa", qa_settings)

    pending = _run_stage(
        db_session,
        corrected_episode,
        qa_settings,
        "review_gate_transcript_qa",
    )
    task = db_session.query(ReviewTask).filter_by(episode_id="ep_qa").one()
    assert pending.status == "review_pending"
    assert task.stage == "transcript_qa"
    assert task.status == ReviewStatus.PENDING.value

    approve_review(db_session, task.id, notes="Verified against source audio")
    resumed = _run_stage(
        db_session,
        corrected_episode,
        qa_settings,
        "review_gate_transcript_qa",
    )
    assert resumed.status == "success"


def test_new_artifacts_require_new_approval(db_session, corrected_episode, qa_settings):
    _write_corrected(
        qa_settings,
        flags=["casualty_disagreement"],
        status="unresolved",
        severity="critical",
    )
    evaluate_transcript_qa(db_session, "ep_qa", qa_settings)
    _run_stage(
        db_session,
        corrected_episode,
        qa_settings,
        "review_gate_transcript_qa",
    )
    first_task = db_session.query(ReviewTask).filter_by(episode_id="ep_qa").one()
    approve_review(db_session, first_task.id)

    _write_corrected(
        qa_settings,
        flags=["negation_disagreement"],
        status="unresolved",
        severity="critical",
    )
    evaluate_transcript_qa(db_session, "ep_qa", qa_settings, force=True)
    gate = _run_stage(
        db_session,
        corrected_episode,
        qa_settings,
        "review_gate_transcript_qa",
    )
    tasks = (
        db_session.query(ReviewTask)
        .filter_by(episode_id="ep_qa", stage="transcript_qa")
        .order_by(ReviewTask.id)
        .all()
    )

    assert gate.status == "review_pending"
    assert len(tasks) == 2
    assert tasks[0].status == ReviewStatus.APPROVED.value
    assert tasks[1].status == ReviewStatus.PENDING.value


def test_review_detail_contains_transcript_qa(
    db_session, corrected_episode, qa_settings, monkeypatch
):
    _write_corrected(
        qa_settings,
        flags=["negation_disagreement"],
        status="unresolved",
        severity="critical",
    )
    evaluate_transcript_qa(db_session, "ep_qa", qa_settings)
    _run_stage(
        db_session,
        corrected_episode,
        qa_settings,
        "review_gate_transcript_qa",
    )
    task = db_session.query(ReviewTask).filter_by(episode_id="ep_qa").one()
    monkeypatch.setattr("btcedu.core.reviewer._get_runtime_settings", lambda: qa_settings)

    detail = get_review_detail(db_session, task.id)

    assert detail["transcript_qa"]["status"] == "red"
    assert detail["transcript_qa"]["findings"][0]["primary_text"]
    assert detail["transcript_qa"]["findings"][0]["corrected_text"]
    assert detail["original_text"] == "Es gab zwölf Opfer."
    assert detail["corrected_text"] == "Es gab zwölf Opfer."


def test_transcript_qa_cli_help():
    from btcedu.cli import cli

    result = CliRunner().invoke(cli, ["transcript-qa", "--help"])
    assert result.exit_code == 0
    assert "--episode-id" in result.output
    assert "--force" in result.output
