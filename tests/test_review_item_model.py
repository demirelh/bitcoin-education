"""Tests for ReviewItemDecision model and reviewer upsert/get functions."""

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.db import Base
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.review import ReviewStatus, ReviewTask


@pytest.fixture
def item_db_engine():
    """In-memory engine with all tables including review_item_decisions."""
    engine = create_engine("sqlite:///:memory:")
    # Base includes ReviewItemDecision because it shares btcedu.db.Base
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def item_session(item_db_engine):
    factory = sessionmaker(bind=item_db_engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture
def pending_task(item_session, tmp_path):
    """PENDING ReviewTask with a correction diff fixture."""
    import json

    diff_dir = tmp_path / "review"
    diff_dir.mkdir(parents=True)
    diff_file = diff_dir / "correction_diff.json"
    diff_data = {
        "episode_id": "ep001",
        "changes": [
            {
                "item_id": "corr-0000",
                "type": "replace",
                "original": "Bit Coin",
                "corrected": "Bitcoin",
                "context": "...Bitcoin...",
                "position": {"start_word": 0, "end_word": 2},
                "category": "auto",
            },
            {
                "item_id": "corr-0001",
                "type": "replace",
                "original": "blokchain",
                "corrected": "blockchain",
                "context": "...blockchain...",
                "position": {"start_word": 5, "end_word": 6},
                "category": "auto",
            },
        ],
        "summary": {"total_changes": 2, "by_type": {"replace": 2}},
    }
    diff_file.write_text(json.dumps(diff_data), encoding="utf-8")

    episode = Episode(
        episode_id="ep001",
        source="youtube_rss",
        title="Test",
        url="http://example.com",
        status=EpisodeStatus.CORRECTED,
        transcript_path=str(tmp_path / "transcript.de.txt"),
    )
    item_session.add(episode)
    item_session.commit()

    task = ReviewTask(
        episode_id="ep001",
        stage="correct",
        status=ReviewStatus.PENDING.value,
        artifact_paths='["/tmp/a.txt"]',
        diff_path=str(diff_file),
    )
    item_session.add(task)
    item_session.commit()
    return task


def test_create_review_item_decision(item_session, pending_task):
    from btcedu.models.review_item import ReviewItemAction, ReviewItemDecision

    record = ReviewItemDecision(
        review_task_id=pending_task.id,
        item_id="corr-0000",
        operation_type="replace",
        original_text="Bit Coin",
        proposed_text="Bitcoin",
        action=ReviewItemAction.PENDING.value,
    )
    item_session.add(record)
    item_session.commit()

    fetched = (
        item_session.query(ReviewItemDecision)
        .filter(ReviewItemDecision.review_task_id == pending_task.id)
        .first()
    )
    assert fetched is not None
    assert fetched.action == "pending"
    assert fetched.decided_at is None
    assert fetched.edited_text is None


def test_upsert_item_decision_create(item_session, pending_task):
    from btcedu.core.reviewer import upsert_item_decision
    from btcedu.models.review_item import ReviewItemDecision

    record = upsert_item_decision(item_session, pending_task.id, "corr-0000", "accepted")
    assert record.action == "accepted"
    assert record.decided_at is not None

    count = (
        item_session.query(ReviewItemDecision)
        .filter(ReviewItemDecision.review_task_id == pending_task.id)
        .count()
    )
    assert count == 1


def test_upsert_item_decision_update(item_session, pending_task):
    from btcedu.core.reviewer import upsert_item_decision
    from btcedu.models.review_item import ReviewItemDecision

    upsert_item_decision(item_session, pending_task.id, "corr-0000", "accepted")
    upsert_item_decision(item_session, pending_task.id, "corr-0000", "rejected")

    count = (
        item_session.query(ReviewItemDecision)
        .filter(ReviewItemDecision.review_task_id == pending_task.id)
        .count()
    )
    assert count == 1  # no duplicate row

    record = (
        item_session.query(ReviewItemDecision)
        .filter(ReviewItemDecision.review_task_id == pending_task.id)
        .first()
    )
    assert record.action == "rejected"


def test_item_decision_cascade_delete(item_session, pending_task):
    from btcedu.core.reviewer import upsert_item_decision
    from btcedu.models.review_item import ReviewItemDecision

    upsert_item_decision(item_session, pending_task.id, "corr-0000", "accepted")
    upsert_item_decision(item_session, pending_task.id, "corr-0001", "rejected")

    count_before = (
        item_session.query(ReviewItemDecision)
        .filter(ReviewItemDecision.review_task_id == pending_task.id)
        .count()
    )
    assert count_before == 2

    item_session.delete(pending_task)
    item_session.commit()

    count_after = (
        item_session.query(ReviewItemDecision)
        .filter(ReviewItemDecision.review_task_id == pending_task.id)
        .count()
    )
    assert count_after == 0


def test_get_item_decisions(item_session, tmp_path):
    import json

    from btcedu.core.reviewer import get_item_decisions, upsert_item_decision

    # Create a second task to verify isolation
    diff_dir2 = tmp_path / "review2"
    diff_dir2.mkdir(parents=True)
    diff_file2 = diff_dir2 / "correction_diff.json"
    diff_file2.write_text(json.dumps({"episode_id": "ep002", "changes": [], "summary": {"total_changes": 0, "by_type": {}}}), encoding="utf-8")

    episode2 = Episode(
        episode_id="ep002",
        source="youtube_rss",
        title="Test 2",
        url="http://example.com/2",
        status=EpisodeStatus.CORRECTED,
    )
    item_session.add(episode2)
    item_session.commit()

    task2 = ReviewTask(
        episode_id="ep002",
        stage="correct",
        status=ReviewStatus.PENDING.value,
        artifact_paths='[]',
        diff_path=str(diff_file2),
    )
    item_session.add(task2)
    item_session.commit()

    # Create a fresh task for ep001
    diff_dir1 = tmp_path / "review1"
    diff_dir1.mkdir(parents=True)
    diff_file1 = diff_dir1 / "correction_diff.json"
    diff_data = {
        "episode_id": "ep001b",
        "changes": [
            {"item_id": "corr-0000", "type": "replace", "original": "a", "corrected": "b", "context": "...", "position": {"start_word": 0, "end_word": 1}, "category": "auto"},
            {"item_id": "corr-0001", "type": "replace", "original": "c", "corrected": "d", "context": "...", "position": {"start_word": 2, "end_word": 3}, "category": "auto"},
            {"item_id": "corr-0002", "type": "replace", "original": "e", "corrected": "f", "context": "...", "position": {"start_word": 4, "end_word": 5}, "category": "auto"},
        ],
        "summary": {"total_changes": 3, "by_type": {"replace": 3}},
    }
    diff_file1.write_text(json.dumps(diff_data), encoding="utf-8")

    episode1b = Episode(
        episode_id="ep001b",
        source="youtube_rss",
        title="Test 1b",
        url="http://example.com/1b",
        status=EpisodeStatus.CORRECTED,
    )
    item_session.add(episode1b)
    item_session.commit()

    task1b = ReviewTask(
        episode_id="ep001b",
        stage="correct",
        status=ReviewStatus.PENDING.value,
        artifact_paths='[]',
        diff_path=str(diff_file1),
    )
    item_session.add(task1b)
    item_session.commit()

    upsert_item_decision(item_session, task1b.id, "corr-0000", "accepted")
    upsert_item_decision(item_session, task1b.id, "corr-0001", "rejected")
    upsert_item_decision(item_session, task1b.id, "corr-0002", "edited", edited_text="custom")
    upsert_item_decision(item_session, task2.id, "corr-0000", "accepted")

    decisions = get_item_decisions(item_session, task1b.id)
    assert len(decisions) == 3
    assert "corr-0000" in decisions
    assert decisions["corr-0001"].action == "rejected"
    assert decisions["corr-0002"].edited_text == "custom"

    # task2's decisions should not appear
    decisions2 = get_item_decisions(item_session, task2.id)
    assert len(decisions2) == 1
