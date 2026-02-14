"""Tests for the btcedu web dashboard API endpoints."""

import json
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

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
        logs_dir=str(tmp_path / "logs"),
    )


@pytest.fixture
def test_db():
    """In-memory SQLite engine + session factory (shared across threads)."""
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
# Health, static assets, and observability
# ---------------------------------------------------------------------------


class TestHealthAndStaticAssets:
    def test_health_endpoint(self, client):
        r = client.get("/api/health")
        assert r.status_code == 200
        data = r.get_json()
        assert data["status"] == "ok"
        assert "time" in data
        assert data["version"] == "0.1.0"

    def test_index_returns_html(self, client):
        r = client.get("/")
        assert r.status_code == 200
        assert b"btcedu" in r.data

    def test_index_uses_relative_static_paths(self, client):
        r = client.get("/")
        html = r.data.decode()
        # Must NOT use absolute /static/ paths (breaks reverse proxy)
        assert 'href="/static/' not in html
        assert 'src="/static/' not in html
        # Must use relative paths
        assert 'href="static/' in html
        assert 'src="static/' in html

    def test_static_css_served(self, client):
        r = client.get("/static/styles.css")
        assert r.status_code == 200
        assert b"--bg" in r.data  # CSS custom property

    def test_static_js_served(self, client):
        r = client.get("/static/app.js")
        assert r.status_code == 200
        assert b"btcedu" in r.data

    def test_js_uses_relative_api_paths(self, client):
        r = client.get("/static/app.js")
        js = r.data.decode()
        # Must NOT use absolute /api paths (breaks reverse proxy)
        assert 'fetch("/api' not in js


# ---------------------------------------------------------------------------
# Episode list + detail
# ---------------------------------------------------------------------------


class TestEpisodeEndpoints:
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
        # v2 file keys should also be present
        assert "outline_v2" in files
        assert "script_v2" in files
        assert "publishing_v2" in files

    def test_episode_detail_includes_cost(self, client):
        data = client.get("/api/episodes/ep001").get_json()
        assert data["cost"]["total_usd"] == pytest.approx(0.045)
        assert data["cost"]["input_tokens"] == 5000
        assert data["cost"]["output_tokens"] == 2000


# ---------------------------------------------------------------------------
# Pipeline action endpoints (now return 202 + job_id)
# ---------------------------------------------------------------------------


class TestPipelineActions:
    def test_detect_endpoint_sync(self, client):
        """Detect stays synchronous."""
        mock_result = MagicMock(found=5, new=2, total=10)
        with patch("btcedu.core.detector.detect_episodes", return_value=mock_result):
            r = client.post("/api/detect")
            assert r.status_code == 200
            data = r.get_json()
            assert data["success"] is True
            assert data["new"] == 2

    def test_download_returns_202(self, client):
        r = client.post("/api/episodes/ep002/download", json={"force": False})
        assert r.status_code == 202
        data = r.get_json()
        assert "job_id" in data
        assert data["state"] in ("queued", "running")

    def test_transcribe_returns_202(self, client):
        r = client.post("/api/episodes/ep002/transcribe", json={"force": False})
        assert r.status_code == 202
        assert "job_id" in r.get_json()

    def test_chunk_returns_202(self, client):
        r = client.post("/api/episodes/ep002/chunk", json={"force": False})
        assert r.status_code == 202
        assert "job_id" in r.get_json()

    def test_generate_returns_202(self, client):
        r = client.post(
            "/api/episodes/ep003/generate",
            json={"force": True, "dry_run": False},
        )
        assert r.status_code == 202
        assert "job_id" in r.get_json()

    def test_run_returns_202(self, client):
        r = client.post("/api/episodes/ep002/run", json={"force": False})
        assert r.status_code == 202
        assert "job_id" in r.get_json()

    def test_refine_returns_202(self, client):
        r = client.post("/api/episodes/ep001/refine", json={"force": False})
        assert r.status_code == 202
        assert "job_id" in r.get_json()

    def test_retry_returns_202(self, client):
        r = client.post("/api/episodes/ep003/retry")
        assert r.status_code == 202
        assert "job_id" in r.get_json()


# ---------------------------------------------------------------------------
# Job lifecycle and logs
# ---------------------------------------------------------------------------


