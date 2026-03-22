"""Tests for the /api/pipeline-health endpoint."""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from btcedu.config import Settings
from btcedu.db import Base
from btcedu.models.dead_letter import DeadLetterEntry
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus


@pytest.fixture
def test_settings(tmp_path):
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
    engine = create_engine(
        "sqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
                "USING fts5(chunk_id UNINDEXED, episode_id UNINDEXED, text)"
            )
        )
        conn.commit()
    factory = sessionmaker(bind=engine)
    return engine, factory


@pytest.fixture
def seeded_db(test_db):
    """DB with episodes, pipeline runs, and DLQ entries."""
    engine, factory = test_db
    session = factory()

    now = datetime.now(UTC)

    # Create episodes
    ep1 = Episode(
        episode_id="ep001",
        source="youtube_rss",
        title="Bitcoin Basics",
        url="https://youtube.com/watch?v=ep001",
        status=EpisodeStatus.GENERATED,
    )
    ep2 = Episode(
        episode_id="ep002",
        source="youtube_rss",
        title="Lightning Network",
        url="https://youtube.com/watch?v=ep002",
        status=EpisodeStatus.NEW,
        error_message="Stage 'download' failed: connection refused",
        retry_count=5,
    )
    session.add_all([ep1, ep2])
    session.commit()

    # Create pipeline runs with timestamps in the last 24h
    runs = [
        PipelineRun(
            episode_id=ep1.id,
            stage=PipelineStage.DOWNLOAD,
            status=RunStatus.SUCCESS,
            started_at=now - timedelta(hours=2),
            completed_at=now - timedelta(hours=2) + timedelta(seconds=120),
            estimated_cost_usd=0.0,
        ),
        PipelineRun(
            episode_id=ep1.id,
            stage=PipelineStage.TRANSCRIBE,
            status=RunStatus.SUCCESS,
            started_at=now - timedelta(hours=1, minutes=50),
            completed_at=now - timedelta(hours=1, minutes=50) + timedelta(seconds=60),
            estimated_cost_usd=0.0,
        ),
        PipelineRun(
            episode_id=ep1.id,
            stage=PipelineStage.GENERATE,
            status=RunStatus.FAILED,
            started_at=now - timedelta(hours=1),
            completed_at=now - timedelta(minutes=55),
            error_message="[auth_error] 401 Unauthorized — Check .env for API keys.",
            estimated_cost_usd=0.0,
        ),
    ]
    session.add_all(runs)
    session.commit()

    # Create DLQ entry
    dlq = DeadLetterEntry(
        episode_id="ep001",
        stage="generate",
        error_category="auth_error",
        error_message="401 Unauthorized",
        suggestion="Check .env for correct API keys.",
    )
    session.add(dlq)
    session.commit()
    session.close()
    return engine, factory


@pytest.fixture
def app(test_settings, seeded_db):
    from btcedu.web.app import create_app
    from btcedu.web.jobs import JobManager

    _engine, factory = seeded_db
    application = create_app(settings=test_settings)
    application.config["session_factory"] = factory
    application.config["TESTING"] = True
    # Ensure job_manager exists
    if "job_manager" not in application.config:
        application.config["job_manager"] = JobManager(test_settings.logs_dir)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


class TestPipelineHealth:
    """Test /api/pipeline-health endpoint."""

    def test_endpoint_returns_200(self, client):
        resp = client.get("/api/pipeline-health")
        assert resp.status_code == 200

    def test_response_structure(self, client):
        resp = client.get("/api/pipeline-health")
        data = resp.get_json()

        assert "generated_at" in data
        assert "stages" in data
        assert "error_trends" in data
        assert "dead_letter_queue" in data
        assert "episodes" in data

    def test_stage_metrics(self, client):
        resp = client.get("/api/pipeline-health")
        data = resp.get_json()

        stages = data["stages"]
        # We seeded download (success) and transcribe (success) and generate (failed)
        assert len(stages) > 0

        # Check download stage
        if "download" in stages:
            dl = stages["download"]
            assert "success_rate_24h" in dl
            assert "success_rate_7d" in dl
            assert "avg_duration_seconds" in dl
            assert "total_runs_24h" in dl
            assert dl["total_runs_7d"] >= 1

    def test_error_trends(self, client):
        resp = client.get("/api/pipeline-health")
        data = resp.get_json()

        trends = data["error_trends"]
        assert isinstance(trends, list)
        # We have one failed run with [auth_error] in message
        if trends:
            assert "date" in trends[0]
            assert "category" in trends[0]
            assert "count" in trends[0]

    def test_dlq_section(self, client):
        resp = client.get("/api/pipeline-health")
        data = resp.get_json()

        dlq = data["dead_letter_queue"]
        assert dlq["pending"] == 1
        assert len(dlq["entries"]) == 1
        assert dlq["entries"][0]["episode_id"] == "ep001"
        assert dlq["entries"][0]["stage"] == "generate"
        assert dlq["entries"][0]["error_category"] == "auth_error"

    def test_episodes_summary(self, client):
        resp = client.get("/api/pipeline-health")
        data = resp.get_json()

        episodes = data["episodes"]
        assert episodes["total"] == 2
        assert episodes["failed"] == 1  # ep002 has error_message
        # ep002 has retry_count=5 > 3, so it's stuck
        assert len(episodes["stuck_episodes"]) == 1
        assert episodes["stuck_episodes"][0]["episode_id"] == "ep002"

    def test_empty_db(self, test_settings):
        """Should return valid structure even with empty DB."""
        from btcedu.web.app import create_app
        from btcedu.web.jobs import JobManager

        engine = create_engine(
            "sqlite:///:memory:",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        with engine.connect() as conn:
            conn.execute(
                text(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
                    "USING fts5(chunk_id UNINDEXED, episode_id UNINDEXED, text)"
                )
            )
            conn.commit()
        factory = sessionmaker(bind=engine)

        app = create_app(settings=test_settings)
        app.config["session_factory"] = factory
        app.config["TESTING"] = True
        if "job_manager" not in app.config:
            app.config["job_manager"] = JobManager(test_settings.logs_dir)

        with app.test_client() as c:
            resp = c.get("/api/pipeline-health")
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["stages"] == {}
            assert data["error_trends"] == []
            assert data["episodes"]["total"] == 0
