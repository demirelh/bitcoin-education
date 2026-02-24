"""Tests for the adaptation module (Sprint 5)."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from btcedu.core.adapter import (
    _classify_adaptation,
    _is_adaptation_current,
    _segment_text,
    _split_prompt,
    adapt_script,
    compute_adaptation_diff,
)
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus
from btcedu.models.review import ReviewStatus, ReviewTask

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def translated_episode(db_session, tmp_path):
    """Episode at TRANSLATED status with translation and German transcript files.

    Also includes approved Review Gate 1.
    """
    transcript_dir = tmp_path / "transcripts" / "ep_test"
    transcript_dir.mkdir(parents=True)

    # German corrected transcript
    corrected_path = transcript_dir / "transcript.corrected.de.txt"
    corrected_path.write_text(
        "Heute sprechen wir über Bitcoin und die Blockchain-Technologie.\n\n"
        "Die BaFin hat neue Regelungen erlassen. Ein Bitcoin kostet etwa 30.000 Euro.",
        encoding="utf-8",
    )

    # Turkish translation
    translation_path = transcript_dir / "transcript.tr.txt"
    translation_path.write_text(
        "Bugün Bitcoin ve Blockchain teknolojisi hakkında konuşacağız.\n\n"
        "BaFin yeni düzenlemeler yayınladı. Bir Bitcoin yaklaşık 30.000 Euro değerinde.",
        encoding="utf-8",
    )

    episode = Episode(
        episode_id="ep_test",
        source="youtube_rss",
        title="Bitcoin Temelleri",
        url="https://youtube.com/watch?v=ep_test",
        status=EpisodeStatus.TRANSLATED,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    # Add approved ReviewTask for Review Gate 1 (correction stage)
    # This is required for adaptation to proceed
    review_task = ReviewTask(
        episode_id="ep_test",
        stage="correct",
        status=ReviewStatus.APPROVED.value,
        artifact_paths="[]",
    )
    db_session.add(review_task)
    db_session.commit()

    return episode


@pytest.fixture
def translated_episode_no_approval(db_session, tmp_path):
    """Episode at TRANSLATED status without Review Gate 1 approval (for testing approval checks)."""
    transcript_dir = tmp_path / "transcripts" / "ep_test"
    transcript_dir.mkdir(parents=True)

    corrected_path = transcript_dir / "transcript.corrected.de.txt"
    corrected_path.write_text("Heute sprechen wir über Bitcoin.", encoding="utf-8")

    translation_path = transcript_dir / "transcript.tr.txt"
    translation_path.write_text("Bugün Bitcoin hakkında konuşacağız.", encoding="utf-8")

    episode = Episode(
        episode_id="ep_test",
        source="youtube_rss",
        title="Bitcoin Temelleri",
        url="https://youtube.com/watch?v=ep_test",
        status=EpisodeStatus.TRANSLATED,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()
    return episode


@pytest.fixture
def mock_settings(tmp_path):
    """Settings object with tmp_path directories and dry_run=True."""
    from btcedu.config import Settings

    return Settings(
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        dry_run=True,
        anthropic_api_key="test-key",
        pipeline_version=2,
    )


@pytest.fixture
def mock_claude_adapt_response():
    """Mock Claude response for adaptation."""
    return {
        "text": (
            "Bugün Bitcoin ve Blockchain teknolojisi hakkında konuşacağız.\n\n"
            "`[T1: SPK (Sermaye Piyasası Kurulu)]` yeni düzenlemeler yayınladı. "
            "Bir Bitcoin yaklaşık `[T1: 30.000 USD]` değerinde."
        ),
        "input_tokens": 200,
        "output_tokens": 150,
        "cost_usd": 0.005,
    }


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------


def test_split_prompt():
    """Test splitting prompt template into system and user parts."""
    template = """---
You are an adapter.
---
# System
System instructions here.

