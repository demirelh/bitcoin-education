"""Tests for stock image API endpoints (Phase 2)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from btcedu.models.review import ReviewStatus, ReviewTask


@pytest.fixture
def app(tmp_path):
    """Create Flask test app with mocked settings."""
    from flask import Flask
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from btcedu.db import Base
    from btcedu.web.api import api_bp
    from btcedu.web.jobs import JobManager

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    settings = MagicMock()
    settings.outputs_dir = str(tmp_path)
    settings.pexels_api_key = "test-key"
    settings.claude_model = "claude-sonnet-4-20250514"
    settings.dry_run = False
    settings.logs_dir = str(tmp_path / "logs")

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["settings"] = settings
    app.config["session_factory"] = session_factory
    app.config["job_manager"] = JobManager(str(tmp_path / "logs"))
    app.register_blueprint(api_bp, url_prefix="/api")
    return app


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def setup_episode(app, tmp_path):
    """Create episode in DB and candidates manifest on disk."""
    from btcedu.models.episode import Episode, EpisodeStatus

    session = app.config["session_factory"]()
    ep = Episode(
        episode_id="ep_test",
        source="youtube_rss",
        title="Test Episode",
        url="https://youtube.com/watch?v=ep_test",
        status=EpisodeStatus.CHAPTERIZED,
    )
    session.add(ep)
    session.commit()

    # Create candidates manifest
    ep_dir = tmp_path / "ep_test"
    candidates_dir = ep_dir / "images" / "candidates"
    candidates_dir.mkdir(parents=True)

    manifest = {
        "episode_id": "ep_test",
        "schema_version": "1.0",
        "chapters": {
            "ch01": {
                "search_query": "bitcoin mining",
                "candidates": [
                    {
                        "pexels_id": 100,
                        "photographer": "John",
                        "photographer_url": "",
                        "source_url": "",
                        "download_url": "",
                        "local_path": "images/candidates/ch01/pexels_100.jpg",
                        "alt_text": "Mining photo",
                        "width": 1880,
                        "height": 1253,
                        "size_bytes": 200000,
                        "downloaded_at": "2026-01-01",
                        "selected": False,
                        "locked": False,
                    },
                    {
                        "pexels_id": 101,
                        "photographer": "Jane",
                        "photographer_url": "",
                        "source_url": "",
                        "download_url": "",
                        "local_path": "images/candidates/ch01/pexels_101.jpg",
                        "alt_text": "Another photo",
                        "width": 1880,
                        "height": 1253,
                        "size_bytes": 210000,
                        "downloaded_at": "2026-01-01",
                        "selected": False,
                        "locked": False,
                    },
                ],
            }
        },
    }
    (candidates_dir / "candidates_manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8"
    )

    # Create dummy image files
    ch_dir = candidates_dir / "ch01"
    ch_dir.mkdir(parents=True, exist_ok=True)
    (ch_dir / "pexels_100.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
    (ch_dir / "pexels_101.jpg").write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)

    session.close()
    return ep


def test_get_candidates_returns_manifest(client, setup_episode):
    """GET /stock/candidates returns full manifest."""
    resp = client.get("/api/episodes/ep_test/stock/candidates")
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["episode_id"] == "ep_test"
    assert "ch01" in data["chapters"]


def test_get_candidates_includes_review_info(client, setup_episode, app):
    """Response includes review_task_id and review_status."""
    # Create a review task
    session = app.config["session_factory"]()
    task = ReviewTask(
        episode_id="ep_test",
        stage="stock_images",
        status=ReviewStatus.PENDING.value,
        artifact_paths="[]",
    )
    session.add(task)
    session.commit()
    task_id = task.id
    session.close()

    resp = client.get("/api/episodes/ep_test/stock/candidates")
    data = resp.get_json()
    assert data["review_task_id"] == task_id
    assert data["review_status"] == "pending"


def test_get_candidates_404_no_manifest(client):
    """404 when no candidates_manifest.json."""
    resp = client.get("/api/episodes/nonexistent/stock/candidates")
    assert resp.status_code == 404


def test_pin_image_updates_manifest(client, setup_episode, tmp_path):
    """POST /stock/pin updates selected+locked in manifest."""
    resp = client.post(
        "/api/episodes/ep_test/stock/pin",
        json={"chapter_id": "ch01", "pexels_id": 101, "lock": True},
    )
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "pinned"
    assert data["pexels_id"] == 101

    # Verify manifest updated
    manifest_path = (
        tmp_path / "ep_test" / "images" / "candidates" / "candidates_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    ch01_cands = manifest["chapters"]["ch01"]["candidates"]
    selected = [c for c in ch01_cands if c["selected"]]
    assert len(selected) == 1
    assert selected[0]["pexels_id"] == 101
    assert selected[0]["locked"] is True


def test_pin_image_sets_pinned_by_human(client, setup_episode, tmp_path):
    """pinned_by='human' after pin."""
    client.post(
        "/api/episodes/ep_test/stock/pin",
        json={"chapter_id": "ch01", "pexels_id": 100, "lock": True},
    )

    manifest_path = (
        tmp_path / "ep_test" / "images" / "candidates" / "candidates_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    assert manifest["chapters"]["ch01"]["pinned_by"] == "human"


def test_pin_image_invalid_chapter(client, setup_episode):
    """400 for unknown chapter_id."""
    resp = client.post(
        "/api/episodes/ep_test/stock/pin",
        json={"chapter_id": "ch99", "pexels_id": 100, "lock": True},
    )
    assert resp.status_code == 400


def test_pin_image_invalid_pexels_id(client, setup_episode):
    """400 for unknown pexels_id."""
    resp = client.post(
        "/api/episodes/ep_test/stock/pin",
        json={"chapter_id": "ch01", "pexels_id": 999999, "lock": True},
    )
    assert resp.status_code == 400


@patch("btcedu.core.stock_images.rank_candidates")
def test_rank_endpoint_triggers_ranking(mock_rank, client, setup_episode):
    """POST /stock/rank calls rank_candidates()."""
    from btcedu.core.stock_images import RankResult

    mock_rank.return_value = RankResult(
        episode_id="ep_test",
        chapters_ranked=5,
        chapters_skipped=2,
        total_cost_usd=0.015,
    )

    resp = client.post("/api/episodes/ep_test/stock/rank", json={})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data["status"] == "ranked"
    assert data["chapters_ranked"] == 5


def test_serve_candidate_image(client, setup_episode):
    """GET /stock/candidate-image serves image file."""
    resp = client.get(
        "/api/episodes/ep_test/stock/candidate-image"
        "?chapter=ch01&filename=pexels_100.jpg"
    )
    assert resp.status_code == 200
    assert "image/" in resp.content_type


def test_serve_candidate_path_traversal(client, setup_episode):
    """Path traversal attempt → rejected."""
    resp = client.get(
        "/api/episodes/ep_test/stock/candidate-image"
        "?chapter=..&filename=../../etc/passwd"
    )
    # secure_filename sanitizes, so either 400 or 404
    assert resp.status_code in (400, 404)
