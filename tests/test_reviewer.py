"""Tests for btcedu.core.reviewer module."""

import json
from pathlib import Path

import pytest

from btcedu.core.reviewer import (
    approve_review,
    create_review_task,
    get_latest_reviewer_feedback,
    get_pending_reviews,
    get_review_detail,
    has_approved_review,
    has_pending_review,
    pending_review_count,
    reject_review,
    request_changes,
)
from btcedu.core.pipeline import StageResult, _run_stage
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.review import ReviewStatus, ReviewTask


@pytest.fixture
def corrected_episode(db_session, tmp_path):
    """Episode at CORRECTED status with transcript and corrected files."""
    transcript_dir = tmp_path / "transcripts" / "ep001"
    transcript_dir.mkdir(parents=True)
    transcript_file = transcript_dir / "transcript.de.txt"
    transcript_file.write_text("Original text here.", encoding="utf-8")

    corrected_file = transcript_dir / "transcript.corrected.de.txt"
    corrected_file.write_text("Corrected text here.", encoding="utf-8")

    review_dir = tmp_path / "outputs" / "ep001" / "review"
    review_dir.mkdir(parents=True)
    diff_file = review_dir / "correction_diff.json"
    diff_data = {
        "episode_id": "ep001",
        "original_length": 19,
        "corrected_length": 20,
        "changes": [
            {
                "type": "replace",
                "original": "Original",
                "corrected": "Corrected",
                "context": "...Corrected text here...",
                "position": {"start_word": 0, "end_word": 1},
                "category": "auto",
            }
        ],
        "summary": {"total_changes": 1, "by_type": {"replace": 1}},
    }
    diff_file.write_text(json.dumps(diff_data), encoding="utf-8")

    episode = Episode(
        episode_id="ep001",
        source="youtube_rss",
        title="Bitcoin und die Zukunft des Geldes",
        url="https://youtube.com/watch?v=ep001",
        status=EpisodeStatus.CORRECTED,
        transcript_path=str(transcript_file),
    )
    db_session.add(episode)
    db_session.commit()

    return {
        "episode": episode,
        "corrected_path": str(corrected_file),
        "diff_path": str(diff_file),
        "tmp_path": tmp_path,
    }


@pytest.fixture
def review_task(db_session, corrected_episode):
    """PENDING ReviewTask for the corrected episode."""
    return create_review_task(
        db_session,
        episode_id="ep001",
        stage="correct",
        artifact_paths=[corrected_episode["corrected_path"]],
        diff_path=corrected_episode["diff_path"],
    )


class TestCreateReviewTask:
    def test_creates_pending_task(self, db_session, corrected_episode):
        task = create_review_task(
            db_session,
            episode_id="ep001",
            stage="correct",
            artifact_paths=[corrected_episode["corrected_path"]],
            diff_path=corrected_episode["diff_path"],
        )
        assert task.id is not None
        assert task.episode_id == "ep001"
        assert task.stage == "correct"
        assert task.status == ReviewStatus.PENDING.value
        assert task.diff_path == corrected_episode["diff_path"]

    def test_computes_artifact_hash(self, db_session, corrected_episode):
        task = create_review_task(
            db_session,
            episode_id="ep001",
            stage="correct",
            artifact_paths=[corrected_episode["corrected_path"]],
        )
        assert task.artifact_hash is not None
        assert len(task.artifact_hash) == 64  # SHA-256 hex


class TestApproveReview:
    def test_sets_approved_status(self, db_session, review_task):
        decision = approve_review(db_session, review_task.id, notes="Looks good")
        db_session.refresh(review_task)
        assert review_task.status == ReviewStatus.APPROVED.value
        assert review_task.reviewed_at is not None
        assert decision.decision == "approved"
        assert decision.notes == "Looks good"

    def test_does_not_advance_episode(self, db_session, review_task, corrected_episode):
        approve_review(db_session, review_task.id)
        episode = corrected_episode["episode"]
        db_session.refresh(episode)
        # Episode should still be CORRECTED — pipeline advances on next run
        assert episode.status == EpisodeStatus.CORRECTED


class TestRejectReview:
    def test_reverts_episode(self, db_session, review_task, corrected_episode):
        decision = reject_review(db_session, review_task.id, notes="Too many errors")
        episode = corrected_episode["episode"]
        db_session.refresh(episode)
        assert episode.status == EpisodeStatus.TRANSCRIBED
        assert decision.decision == "rejected"

    def test_creates_decision(self, db_session, review_task):
        decision = reject_review(db_session, review_task.id)
        assert decision.id is not None
        assert decision.review_task_id == review_task.id
        assert decision.decision == "rejected"


class TestRequestChanges:
    def test_requires_notes(self, db_session, review_task):
        with pytest.raises(ValueError, match="Notes are required"):
            request_changes(db_session, review_task.id, notes="")

    def test_marks_stale(self, db_session, review_task, corrected_episode):
        request_changes(db_session, review_task.id, notes="Fix punctuation")
        stale_marker = Path(corrected_episode["corrected_path"] + ".stale")
        assert stale_marker.exists()

    def test_stores_feedback(self, db_session, review_task):
        request_changes(db_session, review_task.id, notes="Fix Bitcoin spelling")
        db_session.refresh(review_task)
        assert review_task.reviewer_notes == "Fix Bitcoin spelling"
        assert review_task.status == ReviewStatus.CHANGES_REQUESTED.value


class TestCannotActOnDecidedTask:
    def test_cannot_approve_approved(self, db_session, review_task):
        approve_review(db_session, review_task.id)
        with pytest.raises(ValueError, match="cannot act"):
            approve_review(db_session, review_task.id)

    def test_cannot_reject_rejected(self, db_session, review_task):
        reject_review(db_session, review_task.id)
        with pytest.raises(ValueError, match="cannot act"):
            reject_review(db_session, review_task.id)