# Input
{{ translation }}
"""
    system, user = _split_prompt(template)
    assert "System instructions" in system
    assert "# Input" in user
    assert "{{ translation }}" in user


def test_segment_text_short():
    """Test segmentation with text shorter than limit."""
    text = "Short text"
    segments = _segment_text(text, limit=100)
    assert len(segments) == 1
    assert segments[0] == text


def test_segment_text_long():
    """Test segmentation with text longer than limit."""
    # Create text with paragraphs
    para1 = "A" * 8000
    para2 = "B" * 8000
    text = f"{para1}\n\n{para2}"
    segments = _segment_text(text, limit=10000)
    assert len(segments) == 2
    assert para1 in segments[0]
    assert para2 in segments[1]


def test_classify_adaptation_institution():
    """Test classification of institution replacement adaptations."""
    assert _classify_adaptation("BaFin → SPK") == "institution_replacement"
    assert _classify_adaptation("Sparkasse → yerel banka") == "institution_replacement"


def test_classify_adaptation_currency():
    """Test classification of currency conversion adaptations."""
    assert _classify_adaptation("30.000 EUR → 30.000 USD") == "currency_conversion"
    assert _classify_adaptation("€50 → ~2.000 TL") == "currency_conversion"


def test_classify_adaptation_tone():
    """Test classification of tone adjustment adaptations."""
    assert _classify_adaptation("ton düzeltmesi") == "tone_adjustment"


def test_classify_adaptation_legal_removal():
    """Test classification of legal removal adaptations."""
    assert _classify_adaptation("[kaldırıldı: Almanya'ya özgü vergi bilgisi]") == "legal_removal"


def test_classify_adaptation_cultural():
    """Test classification of cultural reference adaptations."""
    assert _classify_adaptation("kültürel uyarlama: Oktoberfest → festival") == "cultural_reference"


def test_classify_adaptation_regulatory():
    """Test classification of regulatory context adaptations."""
    assert _classify_adaptation("Türkiye'de düzenleme farklıdır") == "regulatory_context"


def test_compute_adaptation_diff():
    """Test computing adaptation diff from tagged text."""
    translation = "BaFin yeni düzenlemeler yayınladı. Bir Bitcoin 30.000 Euro değerinde."
    adapted = (
        "`[T1: SPK (Sermaye Piyasası Kurulu)]` yeni düzenlemeler yayınladı. "
        "Bir Bitcoin `[T1: 30.000 USD]` değerinde."
    )

    diff = compute_adaptation_diff(translation, adapted, "ep_test")

    assert diff["episode_id"] == "ep_test"
    assert diff["original_length"] == len(translation)
    assert diff["adapted_length"] == len(adapted)
    assert len(diff["adaptations"]) == 2  # Two [T1] tags

    # Check summary
    summary = diff["summary"]
    assert summary["total_adaptations"] == 2
    assert summary["tier1_count"] == 2
    assert summary["tier2_count"] == 0

    # Check adaptation entries
    adaptations = diff["adaptations"]
    assert all(a["tier"] == "T1" for a in adaptations)
    assert adaptations[0]["category"] == "institution_replacement"
    assert adaptations[1]["category"] == "currency_conversion"


def test_compute_adaptation_diff_mixed_tiers():
    """Test computing adaptation diff with mixed T1/T2 tags."""
    translation = "Original text"
    adapted = "`[T1: adaptation1]` and `[T2: adaptation2]`"

    diff = compute_adaptation_diff(translation, adapted, "ep_test")

    assert diff["summary"]["total_adaptations"] == 2
    assert diff["summary"]["tier1_count"] == 1
    assert diff["summary"]["tier2_count"] == 1


def test_is_adaptation_current_missing_file(tmp_path):
    """Test idempotency check when adapted file doesn't exist."""
    adapted_path = tmp_path / "adapted.md"
    provenance_path = tmp_path / "provenance.json"

    result = _is_adaptation_current(
        adapted_path,
        provenance_path,
        "translation_hash",
        "german_hash",
        "prompt_hash",
    )

    assert result is False


