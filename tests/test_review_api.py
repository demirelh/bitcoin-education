"""Tests for review API endpoints."""

import json

import pytest
from flask import Flask

from btcedu.core.reviewer import create_review_task
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.review import ReviewStatus, ReviewTask


@pytest.fixture
def app(db_engine, tmp_path):
    """Flask test app with in-memory DB."""
    from sqlalchemy.orm import sessionmaker

    from btcedu.web.api import api_bp
    from btcedu.web.jobs import JobManager

    factory = sessionmaker(bind=db_engine)

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["session_factory"] = factory
    app.config["settings"] = type(
        "Settings",
        (),
        {
            "database_url": "sqlite:///:memory:",
            "raw_data_dir": str(tmp_path / "raw"),
            "transcripts_dir": str(tmp_path / "transcripts"),
            "outputs_dir": str(tmp_path / "outputs"),
            "chunks_dir": str(tmp_path / "chunks"),
            "reports_dir": str(tmp_path / "reports"),
            "logs_dir": str(tmp_path / "logs"),
        },
    )()
    app.config["job_manager"] = JobManager(str(tmp_path / "logs"))

    app.register_blueprint(api_bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def review_episode(db_engine, tmp_path):
    """Create a CORRECTED episode with files."""
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=db_engine)
    session = factory()

    # Create transcript files
    transcript_dir = tmp_path / "transcripts" / "ep001"
    transcript_dir.mkdir(parents=True)
    transcript_file = transcript_dir / "transcript.de.txt"
    transcript_file.write_text("Original text.", encoding="utf-8")

    corrected_file = transcript_dir / "transcript.corrected.de.txt"
    corrected_file.write_text("Corrected text.", encoding="utf-8")

    # Create diff file
    review_dir = tmp_path / "outputs" / "ep001" / "review"
    review_dir.mkdir(parents=True)
    diff_file = review_dir / "correction_diff.json"
    diff_data = {
        "episode_id": "ep001",
        "changes": [{"type": "replace", "original": "Bit Coin", "corrected": "Bitcoin"}],
        "summary": {"total_changes": 1, "by_type": {"replace": 1}},
    }
    diff_file.write_text(json.dumps(diff_data), encoding="utf-8")

    episode = Episode(
        episode_id="ep001",
        source="youtube_rss",
        title="Test Episode",
        url="https://youtube.com/watch?v=ep001",
        status=EpisodeStatus.CORRECTED,
        transcript_path=str(transcript_file),
    )
    session.add(episode)
    session.commit()

    # Create a review task
    task = create_review_task(
        session,
        episode_id="ep001",
        stage="correct",
        artifact_paths=[str(corrected_file)],
        diff_path=str(diff_file),
    )
    session.close()

    return {"episode": episode, "task_id": task.id}


class TestListReviews:
    def test_empty_list(self, client):
        resp = client.get("/api/reviews")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["pending_count"] == 0
        assert data["tasks"] == []

    def test_with_pending(self, client, review_episode):
        resp = client.get("/api/reviews")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["pending_count"] == 1
        assert len(data["tasks"]) == 1
        assert data["tasks"][0]["episode_title"] == "Test Episode"


class TestReviewCount:
    def test_count_endpoint(self, client, review_episode):
        resp = client.get("/api/reviews/count")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["pending_count"] == 1


class TestGetReviewDetail:
    def test_404_for_nonexistent(self, client):
        resp = client.get("/api/reviews/999")
        assert resp.status_code == 404

    def test_with_diff(self, client, review_episode):
        resp = client.get(f"/api/reviews/{review_episode['task_id']}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["episode_id"] == "ep001"
        assert data["diff"] is not None
        assert data["diff"]["summary"]["total_changes"] == 1


class TestApproveViaApi:
    def test_approve(self, client, review_episode):
        resp = client.post(
            f"/api/reviews/{review_episode['task_id']}/approve",
            json={"notes": "LGTM"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["decision"] == "approved"


class TestRejectViaApi:
    def test_reject(self, client, review_episode):
        resp = client.post(
            f"/api/reviews/{review_episode['task_id']}/reject",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["decision"] == "rejected"


class TestRequestChangesViaApi:
    def test_with_notes(self, client, review_episode):
        resp = client.post(
            f"/api/reviews/{review_episode['task_id']}/request-changes",
            json={"notes": "Fix the punctuation"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["decision"] == "changes_requested"

    def test_missing_notes(self, client, review_episode):
        resp = client.post(
            f"/api/reviews/{review_episode['task_id']}/request-changes",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "Notes are required" in data["error"]
