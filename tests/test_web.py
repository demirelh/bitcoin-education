"""Tests for the btcedu web dashboard API endpoints."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.db import Base
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus


@pytest.fixture
def test_settings(tmp_path):
    """Settings with temp directories and no .env loading."""
    return Settings(
        anthropic_api_key="test-key",
        openai_api_key="test-key",
        database_url="sqlite:///:memory:",
        raw_data_dir=str(tmp_path / "raw"),
        transcripts_dir=str(tmp_path / "transcripts"),
        chunks_dir=str(tmp_path / "chunks"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
    )


@pytest.fixture
def test_db():
    """In-memory SQLite engine + session factory."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
            "USING fts5(chunk_id UNINDEXED, episode_id UNINDEXED, text)"
        ))
        conn.commit()
    factory = sessionmaker(bind=engine)
    return engine, factory


@pytest.fixture
def seeded_db(test_db):
    """DB with a few episodes at different statuses."""
    engine, factory = test_db
    session = factory()
    episodes = [
        Episode(
            episode_id="ep001",
            source="youtube_rss",
            title="Bitcoin Basics",
            url="https://youtube.com/watch?v=ep001",
            status=EpisodeStatus.GENERATED,
        ),
        Episode(
            episode_id="ep002",
            source="youtube_rss",
            title="Lightning Network",
            url="https://youtube.com/watch?v=ep002",
            status=EpisodeStatus.NEW,
        ),
        Episode(
            episode_id="ep003",
            source="youtube_rss",
            title="Mining Deep Dive",
            url="https://youtube.com/watch?v=ep003",
            status=EpisodeStatus.CHUNKED,
            error_message="Stage 'generate' failed: API error",
            retry_count=1,
        ),
    ]
    session.add_all(episodes)
    session.commit()

    # Add a pipeline run for cost testing
    run = PipelineRun(
        episode_id=episodes[0].id,
        stage=PipelineStage.GENERATE,
        status=RunStatus.SUCCESS,
        input_tokens=5000,
        output_tokens=2000,
        estimated_cost_usd=0.045,
    )
    session.add(run)
    session.commit()
    session.close()
    return engine, factory


@pytest.fixture
def app(test_settings, seeded_db):
    """Flask test app with mocked DB."""
    from btcedu.web.app import create_app

    _engine, factory = seeded_db
    application = create_app(settings=test_settings)
    # Override session factory to use our seeded DB
    application.config["session_factory"] = factory
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


# ---------------------------------------------------------------------------
# Episode list + detail
# ---------------------------------------------------------------------------

class TestEpisodeEndpoints:
    def test_index_returns_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert b"btcedu" in r.data

    def test_get_episodes_returns_json(self, client):
        r = client.get("/api/episodes")
        assert r.status_code == 200
        data = r.get_json()
        assert isinstance(data, list)
        assert len(data) == 3

    def test_episodes_have_required_fields(self, client):
        data = client.get("/api/episodes").get_json()
        ep = data[0]
        assert "episode_id" in ep
        assert "title" in ep
        assert "status" in ep
        assert "files" in ep
        assert isinstance(ep["files"], dict)

    def test_get_episode_detail(self, client):
        r = client.get("/api/episodes/ep001")
        assert r.status_code == 200
        data = r.get_json()
        assert data["episode_id"] == "ep001"
        assert data["title"] == "Bitcoin Basics"
        assert "cost" in data

    def test_get_episode_not_found(self, client):
        r = client.get("/api/episodes/nonexistent")
        assert r.status_code == 404

    def test_episodes_include_file_presence(self, client):
        data = client.get("/api/episodes").get_json()
        ep = next(e for e in data if e["episode_id"] == "ep001")
        files = ep["files"]
        # All should be False since no real files exist in tmp dirs
        assert files["audio"] is False
        assert files["transcript_raw"] is False

    def test_episode_detail_includes_cost(self, client):
        data = client.get("/api/episodes/ep001").get_json()
        assert data["cost"]["total_usd"] == pytest.approx(0.045)
        assert data["cost"]["input_tokens"] == 5000
        assert data["cost"]["output_tokens"] == 2000


# ---------------------------------------------------------------------------
# Pipeline action endpoints
# ---------------------------------------------------------------------------

