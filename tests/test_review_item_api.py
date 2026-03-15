"""Tests for Phase 5 granular review item API endpoints."""

import json

import pytest
from flask import Flask

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
def review_setup(db_engine, tmp_path):
    """Create a CORRECTED episode + PENDING ReviewTask with correction diff."""
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=db_engine)
    session = factory()

    # Create transcript files
    transcript_dir = tmp_path / "transcripts" / "ep001"
    transcript_dir.mkdir(parents=True)
    transcript_file = transcript_dir / "transcript.de.txt"
    transcript_file.write_text("Bit Coin blokchain Protokoll Ende.", encoding="utf-8")

    corrected_file = transcript_dir / "transcript.corrected.de.txt"
    corrected_file.write_text("Bitcoin blockchain Protokoll Ende.", encoding="utf-8")

    # Create diff file with item_ids
    review_dir = tmp_path / "outputs" / "ep001" / "review"
    review_dir.mkdir(parents=True)
    diff_file = review_dir / "correction_diff.json"
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
                "position": {"start_word": 2, "end_word": 3},
                "category": "auto",
            },
        ],
        "summary": {"total_changes": 2, "by_type": {"replace": 2}},
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

    task = ReviewTask(
        episode_id="ep001",
        stage="correct",
        status=ReviewStatus.PENDING.value,
        artifact_paths=json.dumps([str(corrected_file)]),
        diff_path=str(diff_file),
    )
    session.add(task)
    session.commit()
    task_id = task.id
    session.close()

    return {"task_id": task_id, "transcript_path": str(transcript_file), "diff_path": str(diff_file), "tmp_path": tmp_path}


# ── item action endpoint tests ─────────────────────────────────────────────


class TestAcceptItem:
    def test_accept_item(self, client, review_setup):
        task_id = review_setup["task_id"]
        resp = client.post(f"/api/reviews/{task_id}/items/corr-0000/accept")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["item_id"] == "corr-0000"
        assert data["action"] == "accepted"

    def test_accept_creates_db_record(self, client, db_engine, review_setup):
        from sqlalchemy.orm import sessionmaker
        from btcedu.models.review_item import ReviewItemDecision

        task_id = review_setup["task_id"]
        client.post(f"/api/reviews/{task_id}/items/corr-0000/accept")

        session = sessionmaker(bind=db_engine)()
        record = session.query(ReviewItemDecision).filter(
            ReviewItemDecision.review_task_id == task_id,
            ReviewItemDecision.item_id == "corr-0000",
        ).first()
        session.close()
        assert record is not None
        assert record.action == "accepted"


class TestRejectItem:
    def test_reject_item(self, client, review_setup):
        task_id = review_setup["task_id"]
        resp = client.post(f"/api/reviews/{task_id}/items/corr-0001/reject")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["action"] == "rejected"


