"""Tests for the transcript correction module (Sprint 2)."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from btcedu.core.corrector import (
    CorrectionResult,
    _is_correction_current,
    _segment_transcript,
    _split_prompt,
    compute_correction_diff,
    correct_transcript,
)
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def transcribed_episode(db_session, tmp_path):
    """Episode at TRANSCRIBED status with a transcript file."""
    transcript_dir = tmp_path / "transcripts" / "ep_test"
    transcript_dir.mkdir(parents=True)
    transcript_path = transcript_dir / "transcript.clean.de.txt"
    transcript_path.write_text(
        "Heute sprechen wir über Bit Coin und die Blok Chain Technologie.\n\n"
        "Es ist eine dezentrale Währung die von Sattoshi Nakamoto erfunden wurde.",
        encoding="utf-8",
    )

    episode = Episode(
        episode_id="ep_test",
        source="youtube_rss",
        title="Bitcoin Grundlagen",
        url="https://youtube.com/watch?v=ep_test",
        status=EpisodeStatus.TRANSCRIBED,
        transcript_path=str(transcript_path),
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


# ---------------------------------------------------------------------------
# Unit tests: compute_correction_diff
# ---------------------------------------------------------------------------


class TestComputeCorrectionDiff:
    def test_no_changes(self):
        text = "Bitcoin ist eine dezentrale Währung."
        diff = compute_correction_diff(text, text, "ep001")
        assert diff["summary"]["total_changes"] == 0
        assert diff["changes"] == []
        assert diff["episode_id"] == "ep001"
        assert diff["original_length"] == len(text)
        assert diff["corrected_length"] == len(text)

    def test_replace(self):
        original = "Heute über Bit Coin sprechen"
        corrected = "Heute über Bitcoin sprechen"
        diff = compute_correction_diff(original, corrected, "ep001")
        assert diff["summary"]["total_changes"] >= 1
        # Find the replace change
        replaces = [c for c in diff["changes"] if c["type"] == "replace"]
        assert len(replaces) >= 1
        # The replacement should involve "Bit Coin" -> "Bitcoin"
        found = any("Bitcoin" in c["corrected"] for c in replaces)
        assert found

    def test_insert(self):
        original = "Bitcoin ist Geld"
        corrected = "Bitcoin ist digitales Geld"
        diff = compute_correction_diff(original, corrected, "ep001")
        assert diff["summary"]["total_changes"] >= 1
        # Should have at least one insert or replace
        assert len(diff["changes"]) >= 1

    def test_delete(self):
        original = "Bitcoin ist ist eine Währung"
        corrected = "Bitcoin ist eine Währung"
        diff = compute_correction_diff(original, corrected, "ep001")
        assert diff["summary"]["total_changes"] >= 1
        assert len(diff["changes"]) >= 1

    def test_context_included(self):
        original = "Eins zwei drei Bit Coin fünf sechs sieben"
        corrected = "Eins zwei drei Bitcoin fünf sechs sieben"
        diff = compute_correction_diff(original, corrected, "ep001", context_words=3)
        assert len(diff["changes"]) >= 1
        change = diff["changes"][0]
        assert "context" in change
        assert "..." in change["context"]

    def test_summary_counts(self):
        original = "Bit Coin Blok Chain Sattoshi"
        corrected = "Bitcoin Blockchain Satoshi"
        diff = compute_correction_diff(original, corrected, "ep001")
        total = diff["summary"]["total_changes"]
        by_type = diff["summary"]["by_type"]
        assert total == sum(by_type.values())
        assert total >= 1

    def test_category_is_auto(self):
        original = "Bit Coin"
        corrected = "Bitcoin"
        diff = compute_correction_diff(original, corrected, "ep001")
        for change in diff["changes"]:
            assert change["category"] == "auto"

    def test_position_fields(self):
        original = "Eins zwei Bit Coin fünf"
        corrected = "Eins zwei Bitcoin fünf"
        diff = compute_correction_diff(original, corrected, "ep001")
        for change in diff["changes"]:
            assert "position" in change
            assert "start_word" in change["position"]
            assert "end_word" in change["position"]


# ---------------------------------------------------------------------------
# Unit tests: _segment_transcript
# ---------------------------------------------------------------------------


class TestSegmentTranscript:
    def test_short_text(self):
        text = "Short text."
        segments = _segment_transcript(text, limit=100)
        assert segments == [text]

    def test_long_text_splits_at_paragraphs(self):
        para1 = "A" * 80
        para2 = "B" * 80
        para3 = "C" * 80
        text = f"{para1}\n\n{para2}\n\n{para3}"
        segments = _segment_transcript(text, limit=170)
        assert len(segments) >= 2
        # All text should be preserved
        reassembled = "\n\n".join(segments)
        assert reassembled == text

    def test_no_paragraph_breaks(self):
        text = "A" * 200
        segments = _segment_transcript(text, limit=100)
        assert len(segments) >= 2
        # All text should be preserved
        total_len = sum(len(s) for s in segments)
        assert total_len == len(text)

    def test_exact_limit(self):
        text = "A" * 100
        segments = _segment_transcript(text, limit=100)
        assert segments == [text]


# ---------------------------------------------------------------------------
# Unit tests: _split_prompt
# ---------------------------------------------------------------------------


class TestSplitPrompt:
    def test_splits_at_marker(self):
        body = (
            "System instructions here.\n\n# Transkript\n\n"
            "{{ transcript }}\n\n# Ausgabeformat\n\nPlain text."
        )
        system, user = _split_prompt(body)
        assert "System instructions" in system
        assert "# Transkript" in user
        assert "{{ transcript }}" in user

    def test_no_marker_fallback(self):
        body = "Just a user message with no marker"
        system, user = _split_prompt(body)
        assert system == ""
        assert user == body


# ---------------------------------------------------------------------------
# Unit tests: _is_correction_current
# ---------------------------------------------------------------------------


class TestIsCorrectionCurrent:
    def test_fresh_correction(self, tmp_path):
        corrected = tmp_path / "corrected.txt"
        corrected.write_text("corrected text")
        provenance = tmp_path / "provenance.json"
        provenance.write_text(
            json.dumps({"prompt_hash": "hash123", "input_content_hash": "inputhash456"})
        )
        assert _is_correction_current(corrected, provenance, "inputhash456", "hash123") is True

    def test_missing_corrected_file(self, tmp_path):
        corrected = tmp_path / "corrected.txt"  # doesn't exist
        provenance = tmp_path / "provenance.json"
        provenance.write_text(json.dumps({"prompt_hash": "h", "input_content_hash": "i"}))
        assert _is_correction_current(corrected, provenance, "i", "h") is False

    def test_stale_marker(self, tmp_path):
        corrected = tmp_path / "corrected.txt"
        corrected.write_text("corrected text")
        stale = tmp_path / "corrected.txt.stale"
        stale.write_text("{}")
        provenance = tmp_path / "provenance.json"
        provenance.write_text(json.dumps({"prompt_hash": "h", "input_content_hash": "i"}))
        assert _is_correction_current(corrected, provenance, "i", "h") is False

    def test_prompt_hash_mismatch(self, tmp_path):
        corrected = tmp_path / "corrected.txt"
        corrected.write_text("corrected text")
        provenance = tmp_path / "provenance.json"
        provenance.write_text(
            json.dumps({"prompt_hash": "old_hash", "input_content_hash": "inputhash"})
        )
        assert _is_correction_current(corrected, provenance, "inputhash", "new_hash") is False

    def test_input_hash_mismatch(self, tmp_path):
        corrected = tmp_path / "corrected.txt"
        corrected.write_text("corrected text")
        provenance = tmp_path / "provenance.json"
        provenance.write_text(
            json.dumps({"prompt_hash": "hash", "input_content_hash": "old_input"})
        )
        assert _is_correction_current(corrected, provenance, "new_input", "hash") is False

    def test_missing_provenance(self, tmp_path):
        corrected = tmp_path / "corrected.txt"
        corrected.write_text("corrected text")
        provenance = tmp_path / "provenance.json"  # doesn't exist
        assert _is_correction_current(corrected, provenance, "i", "h") is False


# ---------------------------------------------------------------------------
# Integration tests: correct_transcript
# ---------------------------------------------------------------------------


class TestCorrectTranscript:
    def test_success_dry_run(self, db_session, transcribed_episode, mock_settings):
        """Full integration: dry-run correction creates expected files and DB records."""
        result = correct_transcript(db_session, "ep_test", mock_settings, force=False)
        assert isinstance(result, CorrectionResult)
        assert result.episode_id == "ep_test"
        assert Path(result.corrected_path).exists()
        assert Path(result.diff_path).exists()
        assert Path(result.provenance_path).exists()

        # Episode status updated
        db_session.refresh(transcribed_episode)
        assert transcribed_episode.status == EpisodeStatus.CORRECTED

        # PipelineRun created
        runs = (
            db_session.query(PipelineRun).filter(PipelineRun.stage == PipelineStage.CORRECT).all()
        )
        assert len(runs) == 1
        assert runs[0].status == RunStatus.SUCCESS

        # Diff JSON is valid
        diff_data = json.loads(Path(result.diff_path).read_text(encoding="utf-8"))
        assert "changes" in diff_data
        assert "summary" in diff_data
        assert diff_data["episode_id"] == "ep_test"

        # Provenance JSON is valid
        prov_data = json.loads(Path(result.provenance_path).read_text(encoding="utf-8"))
        assert prov_data["stage"] == "correct"
        assert prov_data["episode_id"] == "ep_test"
        assert "prompt_hash" in prov_data
        assert "input_content_hash" in prov_data

    def test_wrong_status(self, db_session, mock_settings):
        """Raises ValueError when episode is not TRANSCRIBED."""
        episode = Episode(
            episode_id="ep_new",
            source="youtube_rss",
            title="Test",
            url="https://youtube.com/watch?v=ep_new",
            status=EpisodeStatus.NEW,
        )
        db_session.add(episode)
        db_session.commit()

        with pytest.raises(ValueError, match="expected 'transcribed'"):
            correct_transcript(db_session, "ep_new", mock_settings)

    def test_not_found(self, db_session, mock_settings):
        """Raises ValueError when episode doesn't exist."""
        with pytest.raises(ValueError, match="Episode not found"):
            correct_transcript(db_session, "nonexistent", mock_settings)

    def test_idempotent(self, db_session, transcribed_episode, mock_settings):
        """Second call without force skips (returns same result, zero cost)."""
        result1 = correct_transcript(db_session, "ep_test", mock_settings)
        assert result1.corrected_path

        # Reset episode status so the function accepts it
        # (it's now CORRECTED, which is also accepted)
        result2 = correct_transcript(db_session, "ep_test", mock_settings)
        assert result2.cost_usd == 0.0
        assert result2.input_tokens == 0
        assert result2.output_tokens == 0
        assert result2.change_count == result1.change_count

    def test_force_reruns(self, db_session, transcribed_episode, mock_settings):
        """With force=True, re-runs even if output exists."""
        correct_transcript(db_session, "ep_test", mock_settings)

        correct_transcript(db_session, "ep_test", mock_settings, force=True)
        # Force run should create a new PipelineRun
        runs = (
            db_session.query(PipelineRun).filter(PipelineRun.stage == PipelineStage.CORRECT).all()
        )
        assert len(runs) == 2