class TestPipelineActions:
    def test_detect_endpoint(self, client):
        mock_result = MagicMock(found=5, new=2, total=10)
        with patch("btcedu.core.detector.detect_episodes", return_value=mock_result):
            r = client.post("/api/detect")
            assert r.status_code == 200
            data = r.get_json()
            assert data["success"] is True
            assert data["new"] == 2

    def test_download_endpoint_mocked(self, client):
        with patch("btcedu.core.detector.download_episode", return_value="/tmp/audio.m4a"):
            r = client.post(
                "/api/episodes/ep002/download",
                json={"force": False},
            )
            assert r.status_code == 200
            data = r.get_json()
            assert data["success"] is True
            assert data["path"] == "/tmp/audio.m4a"

    def test_transcribe_endpoint_mocked(self, client):
        with patch("btcedu.core.transcriber.transcribe_episode", return_value="/tmp/transcript.txt"):
            r = client.post(
                "/api/episodes/ep002/transcribe",
                json={"force": False},
            )
            assert r.status_code == 200
            assert r.get_json()["success"] is True

    def test_chunk_endpoint_mocked(self, client):
        with patch("btcedu.core.transcriber.chunk_episode", return_value=12):
            r = client.post(
                "/api/episodes/ep002/chunk",
                json={"force": False},
            )
            assert r.status_code == 200
            data = r.get_json()
            assert data["success"] is True
            assert data["count"] == 12

    def test_generate_endpoint_mocked(self, client):
        mock_result = MagicMock(
            artifacts=["a.md", "b.md"],
            total_cost_usd=0.38,
            total_input_tokens=8000,
            total_output_tokens=3000,
        )
        with patch("btcedu.core.generator.generate_content", return_value=mock_result):
            r = client.post(
                "/api/episodes/ep003/generate",
                json={"force": True, "dry_run": False},
            )
            assert r.status_code == 200
            data = r.get_json()
            assert data["success"] is True
            assert data["artifacts"] == 2
            assert data["cost_usd"] == pytest.approx(0.38)

    def test_run_endpoint_mocked(self, client):
        mock_report = MagicMock(
            success=True,
            total_cost_usd=0.40,
            error=None,
            stages=[],
        )
        with patch("btcedu.core.pipeline.run_episode_pipeline", return_value=mock_report):
            with patch("btcedu.core.pipeline.write_report"):
                r = client.post(
                    "/api/episodes/ep002/run",
                    json={"force": False},
                )
                assert r.status_code == 200
                data = r.get_json()
                assert data["success"] is True

    def test_run_not_found(self, client):
        r = client.post("/api/episodes/nonexistent/run", json={})
        assert r.status_code == 404

    def test_retry_endpoint_mocked(self, client):
        mock_report = MagicMock(success=True, total_cost_usd=0.38, error=None)
        with patch("btcedu.core.pipeline.retry_episode", return_value=mock_report):
            with patch("btcedu.core.pipeline.write_report"):
                r = client.post("/api/episodes/ep003/retry")
                assert r.status_code == 200
                assert r.get_json()["success"] is True

    def test_retry_invalid_returns_400(self, client):
        with patch(
            "btcedu.core.pipeline.retry_episode",
            side_effect=ValueError("Not in failed state"),
        ):
            r = client.post("/api/episodes/ep001/retry")
            assert r.status_code == 400
            assert "error" in r.get_json()


# ---------------------------------------------------------------------------
# File viewer
# ---------------------------------------------------------------------------

class TestFileViewer:
    def test_file_transcript(self, client, test_settings, tmp_path):
        # Create a fake transcript file
        ep_dir = Path(test_settings.transcripts_dir) / "ep001"
        ep_dir.mkdir(parents=True)
        (ep_dir / "transcript.clean.de.txt").write_text("Hallo Welt", encoding="utf-8")

        r = client.get("/api/episodes/ep001/files/transcript_clean")
        assert r.status_code == 200
        data = r.get_json()
        assert data["content"] == "Hallo Welt"

    def test_file_json_pretty_printed(self, client, test_settings):
        ep_dir = Path(test_settings.outputs_dir) / "ep001"
        ep_dir.mkdir(parents=True)
        (ep_dir / "qa.json").write_text('{"q":"What?","a":"Yes"}', encoding="utf-8")

        r = client.get("/api/episodes/ep001/files/qa")
        assert r.status_code == 200
        data = r.get_json()
        # Should be pretty-printed
        assert "\n" in data["content"]

    def test_file_not_found(self, client):
        r = client.get("/api/episodes/ep001/files/transcript_raw")
        assert r.status_code == 404

    def test_file_unknown_type(self, client):
        r = client.get("/api/episodes/ep001/files/unknown_type")
        assert r.status_code == 400

    def test_file_report(self, client, test_settings):
        rep_dir = Path(test_settings.reports_dir) / "ep001"
        rep_dir.mkdir(parents=True)
        report_data = {"success": True, "episode_id": "ep001"}
        (rep_dir / "report_20260101_120000.json").write_text(
            json.dumps(report_data), encoding="utf-8",
        )

        r = client.get("/api/episodes/ep001/files/report")
        assert r.status_code == 200
        content = json.loads(r.get_json()["content"])
        assert content["success"] is True


# ---------------------------------------------------------------------------
# Cost + What's new
# ---------------------------------------------------------------------------

class TestCostAndWhatsNew:
    def test_cost_endpoint(self, client):
        r = client.get("/api/cost")
        assert r.status_code == 200
        data = r.get_json()
        assert "stages" in data
        assert "total_usd" in data
        assert data["total_usd"] == pytest.approx(0.045)

    def test_whats_new_endpoint(self, client):
        r = client.get("/api/whats-new")
        assert r.status_code == 200
        data = r.get_json()
        assert "new_episodes" in data
        assert "failed" in data
        assert "incomplete" in data
        # ep002 is NEW
        new_ids = [e["episode_id"] for e in data["new_episodes"]]
        assert "ep002" in new_ids
        # ep003 has error_message
        failed_ids = [e["episode_id"] for e in data["failed"]]
        assert "ep003" in failed_ids
