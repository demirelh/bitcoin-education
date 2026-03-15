"""Tests for review_gate_stock pipeline stage and ReviewTask integration."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.core.pipeline import _V2_STAGES, _run_stage
from btcedu.db import Base
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.review import ReviewStatus, ReviewTask


@pytest.fixture
def db_engine():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    factory = sessionmaker(bind=db_engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture
def settings(tmp_path):
    s = MagicMock()
    s.outputs_dir = str(tmp_path)
    s.pexels_api_key = "test-key"
    s.pexels_results_per_chapter = 3
    s.pexels_orientation = "landscape"
    s.pexels_download_size = "large2x"
    s.claude_model = "claude-sonnet-4-20250514"
    s.claude_max_tokens = 4096
    s.claude_temperature = 0.1
    s.max_episode_cost_usd = 10.0
    s.dry_run = False
    s.pipeline_version = 2
    s.image_gen_provider = "pexels"
    s.transcripts_dir = str(tmp_path / "transcripts")
    return s


@pytest.fixture
def chapterized_episode(db_session):
    ep = Episode(
        episode_id="ep_stock",
        source="youtube_rss",
        title="Stock Image Test Episode",
        url="https://youtube.com/watch?v=ep_stock",
        status=EpisodeStatus.CHAPTERIZED,
        pipeline_version=2,
    )
    db_session.add(ep)
    db_session.commit()
    return ep


# ---------------------------------------------------------------------------
# review_gate_stock tests
# ---------------------------------------------------------------------------


def test_v2_stages_includes_review_gate_stock():
    """_V2_STAGES contains review_gate_stock entry."""
    stage_names = [s[0] for s in _V2_STAGES]
    assert "review_gate_stock" in stage_names
    idx = stage_names.index("review_gate_stock")
    # Should be after imagegen and before tts
    assert stage_names[idx - 1] == "imagegen"
    assert stage_names[idx + 1] == "tts"


def test_review_gate_stock_creates_task(
    db_session, settings, chapterized_episode, tmp_path
):
    """ReviewTask created with stage='stock_images'."""
    # Create required files
    ep_dir = tmp_path / "ep_stock"
    candidates_dir = ep_dir / "images" / "candidates"
    candidates_dir.mkdir(parents=True)
    (candidates_dir / "candidates_manifest.json").write_text("{}")
    (ep_dir / "chapters.json").write_text("{}")

    result = _run_stage(
        db_session, chapterized_episode, settings, "review_gate_stock"
    )

    assert result.status == "review_pending"
    assert "review task created" in result.detail

    tasks = db_session.query(ReviewTask).filter(
        ReviewTask.episode_id == "ep_stock",
        ReviewTask.stage == "stock_images",
    ).all()
    assert len(tasks) == 1
    assert tasks[0].status == ReviewStatus.PENDING.value


def test_review_gate_stock_pending(
    db_session, settings, chapterized_episode, tmp_path
):
    """Returns 'review_pending' when task exists."""
    # Create existing review task
    task = ReviewTask(
        episode_id="ep_stock",
        stage="stock_images",
        status=ReviewStatus.PENDING.value,
        artifact_paths="[]",
    )
    db_session.add(task)
    db_session.commit()

    result = _run_stage(
        db_session, chapterized_episode, settings, "review_gate_stock"
    )

    assert result.status == "review_pending"
    assert "awaiting stock image review" in result.detail


@patch("btcedu.core.stock_images.finalize_selections")
def test_review_gate_stock_approved_finalizes(
    mock_finalize, db_session, settings, chapterized_episode
):
    """Calls finalize_selections() on approval."""
    from btcedu.core.stock_images import StockSelectResult

    # Create approved review task
    task = ReviewTask(
        episode_id="ep_stock",
        stage="stock_images",
        status=ReviewStatus.APPROVED.value,
        artifact_paths="[]",
    )
    db_session.add(task)
    db_session.commit()

    mock_finalize.return_value = StockSelectResult(
        episode_id="ep_stock",
        images_path=Path("/tmp/images"),
        manifest_path=Path("/tmp/manifest.json"),
        selected_count=10,
        placeholder_count=2,
    )

    result = _run_stage(
        db_session, chapterized_episode, settings, "review_gate_stock"
    )

    assert result.status == "success"
    assert "approved" in result.detail
    mock_finalize.assert_called_once()


def test_review_gate_stock_artifact_paths(
    db_session, settings, chapterized_episode, tmp_path
):
    """ReviewTask has candidates_manifest + chapters.json paths."""
    ep_dir = tmp_path / "ep_stock"
    candidates_dir = ep_dir / "images" / "candidates"
    candidates_dir.mkdir(parents=True)
    (candidates_dir / "candidates_manifest.json").write_text("{}")
    (ep_dir / "chapters.json").write_text("{}")

    _run_stage(
        db_session, chapterized_episode, settings, "review_gate_stock"
    )

    task = db_session.query(ReviewTask).filter(
        ReviewTask.episode_id == "ep_stock",
        ReviewTask.stage == "stock_images",
    ).first()
    assert task is not None

    paths = json.loads(task.artifact_paths)
    assert len(paths) == 2
    assert "candidates_manifest.json" in paths[0]
    assert "chapters.json" in paths[1]


def test_rejection_keeps_chapterized_status(db_session, settings, chapterized_episode):
    """Rejection keeps episode at CHAPTERIZED (no revert)."""
    from btcedu.core.reviewer import reject_review

    task = ReviewTask(
        episode_id="ep_stock",
        stage="stock_images",
        status=ReviewStatus.PENDING.value,
        artifact_paths="[]",
    )
    db_session.add(task)
    db_session.commit()

    reject_review(db_session, task.id, notes="Wrong images")

    db_session.refresh(chapterized_episode)
    assert chapterized_episode.status == EpisodeStatus.CHAPTERIZED


def test_pexels_key_required_for_v2_imagegen(
    db_session, settings, chapterized_episode
):
    """ValueError if pexels_api_key empty for imagegen."""
    settings.pexels_api_key = ""

    result = _run_stage(
        db_session, chapterized_episode, settings, "imagegen"
    )

    # Should fail because PexelsService raises on empty key
    assert result.status == "failed"


@patch("btcedu.core.stock_images.finalize_selections")
def test_pipeline_resumes_after_stock_approval(
    mock_finalize, db_session, settings, chapterized_episode
):
    """After approval, review_gate_stock returns success."""
    from btcedu.core.stock_images import StockSelectResult

    task = ReviewTask(
        episode_id="ep_stock",
        stage="stock_images",
        status=ReviewStatus.APPROVED.value,
        artifact_paths="[]",
    )
    db_session.add(task)
    db_session.commit()

    mock_finalize.return_value = StockSelectResult(
        episode_id="ep_stock",
        images_path=Path("/tmp/images"),
        manifest_path=Path("/tmp/manifest.json"),
        selected_count=8,
        placeholder_count=4,
    )

    result = _run_stage(
        db_session, chapterized_episode, settings, "review_gate_stock"
    )
    assert result.status == "success"
    assert "8 finalized" in result.detail
