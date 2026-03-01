"""Security tests for path traversal vulnerabilities in API endpoints."""

import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from btcedu.config import Settings
from btcedu.db import Base
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.web.app import create_app


@pytest.fixture
def test_settings(tmp_path):
    """Settings with temp directories."""
    return Settings(
        anthropic_api_key="test-key",
        openai_api_key="test-key",
        database_url="sqlite:///:memory:",
        raw_data_dir=str(tmp_path / "raw"),
        transcripts_dir=str(tmp_path / "transcripts"),
        chunks_dir=str(tmp_path / "chunks"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        logs_dir=str(tmp_path / "logs"),
    )


@pytest.fixture
def test_db():
    """In-memory SQLite database."""
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    return engine, factory


@pytest.fixture
def app_client(test_settings, test_db, tmp_path):
    """Flask test client with seeded database."""
    engine, factory = test_db
    session = factory()

    # Create test episode
    episode = Episode(
        episode_id="ep_valid",
        source="youtube_rss",
        title="Valid Episode",
        url="https://youtube.com/watch?v=valid",
        status=EpisodeStatus.RENDERED,
    )
    session.add(episode)
    session.commit()

    # Create test files for valid episode
    outputs_dir = tmp_path / "outputs"
    render_dir = outputs_dir / "ep_valid" / "render"
    render_dir.mkdir(parents=True)

    manifest_data = {"episode_id": "ep_valid", "duration": 120}
    (render_dir / "render_manifest.json").write_text(json.dumps(manifest_data))
    (render_dir / "draft.mp4").write_bytes(b"fake video data")

    tts_dir = outputs_dir / "ep_valid" / "tts"
    tts_dir.mkdir(parents=True)
    tts_manifest = {"episode_id": "ep_valid", "chapters": []}
    (tts_dir / "manifest.json").write_text(json.dumps(tts_manifest))
    (tts_dir / "chapter_01.mp3").write_bytes(b"fake audio data")

    # Create a sensitive file outside outputs_dir to test path traversal
    sensitive_file = tmp_path / "sensitive.txt"
    sensitive_file.write_text("SENSITIVE DATA - SHOULD NOT BE ACCESSIBLE")

    session.close()

    app = create_app(settings=test_settings)
    # Override session factory to use our seeded DB
    app.config["session_factory"] = factory
    app.config["TESTING"] = True
    return app.test_client()


class TestPathTraversalProtection:
    """Test that path traversal attacks are blocked."""

    def test_render_manifest_path_traversal_blocked(self, app_client):
        """Test that path traversal in episode_id is blocked for render manifest."""
        # Try to access file outside outputs directory using path traversal
        # Flask routing itself blocks URLs with .. so we get 500 or 404
        response = app_client.get("/api/episodes/../../sensitive/render")
        assert response.status_code in (404, 500)  # Either routing error or not found

    def test_render_video_path_traversal_blocked(self, app_client):
        """Test that path traversal in episode_id is blocked for render video."""
        response = app_client.get("/api/episodes/../../../sensitive/render/draft.mp4")
        assert response.status_code in (404, 500)  # Either routing error or not found

    def test_tts_manifest_path_traversal_blocked(self, app_client):
        """Test that path traversal in episode_id is blocked for TTS manifest."""
        response = app_client.get("/api/episodes/../../sensitive/tts")
        assert response.status_code in (404, 500)  # Either routing error or not found

    def test_tts_audio_path_traversal_blocked(self, app_client):
        """Test that path traversal in episode_id is blocked for TTS audio."""
        response = app_client.get("/api/episodes/../../../sensitive/tts/chapter_01.mp3")
        assert response.status_code in (404, 500)  # Either routing error or not found

    def test_tts_audio_chapter_path_traversal_blocked(self, app_client):
        """Test that path traversal in chapter_id is blocked."""
        # Try to access files outside the tts directory using chapter_id
        response = app_client.get("/api/episodes/ep_valid/tts/../../sensitive.mp3")
        assert response.status_code in (404, 500)  # Either routing error or not found

    def test_nonexistent_episode_blocked(self, app_client):
        """Test that nonexistent episode IDs are rejected."""
        response = app_client.get("/api/episodes/nonexistent_ep/render")
        assert response.status_code == 404
        assert b"Episode not found" in response.data

    def test_valid_episode_render_manifest_works(self, app_client):
        """Test that valid episode can access render manifest."""
        response = app_client.get("/api/episodes/ep_valid/render")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["episode_id"] == "ep_valid"

    def test_valid_episode_render_video_works(self, app_client):
        """Test that valid episode can access render video."""
        response = app_client.get("/api/episodes/ep_valid/render/draft.mp4")
        assert response.status_code == 200
        assert response.mimetype == "video/mp4"

    def test_valid_episode_tts_manifest_works(self, app_client):
        """Test that valid episode can access TTS manifest."""
        response = app_client.get("/api/episodes/ep_valid/tts")
        assert response.status_code == 200
        data = json.loads(response.data)
        assert data["episode_id"] == "ep_valid"

    def test_valid_episode_tts_audio_works(self, app_client):
        """Test that valid episode can access TTS audio."""
        response = app_client.get("/api/episodes/ep_valid/tts/chapter_01.mp3")
        assert response.status_code == 200
        assert response.mimetype == "audio/mpeg"


class TestPathValidationHelper:
    """Test the _validate_episode_path helper function."""

    def test_validate_episode_path_valid_episode(self, test_settings, test_db, tmp_path):
        """Test that valid episode returns correct path."""
        from btcedu.web.api import _validate_episode_path

        engine, factory = test_db
        session = factory()
        episode = Episode(
            episode_id="ep_test",
            source="test",
            title="Test",
            url="http://test",
            status=EpisodeStatus.NEW,
        )
        session.add(episode)
        session.commit()
        session.close()

        # Create Flask app context for _get_session to work
        from btcedu.web.app import create_app

        app = create_app(settings=test_settings)
        app.config["session_factory"] = factory
        with app.app_context():
            result = _validate_episode_path("ep_test", Path(tmp_path), "render", "test.mp4")
            assert result is not None
            assert result == (tmp_path / "ep_test" / "render" / "test.mp4").resolve()

    def test_validate_episode_path_nonexistent_episode(self, test_settings, test_db, tmp_path):
        """Test that nonexistent episode returns None."""
        from btcedu.web.api import _validate_episode_path

        engine, factory = test_db

        from btcedu.web.app import create_app

        app = create_app(settings=test_settings)
        app.config["session_factory"] = factory
        with app.app_context():
            result = _validate_episode_path("nonexistent", Path(tmp_path), "render", "test.mp4")
            assert result is None

    def test_validate_episode_path_traversal_blocked(self, test_settings, test_db, tmp_path):
        """Test that path traversal attempts return None."""
        from btcedu.web.api import _validate_episode_path

        engine, factory = test_db
        session = factory()
        episode = Episode(
            episode_id="../../../etc",
            source="test",
            title="Test",
            url="http://test",
            status=EpisodeStatus.NEW,
        )
        session.add(episode)
        session.commit()
        session.close()

        from btcedu.web.app import create_app

        app = create_app(settings=test_settings)
        app.config["session_factory"] = factory
        with app.app_context():
            result = _validate_episode_path("../../../etc", Path(tmp_path), "passwd")
            # Should be rejected by os.path.basename sanitization
            assert result is None