class TestJobsAndLogs:
    def test_job_lifecycle_queued_to_success(self, client, app):
        """Submit a job, poll it, verify it completes."""
        event = threading.Event()

        def mock_download(session, episode_id, settings, force=False):
            event.wait(timeout=5)
            return "/tmp/audio.m4a"

        with patch("btcedu.core.detector.download_episode", side_effect=mock_download):
            r = client.post("/api/episodes/ep002/download", json={})
            assert r.status_code == 202
            job_id = r.get_json()["job_id"]

            # Poll: should be queued or running
            r2 = client.get(f"/api/jobs/{job_id}")
            assert r2.status_code == 200
            assert r2.get_json()["state"] in ("queued", "running")

            # Let the job finish
            event.set()
            time.sleep(0.5)

            # Poll: should be success
            r3 = client.get(f"/api/jobs/{job_id}")
            assert r3.status_code == 200
            data = r3.get_json()
            assert data["state"] == "success"
            assert data["result"]["path"] == "/tmp/audio.m4a"

    def test_job_error_state(self, client, app):
        """Job that raises exception ends in error state."""
        with patch(
            "btcedu.core.detector.download_episode",
            side_effect=RuntimeError("Download failed"),
        ):
            r = client.post("/api/episodes/ep002/download", json={})
            job_id = r.get_json()["job_id"]
            time.sleep(0.5)

            r2 = client.get(f"/api/jobs/{job_id}")
            data = r2.get_json()
            assert data["state"] == "error"
            assert "Download failed" in data["message"]

    def test_active_job_prevents_duplicate(self, client, app):
        """409 when submitting while a job is active for same episode."""
        event = threading.Event()

        def mock_download(session, episode_id, settings, force=False):
            event.wait(timeout=5)
            return "/tmp/audio.m4a"

        with patch("btcedu.core.detector.download_episode", side_effect=mock_download):
            r1 = client.post("/api/episodes/ep002/download", json={})
            assert r1.status_code == 202

            # Second attempt should be blocked
            r2 = client.post("/api/episodes/ep002/transcribe", json={})
            assert r2.status_code == 409
            assert "already active" in r2.get_json()["error"]

            event.set()
            time.sleep(0.5)

    def test_job_not_found(self, client):
        r = client.get("/api/jobs/nonexistent")
        assert r.status_code == 404

    def test_job_includes_episode_status(self, client, app):
        """Job response includes episode_status from DB."""
        with patch(
            "btcedu.core.detector.download_episode",
            return_value="/tmp/audio.m4a",
        ):
            r = client.post("/api/episodes/ep002/download", json={})
            job_id = r.get_json()["job_id"]
            time.sleep(0.5)

            r2 = client.get(f"/api/jobs/{job_id}")
            data = r2.get_json()
            assert "episode_status" in data

    def test_action_log_endpoint(self, client, test_settings):
        """Action log returns lines from per-episode log file."""
        log_dir = Path(test_settings.logs_dir) / "episodes"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "ep001.log").write_text(
            "2026-02-12 10:00:00 [download] Starting...\n2026-02-12 10:00:05 [download] Complete\n",
            encoding="utf-8",
        )

        r = client.get("/api/episodes/ep001/action-log")
        assert r.status_code == 200
        data = r.get_json()
        assert len(data["lines"]) == 2
        assert "Starting" in data["lines"][0]

    def test_action_log_empty(self, client):
        """Episode with no log file returns empty list."""
        r = client.get("/api/episodes/ep999/action-log")
        assert r.status_code == 200
        assert r.get_json()["lines"] == []

    def test_action_log_tail(self, client, test_settings):
        """Tail parameter limits returned lines."""
        log_dir = Path(test_settings.logs_dir) / "episodes"
        log_dir.mkdir(parents=True, exist_ok=True)
        lines = "\n".join(f"line {i}" for i in range(50)) + "\n"
        (log_dir / "ep001.log").write_text(lines, encoding="utf-8")

        r = client.get("/api/episodes/ep001/action-log?tail=5")
        assert r.status_code == 200
        assert len(r.get_json()["lines"]) == 5

    def test_run_all_nothing_to_do_on_refined(self, client, app, seeded_db):
        """Run All on a REFINED episode completes with 'Nothing to do'."""
        # Update ep001 to REFINED status so all stages are skipped
        _, factory = seeded_db
        session = factory()
        ep = session.query(Episode).filter_by(episode_id="ep001").first()
        ep.status = EpisodeStatus.REFINED
        session.commit()
        session.close()

        r = client.post("/api/episodes/ep001/run", json={})
        assert r.status_code == 202
        job_id = r.get_json()["job_id"]
        time.sleep(0.5)

        r2 = client.get(f"/api/jobs/{job_id}")
        data = r2.get_json()
        assert data["state"] == "success"
        assert data["result"]["message"] == "Nothing to do"


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
            json.dumps(report_data),
            encoding="utf-8",
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