class TestEditItem:
    def test_edit_item_valid(self, client, db_engine, review_setup):
        from sqlalchemy.orm import sessionmaker
        from btcedu.models.review_item import ReviewItemDecision

        task_id = review_setup["task_id"]
        resp = client.post(
            f"/api/reviews/{task_id}/items/corr-0002/edit",
            json={"text": "corrected text"},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["action"] == "edited"
        assert data["edited_text"] == "corrected text"

        session = sessionmaker(bind=db_engine)()
        record = session.query(ReviewItemDecision).filter(
            ReviewItemDecision.review_task_id == task_id,
            ReviewItemDecision.item_id == "corr-0002",
        ).first()
        session.close()
        assert record.edited_text == "corrected text"

    def test_edit_item_missing_text(self, client, review_setup):
        task_id = review_setup["task_id"]
        resp = client.post(
            f"/api/reviews/{task_id}/items/corr-0002/edit",
            json={},
        )
        assert resp.status_code == 400

    def test_edit_item_empty_text(self, client, review_setup):
        task_id = review_setup["task_id"]
        resp = client.post(
            f"/api/reviews/{task_id}/items/corr-0002/edit",
            json={"text": "   "},
        )
        assert resp.status_code == 400


class TestResetItem:
    def test_reset_item(self, client, db_engine, review_setup):
        from sqlalchemy.orm import sessionmaker
        from btcedu.models.review_item import ReviewItemDecision

        task_id = review_setup["task_id"]
        # First accept
        client.post(f"/api/reviews/{task_id}/items/corr-0000/accept")
        # Then reset
        resp = client.post(f"/api/reviews/{task_id}/items/corr-0000/reset")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["action"] == "pending"

        session = sessionmaker(bind=db_engine)()
        record = session.query(ReviewItemDecision).filter(
            ReviewItemDecision.review_task_id == task_id,
            ReviewItemDecision.item_id == "corr-0000",
        ).first()
        session.close()
        assert record.action == "pending"


class TestItemActionGuards:
    def test_item_action_on_approved_review(self, client, db_engine, review_setup):
        from sqlalchemy.orm import sessionmaker
        from btcedu.models.review import ReviewTask

        task_id = review_setup["task_id"]
        # Approve the task
        session = sessionmaker(bind=db_engine)()
        task = session.query(ReviewTask).filter(ReviewTask.id == task_id).first()
        task.status = "approved"
        session.commit()
        session.close()

        resp = client.post(f"/api/reviews/{task_id}/items/corr-0000/accept")
        assert resp.status_code == 400

    def test_item_action_on_nonexistent_review(self, client):
        resp = client.post("/api/reviews/99999/items/corr-0000/accept")
        assert resp.status_code == 404


class TestApplyReviewItems:
    def test_apply_corrections(self, client, review_setup, tmp_path):
        task_id = review_setup["task_id"]
        # Accept one item first
        client.post(f"/api/reviews/{task_id}/items/corr-0000/accept")

        resp = client.post(f"/api/reviews/{task_id}/apply")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert "reviewed_file" in data
        assert data["total_items"] == 2
        # pending_count: corr-0001 has no decision → pending
        assert data["pending_count"] >= 1
        # Verify file was created
        import os
        assert os.path.exists(data["reviewed_file"])

    def test_apply_no_decisions_yet(self, client, review_setup):
        task_id = review_setup["task_id"]
        resp = client.post(f"/api/reviews/{task_id}/apply")
        assert resp.status_code == 400
        data = resp.get_json()
        assert "No item decisions" in data["error"]

    def test_review_detail_includes_item_decisions(self, client, db_engine, review_setup):
        from sqlalchemy.orm import sessionmaker
        from btcedu.core.reviewer import upsert_item_decision

        task_id = review_setup["task_id"]
        session = sessionmaker(bind=db_engine)()
        upsert_item_decision(session, task_id, "corr-0000", "accepted")
        session.close()

        resp = client.get(f"/api/reviews/{task_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "item_decisions" in data
        assert isinstance(data["item_decisions"], dict)
        assert "corr-0000" in data["item_decisions"]
        assert data["item_decisions"]["corr-0000"]["action"] == "accepted"


class TestBackwardCompatibility:
    def test_old_diff_without_item_id(self, client, db_engine, review_setup):
        """Old-format diffs (no item_id) return empty item_decisions without crashing."""
        import json
        from sqlalchemy.orm import sessionmaker
        from btcedu.models.review import ReviewTask

        tmp_path = review_setup["tmp_path"]
        # Write old-format diff (no item_id fields)
        old_diff_path = tmp_path / "outputs" / "ep_old" / "review" / "correction_diff.json"
        old_diff_path.parent.mkdir(parents=True, exist_ok=True)
        old_diff_data = {
            "episode_id": "ep_old",
            "changes": [
                {
                    "type": "replace",
                    "original": "Bit Coin",
                    "corrected": "Bitcoin",
                    "context": "...Bitcoin...",
                    "position": {"start_word": 0, "end_word": 2},
                    "category": "auto",
                }
            ],
            "summary": {"total_changes": 1, "by_type": {"replace": 1}},
        }
        old_diff_path.write_text(json.dumps(old_diff_data), encoding="utf-8")

        session = sessionmaker(bind=db_engine)()
        episode = Episode(
            episode_id="ep_old",
            source="youtube_rss",
            title="Old Episode",
            url="http://example.com",
            status=EpisodeStatus.CORRECTED,
        )
        session.add(episode)
        session.commit()

        task = ReviewTask(
            episode_id="ep_old",
            stage="correct",
            status=ReviewStatus.PENDING.value,
            artifact_paths="[]",
            diff_path=str(old_diff_path),
        )
        session.add(task)
        session.commit()
        old_task_id = task.id
        session.close()

        resp = client.get(f"/api/reviews/{old_task_id}")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "item_decisions" in data
        assert data["item_decisions"] == {}