class TestGetPendingReviews:
    def test_returns_pending_only(self, db_session, corrected_episode):
        t1 = create_review_task(
            db_session, "ep001", "correct", [corrected_episode["corrected_path"]]
        )
        approve_review(db_session, t1.id)

        t2 = create_review_task(
            db_session, "ep001", "correct", [corrected_episode["corrected_path"]]
        )

        pending = get_pending_reviews(db_session)
        assert len(pending) == 1
        assert pending[0].id == t2.id

    def test_newest_first(self, db_session, corrected_episode):
        t1 = create_review_task(
            db_session, "ep001", "correct", [corrected_episode["corrected_path"]]
        )
        t2 = create_review_task(
            db_session, "ep001", "correct", [corrected_episode["corrected_path"]]
        )
        pending = get_pending_reviews(db_session)
        assert pending[0].id == t2.id
        assert pending[1].id == t1.id


class TestGetReviewDetail:
    def test_returns_diff_data(self, db_session, review_task, corrected_episode):
        detail = get_review_detail(db_session, review_task.id)
        assert detail["id"] == review_task.id
        assert detail["episode_title"] == "Bitcoin und die Zukunft des Geldes"
        assert detail["diff"] is not None
        assert detail["diff"]["summary"]["total_changes"] == 1
        assert detail["original_text"] == "Original text here."
        assert detail["corrected_text"] == "Corrected text here."


class TestHasApprovedReview:
    def test_true_when_approved(self, db_session, review_task):
        assert not has_approved_review(db_session, "ep001", "correct")
        approve_review(db_session, review_task.id)
        assert has_approved_review(db_session, "ep001", "correct")

    def test_false_when_pending(self, db_session, review_task):
        assert not has_approved_review(db_session, "ep001", "correct")


class TestGetLatestReviewerFeedback:
    def test_returns_notes(self, db_session, review_task):
        request_changes(db_session, review_task.id, notes="Fix the spelling of Bitcoin")
        feedback = get_latest_reviewer_feedback(db_session, "ep001", "correct")
        assert feedback == "Fix the spelling of Bitcoin"

    def test_returns_none_when_no_feedback(self, db_session, review_task):
        assert get_latest_reviewer_feedback(db_session, "ep001", "correct") is None


class TestPendingReviewCount:
    def test_counts_correctly(self, db_session, corrected_episode):
        assert pending_review_count(db_session) == 0
        create_review_task(
            db_session, "ep001", "correct", [corrected_episode["corrected_path"]]
        )
        assert pending_review_count(db_session) == 1


class TestHasPendingReview:
    def test_true_when_pending(self, db_session, corrected_episode):
        assert not has_pending_review(db_session, "ep001")
        create_review_task(
            db_session, "ep001", "correct", [corrected_episode["corrected_path"]]
        )
        assert has_pending_review(db_session, "ep001")


# ---------------------------------------------------------------------------
# Pipeline review_gate_1 integration
# ---------------------------------------------------------------------------


class TestReviewGate1Pipeline:
    """Tests that _run_stage('review_gate_1') works end-to-end."""

    @pytest.fixture
    def gate_settings(self, corrected_episode):
        """Minimal Settings for review gate tests."""
        tmp_path = corrected_episode["tmp_path"]
        from btcedu.config import Settings

        return Settings(
            transcripts_dir=str(tmp_path / "transcripts"),
            outputs_dir=str(tmp_path / "outputs"),
            reports_dir=str(tmp_path / "reports"),
            anthropic_api_key="test-key",
            pipeline_version=2,
        )

    def test_creates_review_task_and_returns_pending(
        self, db_session, corrected_episode, gate_settings
    ):
        """First call creates a ReviewTask and returns review_pending."""
        episode = corrected_episode["episode"]
        result = _run_stage(db_session, episode, gate_settings, "review_gate_1")

        assert isinstance(result, StageResult)
        assert result.stage == "review_gate_1"
        assert result.status == "review_pending"
        assert "created" in result.detail

        # ReviewTask should exist in DB
        tasks = db_session.query(ReviewTask).filter(
            ReviewTask.episode_id == "ep001",
            ReviewTask.stage == "correct",
        ).all()
        assert len(tasks) == 1
        assert tasks[0].status == ReviewStatus.PENDING.value

    def test_returns_pending_when_task_exists(
        self, db_session, corrected_episode, gate_settings
    ):
        """Second call (task already exists) returns review_pending without creating another."""
        episode = corrected_episode["episode"]

        # First call creates the task
        _run_stage(db_session, episode, gate_settings, "review_gate_1")

        # Second call should see existing pending task
        result = _run_stage(db_session, episode, gate_settings, "review_gate_1")
        assert result.status == "review_pending"
        assert "awaiting" in result.detail

        # Still only one task
        count = db_session.query(ReviewTask).filter(
            ReviewTask.episode_id == "ep001",
        ).count()
        assert count == 1

    def test_returns_success_after_approval(
        self, db_session, corrected_episode, gate_settings
    ):
        """After approving the review, the gate returns success."""
        episode = corrected_episode["episode"]

        # Create and approve
        _run_stage(db_session, episode, gate_settings, "review_gate_1")
        task = db_session.query(ReviewTask).filter(
            ReviewTask.episode_id == "ep001",
            ReviewTask.stage == "correct",
        ).first()
        approve_review(db_session, task.id)

        # Now the gate should pass
        result = _run_stage(db_session, episode, gate_settings, "review_gate_1")
        assert result.status == "success"
        assert "approved" in result.detail
