"""Tests for Phase 3 analytics API endpoints."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from btcedu.config import Settings
from btcedu.db import Base
from btcedu.models.dead_letter import DeadLetterEntry  # noqa: F401
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus
from btcedu.models.media_asset import Base as MediaBase
from btcedu.models.prompt_version import PromptVersion  # noqa: F401
from btcedu.web.app import create_app
from btcedu.web.jobs import JobManager


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
    MediaBase.metadata.create_all(engine)
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
    """DB with episodes and pipeline runs for analytics tests."""
    engine, factory = test_db
    session = factory()

    # Create test episodes
    ep1 = Episode(
        episode_id="ep_analytics_1",
        source="youtube_rss",
        title="Analytics Test 1",
        url="https://example.com/1",
        detected_at=datetime(2026, 3, 1, tzinfo=UTC),
        status=EpisodeStatus.COMPLETED,
        pipeline_version=2,
    )
    ep2 = Episode(
        episode_id="ep_analytics_2",
        source="youtube_rss",
        title="Analytics Test 2",
        url="https://example.com/2",
        detected_at=datetime(2026, 3, 2, tzinfo=UTC),
        status=EpisodeStatus.TRANSLATED,
        pipeline_version=2,
    )
    session.add_all([ep1, ep2])
    session.flush()  # populate ep1.id and ep2.id

    # Create pipeline runs with costs
    runs = [
        PipelineRun(
            episode_id=ep1.id,
            stage=PipelineStage.TRANSCRIBE,
            status=RunStatus.SUCCESS,
            started_at=datetime(2026, 3, 1, 10, 0, tzinfo=UTC),
            completed_at=datetime(2026, 3, 1, 10, 5, tzinfo=UTC),
            input_tokens=1000,
            output_tokens=500,
            estimated_cost_usd=0.05,
        ),
        PipelineRun(
            episode_id=ep1.id,
            stage=PipelineStage.CORRECT,
            status=RunStatus.SUCCESS,
            started_at=datetime(2026, 3, 1, 10, 5, tzinfo=UTC),
            completed_at=datetime(2026, 3, 1, 10, 10, tzinfo=UTC),
            input_tokens=2000,
            output_tokens=1000,
            estimated_cost_usd=0.10,
        ),
        PipelineRun(
            episode_id=ep1.id,
            stage=PipelineStage.TRANSLATE,
            status=RunStatus.FAILED,
            started_at=datetime(2026, 3, 1, 10, 10, tzinfo=UTC),
            completed_at=datetime(2026, 3, 1, 10, 11, tzinfo=UTC),
            error_message="API error",
            estimated_cost_usd=0.01,
        ),
        PipelineRun(
            episode_id=ep1.id,
            stage=PipelineStage.TRANSLATE,
            status=RunStatus.SUCCESS,
            started_at=datetime(2026, 3, 1, 10, 12, tzinfo=UTC),
            completed_at=datetime(2026, 3, 1, 10, 15, tzinfo=UTC),
            input_tokens=3000,
            output_tokens=2000,
            estimated_cost_usd=0.15,
        ),
        PipelineRun(
            episode_id=ep2.id,
            stage=PipelineStage.TRANSCRIBE,
            status=RunStatus.SUCCESS,
            started_at=datetime(2026, 3, 2, 10, 0, tzinfo=UTC),
            completed_at=datetime(2026, 3, 2, 10, 5, tzinfo=UTC),
            input_tokens=800,
            output_tokens=400,
            estimated_cost_usd=0.04,
        ),
        PipelineRun(
            episode_id=ep2.id,
            stage=PipelineStage.CORRECT,
            status=RunStatus.FAILED,
            started_at=datetime(2026, 3, 2, 10, 5, tzinfo=UTC),
            completed_at=datetime(2026, 3, 2, 10, 6, tzinfo=UTC),
            error_message="timeout",
            estimated_cost_usd=0.0,
        ),
    ]
    session.add_all(runs)
    session.commit()
    session.close()

    return engine, factory


@pytest.fixture
def app(test_settings, seeded_db):
    _engine, factory = seeded_db
    application = create_app(settings=test_settings)
    application.config["session_factory"] = factory
    application.config["TESTING"] = True
    if "job_manager" not in application.config:
        application.config["job_manager"] = JobManager(test_settings.logs_dir)
    return application


@pytest.fixture
def client(app):
    return app.test_client()


class TestThroughputEndpoint:
    def test_returns_200(self, client):
        resp = client.get("/api/analytics/throughput")
        assert resp.status_code == 200

    def test_returns_daily_data(self, client):
        data = client.get("/api/analytics/throughput").get_json()
        assert "days" in data
        # Should have data for at least one day
        assert len(data["days"]) >= 1

    def test_day_has_required_fields(self, client):
        data = client.get("/api/analytics/throughput").get_json()
        for day in data["days"]:
            assert "date" in day
            assert "episodes" in day
            assert "cost_usd" in day

    def test_cost_is_rounded(self, client):
        data = client.get("/api/analytics/throughput").get_json()
        for day in data["days"]:
            cost_str = str(day["cost_usd"])
            if "." in cost_str:
                decimals = len(cost_str.split(".")[1])
                assert decimals <= 4


class TestErrorRateEndpoint:
    def test_returns_200(self, client):
        resp = client.get("/api/analytics/error-rate")
        assert resp.status_code == 200

    def test_returns_stage_data(self, client):
        data = client.get("/api/analytics/error-rate").get_json()
        assert "stages" in data
        assert len(data["stages"]) >= 1

    def test_stage_has_required_fields(self, client):
        data = client.get("/api/analytics/error-rate").get_json()
        for stage in data["stages"]:
            assert "stage" in stage
            assert "success" in stage
            assert "failed" in stage
            assert "error_rate" in stage

    def test_translate_has_one_failure(self, client):
        data = client.get("/api/analytics/error-rate").get_json()
        translate = next((s for s in data["stages"] if s["stage"] == "translate"), None)
        assert translate is not None
        assert translate["failed"] == 1
        assert translate["success"] == 1
        assert translate["error_rate"] == 0.5

    def test_transcribe_has_no_failures(self, client):
        data = client.get("/api/analytics/error-rate").get_json()
        transcribe = next((s for s in data["stages"] if s["stage"] == "transcribe"), None)
        assert transcribe is not None
        assert transcribe["failed"] == 0
        assert transcribe["error_rate"] == 0


class TestProviderCostEndpoint:
    def test_returns_200(self, client):
        resp = client.get("/api/analytics/provider-cost")
        assert resp.status_code == 200

    def test_returns_provider_data(self, client):
        data = client.get("/api/analytics/provider-cost").get_json()
        assert "providers" in data
        assert len(data["providers"]) >= 1

    def test_provider_has_required_fields(self, client):
        data = client.get("/api/analytics/provider-cost").get_json()
        for p in data["providers"]:
            assert "provider" in p
            assert "runs" in p
            assert "cost_usd" in p

    def test_anthropic_provider_aggregated(self, client):
        data = client.get("/api/analytics/provider-cost").get_json()
        anthropic = next((p for p in data["providers"] if p["provider"] == "Anthropic"), None)
        assert anthropic is not None
        # correct + translate stages both map to Anthropic
        assert anthropic["runs"] >= 2

    def test_providers_sorted_by_cost_desc(self, client):
        data = client.get("/api/analytics/provider-cost").get_json()
        costs = [p["cost_usd"] for p in data["providers"]]
        assert costs == sorted(costs, reverse=True)
