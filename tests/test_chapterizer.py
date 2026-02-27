"""Tests for the chapterizer module (Sprint 6)."""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.core.chapterizer import (
    ChapterizationResult,
    _compute_duration_estimate,
    _is_chapterization_current,
    _segment_script,
    _split_prompt,
    chapterize_script,
)
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.review import ReviewStatus, ReviewTask

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def adapted_episode(db_session, tmp_path):
    """Episode at ADAPTED status with adapted script file.

    Also includes approved Review Gate 2.
    """
    outputs_dir = tmp_path / "outputs" / "ep_test"
    outputs_dir.mkdir(parents=True)

    # Adapted Turkish script
    adapted_path = outputs_dir / "script.adapted.tr.md"
    adapted_path.write_text(
        "# Bitcoin Nedir?\n\n"
        "Merhaba arkadaşlar, bugün Bitcoin ve blockchain teknolojisi hakkında konuşacağız.\n\n"
        "Bitcoin, merkezi olmayan bir dijital para birimidir. "
        "2008 yılında Satoshi Nakamoto tarafından yaratıldı.\n\n"
        "Blockchain teknolojisi, Bitcoin'in temelini oluşturur. "
        "Bu teknoloji sayesinde işlemler güvenli bir şekilde kaydedilir.",
        encoding="utf-8",
    )

    episode = Episode(
        episode_id="ep_test",
        source="youtube_rss",
        title="Bitcoin Temelleri",
        url="https://youtube.com/watch?v=ep_test",
        status=EpisodeStatus.ADAPTED,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    # Add approved ReviewTask for Review Gate 2 (adaptation stage)
    review_task = ReviewTask(
        episode_id="ep_test",
        stage="adapt",
        status=ReviewStatus.APPROVED.value,
        artifact_paths="[]",
    )
    db_session.add(review_task)
    db_session.commit()

    return episode


# ---------------------------------------------------------------------------
# Helper Function Tests
# ---------------------------------------------------------------------------


def test_compute_duration_estimate():
    """Test duration estimate calculation (150 words/min for Turkish)."""
    # 150 words = 60 seconds
    assert _compute_duration_estimate(150) == 60

    # 75 words = 30 seconds
    assert _compute_duration_estimate(75) == 30

    # 300 words = 120 seconds
    assert _compute_duration_estimate(300) == 120

    # 1 word = 0.4 seconds, rounds to 0
    assert _compute_duration_estimate(1) == 0


def test_split_prompt():
    """Test prompt splitting at '# Input' marker."""
    template = """# System

You are an editor.

# Instructions

Do X and Y.

# Input

Here is the data.
"""
    system, user = _split_prompt(template)
    assert "# System" in system
    assert "# Instructions" in system
    assert "# Input" not in system
    assert "# Input" in user
    assert "Here is the data." in user


def test_split_prompt_no_marker():
    """Test prompt splitting when no '# Input' marker exists."""
    template = "This is a simple prompt without Input section."
    system, user = _split_prompt(template)
    assert system == ""
    assert user == template


def test_segment_script_short():
    """Test script segmentation: short script returns single segment."""
    script = "This is a short script."
    segments = _segment_script(script, limit=100)
    assert len(segments) == 1
    assert segments[0] == script


def test_segment_script_long():
    """Test script segmentation: long script splits at paragraphs."""
    # Create script longer than limit
    paragraphs = [f"Paragraph {i}. " * 50 for i in range(10)]
    script = "\n\n".join(paragraphs)

    segments = _segment_script(script, limit=1000)
    assert len(segments) > 1

    # Check that segments are within limit (with some tolerance for paragraph boundaries)
    for seg in segments:
        assert len(seg) <= 1500  # Allow some overflow for paragraph boundaries


def test_is_chapterization_current_no_output():
    """Test idempotency check: returns False if output doesn't exist."""
    chapters_path = Path("/tmp/nonexistent/chapters.json")
    provenance_path = Path("/tmp/nonexistent/provenance.json")
    result = _is_chapterization_current(chapters_path, provenance_path, "hash1", "hash2")
    assert result is False


def test_is_chapterization_current_stale_marker(tmp_path):
    """Test idempotency check: returns False if .stale marker exists."""
    chapters_path = tmp_path / "chapters.json"
    chapters_path.write_text("{}", encoding="utf-8")

    stale_marker = tmp_path / "chapters.json.stale"
    stale_marker.write_text('{"reason": "test"}', encoding="utf-8")

    provenance_path = tmp_path / "provenance.json"
    provenance_path.write_text(
        '{"prompt_hash": "hash1", "input_content_hash": "hash2"}', encoding="utf-8"
    )

    result = _is_chapterization_current(chapters_path, provenance_path, "hash2", "hash1")
    assert result is False
    assert not stale_marker.exists()  # Marker should be consumed


def test_is_chapterization_current_valid(tmp_path):
    """Test idempotency check: returns True if output is current."""
    chapters_path = tmp_path / "chapters.json"
    chapters_path.write_text("{}", encoding="utf-8")

    provenance_path = tmp_path / "provenance.json"
    provenance = {
        "prompt_hash": "hash1",
        "input_content_hash": "hash2",
    }
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

    result = _is_chapterization_current(chapters_path, provenance_path, "hash2", "hash1")
    assert result is True


# ---------------------------------------------------------------------------
# Integration Tests (with mocked Claude API)
# ---------------------------------------------------------------------------


@patch("btcedu.core.chapterizer.call_claude")
@patch("btcedu.core.chapterizer.PromptRegistry")
def test_chapterize_script_success(
    mock_registry, mock_call_claude, adapted_episode, db_session, tmp_path
):
    """Test successful chapterization."""
    # Mock settings
    settings = MagicMock()
    settings.outputs_dir = str(tmp_path / "outputs")
    settings.transcripts_dir = str(tmp_path / "transcripts")
    settings.claude_model = "claude-sonnet-4-20250514"
    settings.claude_temperature = 0.3
    settings.claude_max_tokens = 8192
    settings.dry_run = False

    # Create adapted script
    adapted_path = Path(settings.outputs_dir) / "ep_test" / "script.adapted.tr.md"
    adapted_path.parent.mkdir(parents=True)
    adapted_path.write_text(
        "# Bitcoin Nedir?\n\nMerhaba arkadaşlar, bugün Bitcoin hakkında konuşacağız. " * 50,
        encoding="utf-8",
    )

    # Mock prompt registry
    mock_version = MagicMock()
    mock_version.version = 1
    mock_registry.return_value.register_version.return_value = mock_version
    mock_registry.return_value.load_template.return_value = (
        "",
        "# System\n\n# Input\n\n{{episode_id}}\n{{adapted_script}}",
    )
    mock_registry.return_value.compute_hash.return_value = "prompt_hash_123"

    # Mock Claude API response
    mock_response = MagicMock()
    mock_response.text = json.dumps(
        {
            "schema_version": "1.0",
            "episode_id": "ep_test",
            "title": "Bitcoin Nedir?",
            "total_chapters": 2,
            "estimated_duration_seconds": 120,
            "chapters": [
                {
                    "chapter_id": "ch01",
                    "title": "Giriş",
                    "order": 1,
                    "narration": {
                        "text": "Merhaba arkadaşlar.",
                        "word_count": 2,
                        "estimated_duration_seconds": 60,
                    },
                    "visual": {
                        "type": "title_card",
                        "description": "Channel logo",
                        "image_prompt": None,
                    },
                    "overlays": [],
                    "transitions": {"in": "fade", "out": "cut"},
                },
                {
                    "chapter_id": "ch02",
                    "title": "İçerik",
                    "order": 2,
                    "narration": {
                        "text": "Bitcoin nedir?",
                        "word_count": 2,
                        "estimated_duration_seconds": 60,
                    },
                    "visual": {
                        "type": "diagram",
                        "description": "Bitcoin diagram",
                        "image_prompt": "Clean Bitcoin diagram",
                    },
                    "overlays": [],
                    "transitions": {"in": "cut", "out": "fade"},
                },
            ],
        }
    )
    mock_response.input_tokens = 1000
    mock_response.output_tokens = 500
    mock_response.cost_usd = 0.05
    mock_call_claude.return_value = mock_response

    # Run chapterization
    result = chapterize_script(db_session, "ep_test", settings, force=False)

    # Assertions
    assert isinstance(result, ChapterizationResult)
    assert result.episode_id == "ep_test"
    assert result.chapter_count == 2
    assert result.estimated_duration_seconds == 120
    assert result.input_tokens == 1000
    assert result.output_tokens == 500
    assert result.cost_usd == 0.05
    assert not result.skipped

    # Check files were created
    chapters_path = Path(settings.outputs_dir) / "ep_test" / "chapters.json"
    assert chapters_path.exists()

    provenance_path = (
        Path(settings.outputs_dir) / "ep_test" / "provenance" / "chapterize_provenance.json"
    )
    assert provenance_path.exists()

    # Check episode status updated
    episode = db_session.query(Episode).filter(Episode.episode_id == "ep_test").first()
    assert episode.status == EpisodeStatus.CHAPTERIZED


@patch("btcedu.core.chapterizer.call_claude")
@patch("btcedu.core.chapterizer.PromptRegistry")
def test_chapterize_script_idempotency(
    mock_registry, mock_call_claude, adapted_episode, db_session, tmp_path
):
    """Test chapterization idempotency: second run skips if output is current."""
    settings = MagicMock()
    settings.outputs_dir = str(tmp_path / "outputs")
    settings.transcripts_dir = str(tmp_path / "transcripts")
    settings.claude_model = "claude-sonnet-4-20250514"
    settings.claude_temperature = 0.3
    settings.claude_max_tokens = 8192
    settings.dry_run = False

    # Create adapted script
    adapted_path = Path(settings.outputs_dir) / "ep_test" / "script.adapted.tr.md"
    adapted_path.parent.mkdir(parents=True)
    adapted_text = "# Bitcoin Nedir?\n\nTest script."
    adapted_path.write_text(adapted_text, encoding="utf-8")

    # Mock prompt registry
    mock_version = MagicMock()
    mock_version.version = 1
    mock_registry.return_value.register_version.return_value = mock_version
    mock_registry.return_value.load_template.return_value = (
        "",
        "# System\n\n# Input\n\n{{episode_id}}",
    )
    prompt_hash = "prompt_hash_123"
    mock_registry.return_value.compute_hash.return_value = prompt_hash

    # Pre-create chapters.json and provenance with matching hashes
    chapters_path = Path(settings.outputs_dir) / "ep_test" / "chapters.json"
    chapters_path.parent.mkdir(parents=True, exist_ok=True)
    chapters_path.write_text('{"schema_version": "1.0"}', encoding="utf-8")

    adapted_hash = hashlib.sha256(adapted_text.encode("utf-8")).hexdigest()

    provenance_path = (
        Path(settings.outputs_dir) / "ep_test" / "provenance" / "chapterize_provenance.json"
    )
    provenance_path.parent.mkdir(parents=True, exist_ok=True)
    provenance = {
        "prompt_hash": prompt_hash,
        "input_content_hash": adapted_hash,
        "chapter_count": 2,
        "estimated_duration_seconds": 120,
        "input_tokens": 1000,
        "output_tokens": 500,
        "cost_usd": 0.05,
        "segments_processed": 1,
    }
    provenance_path.write_text(json.dumps(provenance), encoding="utf-8")

    # Run chapterization (should skip)
    result = chapterize_script(db_session, "ep_test", settings, force=False)

    # Assertions
    assert result.skipped is True
    assert result.chapter_count == 2
    assert mock_call_claude.call_count == 0  # Should not call Claude API


@patch("btcedu.core.chapterizer.call_claude")
@patch("btcedu.core.chapterizer.PromptRegistry")
def test_chapterize_script_force(
    mock_registry, mock_call_claude, adapted_episode, db_session, tmp_path
):
    """Test chapterization with force=True always re-runs."""
    settings = MagicMock()
    settings.outputs_dir = str(tmp_path / "outputs")
    settings.transcripts_dir = str(tmp_path / "transcripts")
    settings.claude_model = "claude-sonnet-4-20250514"
    settings.claude_temperature = 0.3
    settings.claude_max_tokens = 8192
    settings.dry_run = False

    # Create adapted script
    adapted_path = Path(settings.outputs_dir) / "ep_test" / "script.adapted.tr.md"
    adapted_path.parent.mkdir(parents=True)
    adapted_path.write_text("# Test\n\nContent.", encoding="utf-8")

    # Pre-create existing output
    chapters_path = Path(settings.outputs_dir) / "ep_test" / "chapters.json"
    chapters_path.write_text('{"old": "data"}', encoding="utf-8")

    # Mock prompt registry
    mock_version = MagicMock()
    mock_version.version = 1
    mock_registry.return_value.register_version.return_value = mock_version
    mock_registry.return_value.load_template.return_value = (
        "",
        "# System\n\n# Input\n\n{{episode_id}}",
    )
    mock_registry.return_value.compute_hash.return_value = "prompt_hash"

    # Mock Claude API response
    mock_response = MagicMock()
    mock_response.text = json.dumps(
        {
            "schema_version": "1.0",
            "episode_id": "ep_test",
            "title": "Test",
            "total_chapters": 1,
            "estimated_duration_seconds": 60,
            "chapters": [
                {
                    "chapter_id": "ch01",
                    "title": "Test",
                    "order": 1,
                    "narration": {
                        "text": "Test.",
                        "word_count": 1,
                        "estimated_duration_seconds": 60,
                    },
                    "visual": {"type": "title_card", "description": "Title", "image_prompt": None},
                    "overlays": [],
                    "transitions": {"in": "fade", "out": "fade"},
                },
            ],
        }
    )
    mock_response.input_tokens = 100
    mock_response.output_tokens = 50
    mock_response.cost_usd = 0.01
    mock_call_claude.return_value = mock_response

    # Run with force=True
    result = chapterize_script(db_session, "ep_test", settings, force=True)

    # Should not skip
    assert result.skipped is False
    assert mock_call_claude.call_count == 1


def test_chapterize_script_missing_adapted(db_session, tmp_path):
    """Test chapterization fails if adapted script missing."""
    settings = MagicMock()
    settings.outputs_dir = str(tmp_path / "outputs")

    episode = Episode(
        episode_id="ep_missing",
        source="youtube_rss",
        title="Test",
        url="https://youtube.com/watch?v=test",
        status=EpisodeStatus.ADAPTED,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    with pytest.raises(FileNotFoundError) as exc_info:
        chapterize_script(db_session, "ep_missing", settings, force=False)
    assert "Adapted script not found" in str(exc_info.value)


def test_chapterize_script_wrong_status(db_session, tmp_path):
    """Test chapterization fails if episode not in ADAPTED status."""
    settings = MagicMock()
    settings.outputs_dir = str(tmp_path / "outputs")

    episode = Episode(
        episode_id="ep_wrong_status",
        source="youtube_rss",
        title="Test",
        url="https://youtube.com/watch?v=test",
        status=EpisodeStatus.TRANSLATED,  # Wrong status
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    with pytest.raises(ValueError) as exc_info:
        chapterize_script(db_session, "ep_wrong_status", settings, force=False)
    assert "expected 'adapted'" in str(exc_info.value).lower()