def test_is_adaptation_current_stale_marker(tmp_path):
    """Test idempotency check when .stale marker exists."""
    adapted_path = tmp_path / "adapted.md"
    adapted_path.write_text("adapted content", encoding="utf-8")

    stale_marker = tmp_path / "adapted.md.stale"
    stale_marker.write_text("upstream changed", encoding="utf-8")

    provenance_path = tmp_path / "provenance.json"
    provenance_path.write_text("{}", encoding="utf-8")

    result = _is_adaptation_current(
        adapted_path,
        provenance_path,
        "translation_hash",
        "german_hash",
        "prompt_hash",
    )

    assert result is False
    assert not stale_marker.exists()  # Marker should be consumed


def test_is_adaptation_current_hash_mismatch(tmp_path):
    """Test idempotency check when content hashes don't match."""
    adapted_path = tmp_path / "adapted.md"
    adapted_path.write_text("adapted content", encoding="utf-8")

    provenance_path = tmp_path / "provenance.json"
    provenance_data = {
        "prompt_hash": "old_prompt",
        "input_content_hashes": {
            "translation": "old_translation",
            "german": "old_german",
        },
    }
    provenance_path.write_text(json.dumps(provenance_data), encoding="utf-8")

    result = _is_adaptation_current(
        adapted_path,
        provenance_path,
        "new_translation",
        "new_german",
        "new_prompt",
    )

    assert result is False


def test_is_adaptation_current_all_match(tmp_path):
    """Test idempotency check when all hashes match (should skip)."""
    adapted_path = tmp_path / "adapted.md"
    adapted_path.write_text("adapted content", encoding="utf-8")

    provenance_path = tmp_path / "provenance.json"
    provenance_data = {
        "prompt_hash": "prompt123",
        "input_content_hashes": {
            "translation": "trans123",
            "german": "german123",
        },
    }
    provenance_path.write_text(json.dumps(provenance_data), encoding="utf-8")

    result = _is_adaptation_current(
        adapted_path,
        provenance_path,
        "trans123",
        "german123",
        "prompt123",
    )

    assert result is True


# ---------------------------------------------------------------------------
# Integration Tests
# ---------------------------------------------------------------------------


@patch("btcedu.core.adapter.call_claude")
def test_adapt_script_success(
    mock_call_claude,
    translated_episode,
    mock_settings,
    db_session,
    mock_claude_adapt_response,
):
    """Test successful adaptation of a translated episode."""
    mock_call_claude.return_value = type("Response", (), mock_claude_adapt_response)

    result = adapt_script(db_session, "ep_test", mock_settings, force=False)

    assert result.episode_id == "ep_test"
    assert result.skipped is False
    assert result.adaptation_count == 2  # Two [T1] tags in mock response
    assert result.tier1_count == 2
    assert result.tier2_count == 0
    assert result.cost_usd == 0.005

    # Check that episode status was updated
    db_session.refresh(translated_episode)
    assert translated_episode.status == EpisodeStatus.ADAPTED

    # Check files were written
    adapted_path = (
        Path(mock_settings.outputs_dir) / "ep_test" / "script.adapted.tr.md"
    )
    assert adapted_path.exists()

    diff_path = (
        Path(mock_settings.outputs_dir) / "ep_test" / "review" / "adaptation_diff.json"
    )
    assert diff_path.exists()

    provenance_path = (
        Path(mock_settings.outputs_dir)
        / "ep_test"
        / "provenance"
        / "adapt_provenance.json"
    )
    assert provenance_path.exists()

    # Check provenance content
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    assert provenance["stage"] == "adapt"
    assert provenance["episode_id"] == "ep_test"
    assert "input_content_hashes" in provenance
    assert "translation" in provenance["input_content_hashes"]
    assert "german" in provenance["input_content_hashes"]