# ---------------------------------------------------------------------------
# Reviewer feedback injection
# ---------------------------------------------------------------------------


class TestReviewerFeedbackInjection:
    def test_feedback_injected_into_prompt(self, db_session, transcribed_episode, mock_settings):
        """When reviewer feedback exists, it replaces {{ reviewer_feedback }} in the prompt."""
        from btcedu.core.reviewer import create_review_task, request_changes

        # First correction — produces output files
        result1 = correct_transcript(db_session, "ep_test", mock_settings)
        assert Path(result1.corrected_path).exists()

        # Create and request changes on a review task
        task = create_review_task(
            db_session,
            episode_id="ep_test",
            stage="correct",
            artifact_paths=[result1.corrected_path],
            diff_path=result1.diff_path,
        )
        request_changes(db_session, task.id, notes="Fix Bitcoin spelling")

        # Episode is now TRANSCRIBED again; re-correct should inject feedback
        db_session.refresh(transcribed_episode)
        assert transcribed_episode.status == EpisodeStatus.TRANSCRIBED

        # Patch call_claude to capture the system prompt
        captured_prompts = []
        original_call = None

        from btcedu.services import claude_service

        original_call = claude_service.call_claude

        def spy_call_claude(system_prompt, user_message, **kwargs):
            captured_prompts.append(system_prompt)
            return original_call(system_prompt=system_prompt, user_message=user_message, **kwargs)

        with patch("btcedu.core.corrector.call_claude", side_effect=spy_call_claude):
            correct_transcript(db_session, "ep_test", mock_settings, force=True)

        # The feedback should appear in the system prompt
        assert len(captured_prompts) >= 1
        system_prompt = captured_prompts[0]
        assert "Fix Bitcoin spelling" in system_prompt
        assert "Reviewer-Korrekturen" in system_prompt

    def test_no_feedback_placeholder_removed(self, db_session, transcribed_episode, mock_settings):
        """When no feedback exists, {{ reviewer_feedback }} is replaced with empty string."""
        captured_prompts = []
        original_call = None

        from btcedu.services import claude_service

        original_call = claude_service.call_claude

        def spy_call_claude(system_prompt, user_message, **kwargs):
            captured_prompts.append(system_prompt)
            return original_call(system_prompt=system_prompt, user_message=user_message, **kwargs)

        with patch("btcedu.core.corrector.call_claude", side_effect=spy_call_claude):
            correct_transcript(db_session, "ep_test", mock_settings)

        assert len(captured_prompts) >= 1
        system_prompt = captured_prompts[0]
        # Placeholder should not appear in the rendered prompt
        assert "{{ reviewer_feedback }}" not in system_prompt
        # No feedback block should appear
        assert "Reviewer-Korrekturen" not in system_prompt

    def test_stale_marker_triggers_rerun(self, db_session, transcribed_episode, mock_settings):
        """A .stale marker on the corrected file forces re-correction."""
        result1 = correct_transcript(db_session, "ep_test", mock_settings)
        corrected_path = Path(result1.corrected_path)
        assert corrected_path.exists()

        # Idempotent: second run without force returns cached (zero cost)
        result2 = correct_transcript(db_session, "ep_test", mock_settings)
        assert result2.cost_usd == 0.0

        # Create stale marker
        stale_marker = corrected_path.parent / (corrected_path.name + ".stale")
        stale_marker.write_text('{"reason": "changes_requested"}')

        # Now re-correction should run (non-zero pipeline run created)
        runs_before = (
            db_session.query(PipelineRun).filter(PipelineRun.stage == PipelineStage.CORRECT).count()
        )
        correct_transcript(db_session, "ep_test", mock_settings)
        runs_after = (
            db_session.query(PipelineRun).filter(PipelineRun.stage == PipelineStage.CORRECT).count()
        )
        assert runs_after == runs_before + 1

    def test_marks_downstream_translation_stale(
        self, db_session, transcribed_episode, mock_settings
    ):
        """Re-correcting a transcript marks the translation as stale."""
        # Create a fake translation file that should be invalidated
        tr_path = Path(mock_settings.transcripts_dir) / "ep_test" / "transcript.tr.txt"
        tr_path.parent.mkdir(parents=True, exist_ok=True)
        tr_path.write_text("Sahte çeviri", encoding="utf-8")

        # Run correction
        correct_transcript(db_session, "ep_test", mock_settings)

        # The translation's stale marker should now exist
        stale_marker = tr_path.parent / (tr_path.name + ".stale")
        assert stale_marker.exists(), "Correction should mark downstream translation as stale"

        stale_data = json.loads(stale_marker.read_text(encoding="utf-8"))
        assert stale_data["invalidated_by"] == "correct"
        assert stale_data["reason"] == "correction_changed"

    def test_no_stale_marker_when_no_translation(
        self, db_session, transcribed_episode, mock_settings
    ):
        """If no translation exists yet, no stale marker is created."""
        correct_transcript(db_session, "ep_test", mock_settings)

        tr_stale = (
            Path(mock_settings.transcripts_dir)
            / "ep_test"
            / "transcript.tr.txt.stale"
        )
        assert not tr_stale.exists()


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------


class TestCorrectCLI:
    def test_help(self):
        from btcedu.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["correct", "--help"])
        assert result.exit_code == 0
        assert "Correct Whisper transcripts" in result.output
        assert "--episode-id" in result.output
        assert "--force" in result.output