# ---------------------------------------------------------------------------
# Batch job progress tracking
# ---------------------------------------------------------------------------


class TestBatchJobProgress:
    def test_calculate_episode_work_new_episode(self, app):
        """NEW episode should require all stages (100 units of work)."""
        from btcedu.models.episode import EpisodeStatus
        from btcedu.web.jobs import STAGE_WEIGHTS

        job_manager = app.config["job_manager"]
        work = job_manager._calculate_episode_work(EpisodeStatus.NEW, force=False)
        expected = sum(STAGE_WEIGHTS.values())
        assert work == expected
        assert work == 100  # 5 + 45 + 10 + 35 + 5

    def test_calculate_episode_work_downloaded(self, app):
        """DOWNLOADED episode should skip download stage."""
        from btcedu.models.episode import EpisodeStatus
        from btcedu.web.jobs import STAGE_WEIGHTS

        job_manager = app.config["job_manager"]
        work = job_manager._calculate_episode_work(EpisodeStatus.DOWNLOADED, force=False)
        expected = sum(STAGE_WEIGHTS.values()) - STAGE_WEIGHTS["download"]
        assert work == expected
        assert work == 95  # 100 - 5

    def test_calculate_episode_work_chunked(self, app):
        """CHUNKED episode should only need generate + refine."""
        from btcedu.models.episode import EpisodeStatus
        from btcedu.web.jobs import STAGE_WEIGHTS

        job_manager = app.config["job_manager"]
        work = job_manager._calculate_episode_work(EpisodeStatus.CHUNKED, force=False)
        expected = STAGE_WEIGHTS["generate"] + STAGE_WEIGHTS["refine"]
        assert work == expected
        assert work == 40  # 35 + 5

    def test_calculate_episode_work_generated(self, app):
        """GENERATED episode should only need refine stage."""
        from btcedu.models.episode import EpisodeStatus
        from btcedu.web.jobs import STAGE_WEIGHTS

        job_manager = app.config["job_manager"]
        work = job_manager._calculate_episode_work(EpisodeStatus.GENERATED, force=False)
        expected = STAGE_WEIGHTS["refine"]
        assert work == expected
        assert work == 5

    def test_calculate_episode_work_force_mode(self, app):
        """Force mode should include all stages regardless of status."""
        from btcedu.models.episode import EpisodeStatus
        from btcedu.web.jobs import STAGE_WEIGHTS

        job_manager = app.config["job_manager"]
        work = job_manager._calculate_episode_work(EpisodeStatus.GENERATED, force=True)
        expected = sum(STAGE_WEIGHTS.values())
        assert work == expected
        assert work == 100

    def test_batch_job_has_progress_fields(self, client):
        """Batch job status should include progress fields."""
        with patch("btcedu.core.pipeline.run_episode_pipeline") as mock_run:
            from btcedu.core.pipeline import PipelineReport

            # Mock successful pipeline run
            mock_run.return_value = PipelineReport(
                episode_id="ep002",
                title="Test",
                success=True,
                total_cost_usd=0.05,
            )

            r = client.post("/api/batch/start", json={})
            assert r.status_code == 202
            batch_id = r.get_json()["batch_id"]
            time.sleep(1.0)

            r2 = client.get(f"/api/batch/{batch_id}")
            assert r2.status_code == 200
            data = r2.get_json()
            assert "progress_pct" in data
            assert "total_work" in data
            assert "completed_work" in data
            assert "current_episode_title" in data
            assert isinstance(data["progress_pct"], int)
            assert data["progress_pct"] >= 0
            assert data["progress_pct"] <= 100

    def test_stage_weights_sum_to_100(self):
        """Stage weights should sum to 100% for a full pipeline."""
        from btcedu.web.jobs import STAGE_WEIGHTS

        total = sum(STAGE_WEIGHTS.values())
        assert total == 100