@patch("btcedu.core.adapter.call_claude")
def test_adapt_script_idempotent(
    mock_call_claude,
    translated_episode,
    mock_settings,
    db_session,
    mock_claude_adapt_response,
):
    """Test that second run is skipped due to idempotency."""
    mock_call_claude.return_value = type("Response", (), mock_claude_adapt_response)

    # First run
    result1 = adapt_script(db_session, "ep_test", mock_settings, force=False)
    assert result1.skipped is False

    # Second run (should skip)
    result2 = adapt_script(db_session, "ep_test", mock_settings, force=False)
    assert result2.skipped is True
    assert result2.adaptation_count == 2  # Still reports from cached diff


@patch("btcedu.core.adapter.call_claude")
def test_adapt_script_force_rerun(mock_call_claude, translated_episode, mock_settings, db_session, mock_claude_adapt_response):
    """Test that force=True re-runs even if output exists."""
    mock_call_claude.return_value = type("Response", (), mock_claude_adapt_response)

    # First run
    result1 = adapt_script(db_session, "ep_test", mock_settings, force=False)
    assert result1.skipped is False

    # Force re-run
    result2 = adapt_script(db_session, "ep_test", mock_settings, force=True)
    assert result2.skipped is False
    assert mock_call_claude.call_count == 2


def test_adapt_script_missing_episode(db_session, mock_settings):
    """Test that adaptation fails if episode not found."""
    with pytest.raises(ValueError, match="Episode not found"):
        adapt_script(db_session, "nonexistent", mock_settings)


