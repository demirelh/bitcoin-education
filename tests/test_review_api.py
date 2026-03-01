"""Tests for review API endpoints."""

import json

import pytest
from flask import Flask

from btcedu.core.reviewer import create_review_task
from btcedu.models.episode import Episode, EpisodeStatus


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


@pytest.fixture
def render_review_episode(db_engine, tmp_path):
    """Create a RENDERED episode with render outputs and review task."""
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=db_engine)
    session = factory()

    episode = Episode(
        episode_id="ep_render",
        source="youtube_rss",
        title="Render Episode",
        url="https://youtube.com/watch?v=render",
        status=EpisodeStatus.RENDERED,
    )
    session.add(episode)
    session.commit()

    output_root = tmp_path / "outputs" / "ep_render"
    render_dir = output_root / "render"
    render_dir.mkdir(parents=True)
    (render_dir / "draft.mp4").write_bytes(b"fake video")
    (render_dir / "render_manifest.json").write_text(
        json.dumps({"episode_id": "ep_render", "segments": []}), encoding="utf-8"
    )
    (output_root / "chapters.json").write_text(
        json.dumps(
            {
                "episode_id": "ep_render",
                "chapters": [
                    {
                        "chapter_id": "ch01",
                        "title": "Intro",
                        "order": 1,
                        "narration": {"text": "Hello"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    task = create_review_task(
        session,
        episode_id="ep_render",
        stage="render",
        artifact_paths=[str(render_dir / "draft.mp4")],
        diff_path=str(render_dir / "render_manifest.json"),
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

    def test_render_review_detail_uses_app_settings(self, client, render_review_episode):
        resp = client.get(f"/api/reviews/{render_review_episode['task_id']}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["episode_id"] == "ep_render"
        assert data["video_url"] == "/api/episodes/ep_render/render/draft.mp4"
        assert data["render_manifest"] is not None
        assert data["chapter_script"] is not None
        assert data["chapter_script"][0]["text"] == "Hello"


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

    def test_reject_render_requires_notes(self, client, render_review_episode):
        resp = client.post(
            f"/api/reviews/{render_review_episode['task_id']}/reject",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 400
        data = resp.get_json()
        assert "Notes are required" in data["error"]

        resp = client.post(
            f"/api/reviews/{render_review_episode['task_id']}/reject",
            json={"notes": "Needs tweaks"},
            content_type="application/json",
        )
        assert resp.status_code == 200
        data = resp.get_json()
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
