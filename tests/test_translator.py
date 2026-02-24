"""Tests for the translation module (Sprint 4)."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from btcedu.core.translator import (
    _is_translation_current,
    _segment_text,
    _split_prompt,
    translate_transcript,
)
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus
from btcedu.models.review import ReviewStatus, ReviewTask

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def corrected_episode(db_session, tmp_path):
    """Episode at CORRECTED status with a corrected transcript file and approved Review Gate 1."""
    transcript_dir = tmp_path / "transcripts" / "ep_test"
    transcript_dir.mkdir(parents=True)
    corrected_path = transcript_dir / "transcript.corrected.de.txt"
    corrected_path.write_text(
        "Heute sprechen wir über Bitcoin und die Blockchain-Technologie.\n\n"
        "Es ist eine dezentrale Währung, die von Satoshi Nakamoto erfunden wurde.",
        encoding="utf-8",
    )

    episode = Episode(
        episode_id="ep_test",
        source="youtube_rss",
        title="Bitcoin Grundlagen",
        url="https://youtube.com/watch?v=ep_test",
        status=EpisodeStatus.CORRECTED,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    # Add approved ReviewTask for Review Gate 1 (correction stage)
    # This is required for translation to proceed per MASTERPLAN §3.1
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
def corrected_episode_no_approval(db_session, tmp_path):
    """Episode at CORRECTED status without Review Gate 1 approval (for testing approval checks)."""
    transcript_dir = tmp_path / "transcripts" / "ep_test"
    transcript_dir.mkdir(parents=True)
    corrected_path = transcript_dir / "transcript.corrected.de.txt"
    corrected_path.write_text(
        "Heute sprechen wir über Bitcoin und die Blockchain-Technologie.\n\n"
        "Es ist eine dezentrale Währung, die von Satoshi Nakamoto erfunden wurde.",
        encoding="utf-8",
    )

    episode = Episode(
        episode_id="ep_test",
        source="youtube_rss",
        title="Bitcoin Grundlagen",
        url="https://youtube.com/watch?v=ep_test",
        status=EpisodeStatus.CORRECTED,
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
# Unit tests: _segment_text
# ---------------------------------------------------------------------------


class TestSegmentText:
    def test_short_text(self):
        text = "Short text."
        segments = _segment_text(text, limit=15_000)
        assert len(segments) == 1
        assert segments[0] == text

    def test_text_at_limit(self):
        text = "a" * 15_000
        segments = _segment_text(text, limit=15_000)
        assert len(segments) == 1

    def test_text_just_over_limit(self):
        text = "a" * 15_001
        segments = _segment_text(text, limit=15_000)
        # Should split into multiple segments
        assert len(segments) > 1
        for seg in segments:
            assert len(seg) <= 15_000 + 100  # Allow some tolerance for splitting

    def test_paragraph_splitting(self):
        para1 = "A" * 5000
        para2 = "B" * 5000
        para3 = "C" * 5000
        text = f"{para1}\n\n{para2}\n\n{para3}"
        segments = _segment_text(text, limit=12_000)
        # Should split at paragraph boundaries
        assert len(segments) >= 2

    def test_long_single_paragraph(self):
        # Single paragraph exceeding limit - should split at sentences
        text = ("Sentence one. " * 1000) + "Sentence two. " * 100
        segments = _segment_text(text, limit=5000)
        assert len(segments) > 1
        for seg in segments:
            assert len(seg) <= 5000 + 100

    def test_empty_text(self):
        segments = _segment_text("", limit=15_000)
        assert len(segments) == 1
        assert segments[0] == ""

    def test_only_whitespace(self):
        text = "\n\n\n"
        segments = _segment_text(text, limit=15_000)
        # Should handle gracefully
        assert len(segments) >= 1


# ---------------------------------------------------------------------------
# Unit tests: _split_prompt
# ---------------------------------------------------------------------------


class TestSplitPrompt:
    def test_split_at_input_marker(self):
        template = (
            "System instructions here\n\n# Input\n\n{{ transcript }}\n\n"
            "# Output Format\n\nReturn Turkish."
        )
        system, user = _split_prompt(template)
        assert "System instructions" in system
        assert "# Input" in user
        assert "{{ transcript }}" in user

    def test_no_marker_fallback(self):
        template = "All content without marker"
        system, user = _split_prompt(template)
        # Fallback: empty system, entire body is user
        assert system == ""
        assert user == template

    def test_marker_at_start(self):
        template = "# Input\n\nImmediate content"
        system, user = _split_prompt(template)
        assert system == ""
        assert "# Input" in user


# ---------------------------------------------------------------------------
# Unit tests: _is_translation_current
# ---------------------------------------------------------------------------


class TestIsTranslationCurrent:
    def test_missing_output_file(self, tmp_path):
        translated_path = tmp_path / "transcript.tr.txt"
        provenance_path = tmp_path / "translate_provenance.json"
        # File doesn't exist
        assert not _is_translation_current(
            translated_path, provenance_path, "input_hash", "prompt_hash"
        )

    def test_stale_marker_exists(self, tmp_path):
        translated_path = tmp_path / "transcript.tr.txt"
        translated_path.write_text("Turkish text", encoding="utf-8")
        stale_marker = tmp_path / "transcript.tr.txt.stale"
        stale_marker.write_text(json.dumps({"invalidated_by": "correct"}), encoding="utf-8")
        provenance_path = tmp_path / "translate_provenance.json"

        # Should return False due to stale marker
        assert not _is_translation_current(
            translated_path, provenance_path, "input_hash", "prompt_hash"
        )
        # Marker should be removed after detection
        assert not stale_marker.exists()

    def test_missing_provenance(self, tmp_path):
        translated_path = tmp_path / "transcript.tr.txt"
        translated_path.write_text("Turkish text", encoding="utf-8")
        provenance_path = tmp_path / "translate_provenance.json"
        # Provenance doesn't exist
        assert not _is_translation_current(
            translated_path, provenance_path, "input_hash", "prompt_hash"
        )

    def test_prompt_hash_mismatch(self, tmp_path):
        translated_path = tmp_path / "transcript.tr.txt"
        translated_path.write_text("Turkish text", encoding="utf-8")
        provenance_path = tmp_path / "translate_provenance.json"
        provenance_path.write_text(
            json.dumps(
                {
                    "prompt_hash": "old_prompt_hash",
                    "input_content_hash": "input_hash",
                }
            ),
            encoding="utf-8",
        )

        # Hash mismatch -> not current
        assert not _is_translation_current(
            translated_path, provenance_path, "input_hash", "new_prompt_hash"
        )

    def test_input_hash_mismatch(self, tmp_path):
        translated_path = tmp_path / "transcript.tr.txt"
        translated_path.write_text("Turkish text", encoding="utf-8")
        provenance_path = tmp_path / "translate_provenance.json"
        provenance_path.write_text(
            json.dumps(
                {
                    "prompt_hash": "prompt_hash",
                    "input_content_hash": "old_input_hash",
                }
            ),
            encoding="utf-8",
        )

        # Hash mismatch -> not current
        assert not _is_translation_current(
            translated_path, provenance_path, "new_input_hash", "prompt_hash"
        )

    def test_all_checks_pass(self, tmp_path):
        translated_path = tmp_path / "transcript.tr.txt"
        translated_path.write_text("Turkish text", encoding="utf-8")
        provenance_path = tmp_path / "translate_provenance.json"
        provenance_path.write_text(
            json.dumps(
                {
                    "prompt_hash": "prompt_hash",
                    "input_content_hash": "input_hash",
                }
            ),
            encoding="utf-8",
        )

        # All checks pass -> current
        assert _is_translation_current(
            translated_path, provenance_path, "input_hash", "prompt_hash"
        )


# ---------------------------------------------------------------------------
# Integration tests: translate_transcript
# ---------------------------------------------------------------------------


class TestTranslateTranscript:
    def test_translate_creates_output(self, db_session, corrected_episode, mock_settings, tmp_path):
        """Test that translation creates the Turkish output file and provenance."""
        # Mock Claude API call to return Turkish translation
        with patch("btcedu.core.translator.call_claude") as mock_claude:
            mock_claude.return_value = type(
                "Response",
                (),
                {
                    "text": (
                        "Bugün Bitcoin ve Blockchain teknolojisi hakkında konuşuyoruz.\n\n"
                        "Bu, Satoshi Nakamoto tarafından icat edilen merkezi olmayan "
                        "bir para birimidir."
                    ),
                    "input_tokens": 100,
                    "output_tokens": 120,
                    "cost_usd": 0.005,
                },
            )

            result = translate_transcript(db_session, "ep_test", mock_settings, force=False)

            assert not result.skipped
            assert result.episode_id == "ep_test"
            assert result.input_tokens == 100
            assert result.output_tokens == 120
            assert result.cost_usd == 0.005
            assert result.input_char_count > 0
            assert result.output_char_count > 0

            # Check files were created
            translated_path = Path(result.translated_path)
            provenance_path = Path(result.provenance_path)
            assert translated_path.exists()
            assert provenance_path.exists()

            # Check content
            translated_text = translated_path.read_text(encoding="utf-8")
            assert "Bitcoin" in translated_text
            assert "Blockchain" in translated_text

            # Check provenance
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            assert provenance["stage"] == "translate"
            assert provenance["episode_id"] == "ep_test"
            assert "prompt_hash" in provenance
            assert "input_content_hash" in provenance

    def test_translate_idempotent_skip(
        self, db_session, corrected_episode, mock_settings, tmp_path
    ):
        """Test that running translate twice skips on second run."""
        with patch("btcedu.core.translator.call_claude") as mock_claude:
            mock_claude.return_value = type(
                "Response",
                (),
                {
                    "text": "Turkish translation here.",
                    "input_tokens": 50,
                    "output_tokens": 60,
                    "cost_usd": 0.002,
                },
            )

            # First run
            result1 = translate_transcript(db_session, "ep_test", mock_settings, force=False)
            assert not result1.skipped
            assert mock_claude.call_count == 1

            # Second run (should skip)
            result2 = translate_transcript(db_session, "ep_test", mock_settings, force=False)
            assert result2.skipped
            assert mock_claude.call_count == 1  # Not called again

    def test_translate_force_reprocesses(
        self, db_session, corrected_episode, mock_settings, tmp_path
    ):
        """Test that --force flag re-translates even if output exists."""
        with patch("btcedu.core.translator.call_claude") as mock_claude:
            mock_claude.return_value = type(
                "Response",
                (),
                {
                    "text": "Turkish translation.",
                    "input_tokens": 50,
                    "output_tokens": 60,
                    "cost_usd": 0.002,
                },
            )

            # First run
            result1 = translate_transcript(db_session, "ep_test", mock_settings, force=False)
            assert not result1.skipped

            # Force re-run
            result2 = translate_transcript(db_session, "ep_test", mock_settings, force=True)
            assert not result2.skipped
            assert mock_claude.call_count == 2  # Called again

    def test_translate_updates_episode_status(self, db_session, corrected_episode, mock_settings):
        """Test that episode status transitions to TRANSLATED."""
        with patch("btcedu.core.translator.call_claude") as mock_claude:
            mock_claude.return_value = type(
                "Response",
                (),
                {
                    "text": "Turkish text.",
                    "input_tokens": 50,
                    "output_tokens": 60,
                    "cost_usd": 0.002,
                },
            )

            assert corrected_episode.status == EpisodeStatus.CORRECTED

            translate_transcript(db_session, "ep_test", mock_settings, force=False)

            db_session.refresh(corrected_episode)
            assert corrected_episode.status == EpisodeStatus.TRANSLATED
            assert corrected_episode.error_message is None

    def test_translate_creates_pipeline_run(self, db_session, corrected_episode, mock_settings):
        """Test that a PipelineRun record is created."""
        with patch("btcedu.core.translator.call_claude") as mock_claude:
            mock_claude.return_value = type(
                "Response",
                (),
                {
                    "text": "Turkish text.",
                    "input_tokens": 50,
                    "output_tokens": 60,
                    "cost_usd": 0.002,
                },
            )

            translate_transcript(db_session, "ep_test", mock_settings, force=False)

            runs = db_session.query(PipelineRun).filter_by(episode_id=corrected_episode.id).all()
            translate_runs = [r for r in runs if r.stage == PipelineStage.TRANSLATE]
            assert len(translate_runs) == 1
            assert translate_runs[0].status == RunStatus.SUCCESS
            assert translate_runs[0].input_tokens == 50
            assert translate_runs[0].output_tokens == 60
            assert translate_runs[0].estimated_cost_usd == 0.002

    def test_translate_wrong_status_fails(self, db_session, mock_settings):
        """Test that translation fails if episode is not CORRECTED."""
        # Create episode with wrong status
        episode = Episode(
            episode_id="ep_wrong",
            source="youtube_rss",
            title="Test",
            url="https://test.com",
            status=EpisodeStatus.TRANSCRIBED,  # Wrong status
            pipeline_version=2,
        )
        db_session.add(episode)
        db_session.commit()

        with pytest.raises(ValueError, match="expected 'corrected'"):
            translate_transcript(db_session, "ep_wrong", mock_settings, force=False)

    def test_translate_fails_without_review_approval(
        self, db_session, corrected_episode_no_approval, mock_settings
    ):
        """Test that translation fails if Review Gate 1 is not approved."""
        # Episode is CORRECTED but no approved ReviewTask exists
        with pytest.raises(ValueError, match="correction has not been approved"):
            translate_transcript(db_session, "ep_test", mock_settings, force=False)

    def test_translate_fails_with_pending_review(
        self, db_session, corrected_episode_no_approval, mock_settings
    ):
        """Test that translation fails if Review Gate 1 is still pending."""
        # Create pending ReviewTask
        review_task = ReviewTask(
            episode_id="ep_test",
            stage="correct",
            status=ReviewStatus.PENDING.value,
            artifact_paths="[]",
        )
        db_session.add(review_task)
        db_session.commit()

        with pytest.raises(ValueError, match="has pending review"):
            translate_transcript(db_session, "ep_test", mock_settings, force=False)

    def test_translate_succeeds_with_review_approval(
        self, db_session, corrected_episode_no_approval, mock_settings
    ):
        """Test that translation succeeds when Review Gate 1 is approved."""
        # Create approved ReviewTask
        review_task = ReviewTask(
            episode_id="ep_test",
            stage="correct",
            status=ReviewStatus.APPROVED.value,
            artifact_paths="[]",
        )
        db_session.add(review_task)
        db_session.commit()

        # Now translation should succeed
        with patch("btcedu.core.translator.call_claude") as mock_claude:
            mock_claude.return_value = type(
                "Response",
                (),
                {
                    "text": "Turkish translation here",
                    "input_tokens": 100,
                    "output_tokens": 120,
                    "cost_usd": 0.01,
                },
            )

            result = translate_transcript(db_session, "ep_test", mock_settings, force=False)
            assert not result.skipped
            assert result.cost_usd > 0

    def test_translate_force_bypasses_approval_check(
        self, db_session, corrected_episode_no_approval, mock_settings
    ):
        """Test that --force flag bypasses Review Gate 1 approval check."""
        # No approved ReviewTask exists, but force=True should bypass check
        with patch("btcedu.core.translator.call_claude") as mock_claude:
            mock_claude.return_value = type(
                "Response",
                (),
                {
                    "text": "Forced Turkish translation",
                    "input_tokens": 50,
                    "output_tokens": 60,
                    "cost_usd": 0.005,
                },
            )

            # force=True should bypass approval check
            result = translate_transcript(db_session, "ep_test", mock_settings, force=True)
            assert not result.skipped
            assert result.cost_usd > 0

    def test_translate_missing_corrected_file_fails(self, db_session, mock_settings):
        """Test that translation fails if corrected transcript file is missing."""
        episode = Episode(
            episode_id="ep_missing",
            source="youtube_rss",
            title="Test",
            url="https://test.com",
            status=EpisodeStatus.CORRECTED,
            pipeline_version=2,
        )
        db_session.add(episode)
        db_session.commit()

        # Add approved ReviewTask so we can test file-not-found error (not approval error)
        review_task = ReviewTask(
            episode_id="ep_missing",
            stage="correct",
            status=ReviewStatus.APPROVED.value,
            artifact_paths="[]",
        )
        db_session.add(review_task)
        db_session.commit()

        with pytest.raises(FileNotFoundError):
            translate_transcript(db_session, "ep_missing", mock_settings, force=False)

    def test_translate_segmentation_for_long_text(
        self, db_session, corrected_episode, mock_settings, tmp_path
    ):
        """Test that long transcripts are segmented and reassembled."""
        # Create a very long corrected transcript
        transcript_dir = tmp_path / "transcripts" / "ep_test"
        corrected_path = transcript_dir / "transcript.corrected.de.txt"
        long_text = (
            ("Dies ist ein sehr langer Absatz. " * 1000) + "\n\n" + ("Zweiter Absatz. " * 1000)
        )
        corrected_path.write_text(long_text, encoding="utf-8")

        with patch("btcedu.core.translator.call_claude") as mock_claude:
            mock_claude.return_value = type(
                "Response",
                (),
                {
                    "text": "Turkish segment.",
                    "input_tokens": 1000,
                    "output_tokens": 1100,
                    "cost_usd": 0.010,
                },
            )

            result = translate_transcript(db_session, "ep_test", mock_settings, force=False)

            # Should have called Claude multiple times (once per segment)
            assert mock_claude.call_count > 1
            assert result.segments_processed > 1

            # Check that segments were reassembled with paragraph breaks
            translated_path = Path(result.translated_path)
            translated_text = translated_path.read_text(encoding="utf-8")
            assert "\n\n" in translated_text  # Paragraphs preserved


# ---------------------------------------------------------------------------
# CLI tests
# ---------------------------------------------------------------------------


class TestTranslateCLI:
    def test_cli_help(self):
        """Test that translate CLI command has proper help message."""
        from btcedu.cli import cli

        runner = CliRunner()
        result = runner.invoke(cli, ["translate", "--help"])
        assert result.exit_code == 0
        assert "Translate corrected German transcripts to Turkish" in result.output
        assert "--force" in result.output
        assert "--dry-run" in result.output

    def test_cli_translate_success(self, db_session, corrected_episode, mock_settings, tmp_path):
        """Test successful translation via CLI."""
        from btcedu.cli import cli

        with (
            patch("btcedu.core.translator.call_claude") as mock_claude,
            patch("btcedu.cli.get_settings") as mock_get_settings,
            patch("btcedu.cli.init_db"),
            patch("btcedu.cli.get_session_factory") as mock_get_session_factory,
        ):
            mock_claude.return_value = type(
                "Response",
                (),
                {
                    "text": "Turkish text.",
                    "input_tokens": 50,
                    "output_tokens": 60,
                    "cost_usd": 0.002,
                },
            )
            mock_get_settings.return_value = mock_settings
            mock_get_session_factory.return_value = lambda: db_session

            runner = CliRunner()
            result = runner.invoke(cli, ["translate", "--episode-id", "ep_test"])

            assert result.exit_code == 0
            assert "[OK] ep_test" in result.output
            assert "$" in result.output  # Cost displayed
            assert "chars" in result.output

    def test_cli_translate_with_dry_run(self, db_session, corrected_episode, mock_settings):
        """Test dry-run mode via CLI."""
        from btcedu.cli import cli

        with (
            patch("btcedu.cli.get_settings") as mock_get_settings,
            patch("btcedu.cli.init_db"),
            patch("btcedu.cli.get_session_factory") as mock_get_session_factory,
        ):
            mock_get_settings.return_value = mock_settings
            mock_get_session_factory.return_value = lambda: db_session

            runner = CliRunner()
            # Note: dry-run won't actually call API if settings.dry_run=True in mock_settings
            result = runner.invoke(cli, ["translate", "--episode-id", "ep_test", "--dry-run"])

            # Should complete without error
            assert result.exit_code == 0