def test_adapt_script_wrong_status(db_session, mock_settings):
    """Test that adaptation fails if episode is not TRANSLATED."""
    episode = Episode(
        episode_id="ep_wrong_status",
        source="youtube_rss",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.CORRECTED,  # Wrong status
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    with pytest.raises(ValueError, match="expected 'translated'"):
        adapt_script(db_session, "ep_wrong_status", mock_settings)


def test_adapt_script_missing_translation(translated_episode, mock_settings, db_session, tmp_path):
    """Test that adaptation fails if translation file is missing."""
    # Remove translation file
    translation_path = Path(mock_settings.transcripts_dir) / "ep_test" / "transcript.tr.txt"
    translation_path.unlink()

    with pytest.raises(FileNotFoundError, match="Turkish translation not found"):
        adapt_script(db_session, "ep_test", mock_settings)


def test_adapt_script_missing_german(
    translated_episode, mock_settings, db_session, tmp_path
):
    """Test that adaptation fails if German corrected transcript is missing."""
    # Remove German file
    corrected_path = (
        Path(mock_settings.transcripts_dir)
        / "ep_test"
        / "transcript.corrected.de.txt"
    )
    corrected_path.unlink()

    with pytest.raises(FileNotFoundError, match="Corrected German transcript not found"):
        adapt_script(db_session, "ep_test", mock_settings)


def test_adapt_script_no_review_approval(
    translated_episode_no_approval, mock_settings, db_session
):
    """Test that adaptation fails if Review Gate 1 (correction) is not approved."""
    with pytest.raises(ValueError, match="correction has not been approved"):
        adapt_script(db_session, "ep_test", mock_settings)


@patch("btcedu.core.adapter.call_claude")
def test_adapt_script_pipeline_run_tracking(
    mock_call_claude,
    translated_episode,
    mock_settings,
    db_session,
    mock_claude_adapt_response,
):
    """Test that PipelineRun is created and updated correctly."""
    mock_call_claude.return_value = type("Response", (), mock_claude_adapt_response)

    adapt_script(db_session, "ep_test", mock_settings, force=False)

    # Check PipelineRun was created
    pipeline_run = (
        db_session.query(PipelineRun)
        .filter(
            PipelineRun.episode_id == translated_episode.id,
            PipelineRun.stage == PipelineStage.ADAPT,
        )
        .first()
    )

    assert pipeline_run is not None
    assert pipeline_run.status == RunStatus.SUCCESS
    assert pipeline_run.input_tokens == 200
    assert pipeline_run.output_tokens == 150
    assert pipeline_run.estimated_cost_usd == 0.005


@patch("btcedu.core.adapter.call_claude")
def test_adapt_script_error_handling(mock_call_claude, translated_episode, mock_settings, db_session):
    """Test that errors are properly recorded in PipelineRun and Episode."""
    mock_call_claude.side_effect = RuntimeError("Claude API error")

    with pytest.raises(RuntimeError, match="Claude API error"):
        adapt_script(db_session, "ep_test", mock_settings)

    # Check PipelineRun was marked as failed
    pipeline_run = (
        db_session.query(PipelineRun)
        .filter(
            PipelineRun.episode_id == translated_episode.id,
            PipelineRun.stage == PipelineStage.ADAPT,
        )
        .first()
    )

    assert pipeline_run is not None
    assert pipeline_run.status == RunStatus.FAILED
    assert "Claude API error" in pipeline_run.error_message

    # Check episode error message was set
    db_session.refresh(translated_episode)
    assert translated_episode.error_message is not None
    assert "Adaptation failed" in translated_episode.error_message


@patch("btcedu.core.adapter.call_claude")
def test_adapt_script_content_artifact(
    mock_call_claude,
    translated_episode,
    mock_settings,
    db_session,
    mock_claude_adapt_response,
):
    """Test that ContentArtifact is created correctly."""
    from btcedu.models.content_artifact import ContentArtifact

    mock_call_claude.return_value = type("Response", (), mock_claude_adapt_response)

    adapt_script(db_session, "ep_test", mock_settings, force=False)

    # Check ContentArtifact was created
    artifact = (
        db_session.query(ContentArtifact)
        .filter(
            ContentArtifact.episode_id == "ep_test",
            ContentArtifact.artifact_type == "adapt",
        )
        .first()
    )

    assert artifact is not None
    assert "script.adapted.tr.md" in artifact.file_path
    assert artifact.model == mock_settings.claude_model


# ---------------------------------------------------------------------------
# CLI Tests
# ---------------------------------------------------------------------------


@patch("btcedu.core.adapter.call_claude")
def test_cli_adapt_command(
    mock_call_claude,
    translated_episode,
    mock_settings,
    db_session,
    mock_claude_adapt_response,
    tmp_path,
):
    """Test the 'btcedu adapt' CLI command."""
    from click.testing import CliRunner

    from btcedu.cli import cli

    mock_call_claude.return_value = type("Response", (), mock_claude_adapt_response)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["adapt", "--episode-id", "ep_test", "--dry-run"],
        obj={"settings": mock_settings, "session_factory": lambda: db_session},
    )

    assert result.exit_code == 0
    assert "OK" in result.output
    assert "ep_test" in result.output
    assert "adaptations" in result.output


@patch("btcedu.core.adapter.call_claude")
def test_cli_adapt_command_force(
    mock_call_claude,
    translated_episode,
    mock_settings,
    db_session,
    mock_claude_adapt_response,
):
    """Test the 'btcedu adapt --force' CLI command."""
    from click.testing import CliRunner

    from btcedu.cli import cli

    mock_call_claude.return_value = type("Response", (), mock_claude_adapt_response)

    runner = CliRunner()

    # First run
    result1 = runner.invoke(
        cli,
        ["adapt", "--episode-id", "ep_test", "--dry-run"],
        obj={"settings": mock_settings, "session_factory": lambda: db_session},
    )
    assert result1.exit_code == 0

    # Second run without force (should skip)
    result2 = runner.invoke(
        cli,
        ["adapt", "--episode-id", "ep_test", "--dry-run"],
        obj={"settings": mock_settings, "session_factory": lambda: db_session},
    )
    assert result2.exit_code == 0
    assert "SKIP" in result2.output

    # Third run with force (should re-run)
    result3 = runner.invoke(
        cli,
        ["adapt", "--episode-id", "ep_test", "--dry-run", "--force"],
        obj={"settings": mock_settings, "session_factory": lambda: db_session},
    )
    assert result3.exit_code == 0
    assert "OK" in result3.output
