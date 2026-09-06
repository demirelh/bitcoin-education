"""Tests for Phase 2: Pipeline Progress Visualization.

Tests the stage_progress field added to episode API responses,
the _build_stage_progress helper, and the batch duration query.
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from btcedu.config import Settings
from btcedu.db import Base
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus
from btcedu.models.review import ReviewStatus, ReviewTask
from btcedu.web.api import (
    _STAGE_LABELS,
    _STAGE_TO_PIPELINE_STAGE,
    _build_stage_progress,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def test_settings(tmp_path):
    """Settings with temp directories and no .env loading."""
    return Settings(
        anthropic_api_key="test-key",
        openai_api_key="test-key",
        database_url="sqlite:///:memory:",
        raw_data_dir=str(tmp_path / "raw"),
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        logs_dir=str(tmp_path / "logs"),
        pipeline_version=2,
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
    """DB with various episodes for testing stage progress."""
    engine, factory = test_db
    session = factory()

    ep_new = Episode(
        episode_id="ep_new",
        source="youtube_rss",
        title="New Episode",
        url="https://youtube.com/watch?v=ep_new",
        status=EpisodeStatus.NEW,
        pipeline_version=2,
    )
    ep_corrected = Episode(
        episode_id="ep_corrected",
        source="youtube_rss",
        title="Corrected Episode",
        url="https://youtube.com/watch?v=ep_corrected",
        status=EpisodeStatus.CORRECTED,
        pipeline_version=2,
    )
    ep_corrected_approved = Episode(
        episode_id="ep_corrected_approved",
        source="youtube_rss",
        title="Corrected+Approved Episode",
        url="https://youtube.com/watch?v=ep_corrected_approved",
        status=EpisodeStatus.CORRECTED,
        pipeline_version=2,
    )
    ep_failed = Episode(
        episode_id="ep_failed",
        source="youtube_rss",
        title="Failed Episode",
        url="https://youtube.com/watch?v=ep_failed",
        status=EpisodeStatus.FAILED,
        error_message="Stage translate failed",
        pipeline_version=2,
    )
    ep_published = Episode(
        episode_id="ep_published",
        source="youtube_rss",
        title="Published Episode",
        url="https://youtube.com/watch?v=ep_published",
        status=EpisodeStatus.PUBLISHED,
        pipeline_version=2,
    )
    ep_v2_new = Episode(
        episode_id="ep_v2_new",
        source="youtube_rss",
        title="V2 New Episode",
        url="https://youtube.com/watch?v=ep_v2_new",
        status=EpisodeStatus.NEW,
        pipeline_version=2,
    )

    session.add_all(
        [
            ep_new,
            ep_corrected,
            ep_corrected_approved,
            ep_failed,
            ep_published,
            ep_v2_new,
        ]
    )
    session.commit()

    # Pending review for ep_corrected
    rt_pending = ReviewTask(
        episode_id="ep_corrected",
        stage="correct",
        status=ReviewStatus.PENDING.value,
        artifact_paths="[]",
    )
    # Approved review for ep_corrected_approved
    rt_approved = ReviewTask(
        episode_id="ep_corrected_approved",
        stage="correct",
        status=ReviewStatus.APPROVED.value,
        artifact_paths="[]",
    )
    session.add_all([rt_pending, rt_approved])
    session.commit()
    session.close()
    return engine, factory


@pytest.fixture
def app(test_settings, seeded_db):
    """Flask test app with mocked DB."""
    from btcedu.web.app import create_app

    _engine, factory = seeded_db
    application = create_app(settings=test_settings)
    application.config["session_factory"] = factory
    application.config["TESTING"] = True
    return application


@pytest.fixture
def client(app):
    """Flask test client."""
    return app.test_client()


# ---------------------------------------------------------------------------
# Unit tests: _build_stage_progress
# ---------------------------------------------------------------------------


class TestBuildStageProgressV2:
    def test_v2_stage_progress_all_stages_present(self, seeded_db, test_settings):
        """v2 episode returns all 20 stages in correct order."""
        _, factory = seeded_db
        session = factory()
        ep = session.query(Episode).filter(Episode.episode_id == "ep_new").first()

        sp = _build_stage_progress(session, ep, test_settings, review_context=None)
        session.close()

        assert sp is not None
        stage_names = [s["name"] for s in sp["stages"]]
        expected_order = [
            "download",
            "transcribe",
            "transcript_analyze",
            "transcript_verify",
            "correct",
            "transcript_qa",
            "review_gate_transcript_qa",
            "review_gate_1",
            "translate",
            "adapt",
            "review_gate_2",
            "chapterize",
            "frameextract",
            "imagegen",
            "review_gate_stock",
            "tts",
            "sceneplan",
            "anchorgen",
            "review_gate_anchor",
            "render",
            "review_gate_3",
            "publish",
        ]
        assert stage_names == expected_order
        assert sp["total_count"] == 22
        assert sp["pipeline_version"] == 2

    def test_new_episode_all_pending_except_first(self, seeded_db, test_settings):
        """NEW episode: download=active, rest=pending."""
        _, factory = seeded_db
        session = factory()
        ep = session.query(Episode).filter(Episode.episode_id == "ep_new").first()

        sp = _build_stage_progress(session, ep, test_settings, review_context=None)
        session.close()

        states = {s["name"]: s["state"] for s in sp["stages"]}
        assert states["download"] == "active"
        # all others pending
        for name, state in states.items():
            if name != "download":
                assert state == "pending", f"Expected pending for {name}, got {state}"

        assert sp["current_stage"] == "download"

    def test_corrected_episode_marks_done_stages(self, seeded_db, test_settings):
        """CORRECTED: download/transcribe/correct=done, review_gate_1=active."""
        _, factory = seeded_db
        session = factory()
        ep = session.query(Episode).filter(Episode.episode_id == "ep_corrected").first()

        sp = _build_stage_progress(session, ep, test_settings, review_context=None)
        session.close()

        states = {s["name"]: s["state"] for s in sp["stages"]}
        assert states["download"] == "done"
        assert states["transcribe"] == "done"
        assert states["correct"] == "done"
        # review_gate_1 would be "active" (run)
        assert states["review_gate_1"] == "active"

    def test_paused_review_gate_state(self, seeded_db, test_settings):
        """Episode with pending ReviewTask → review_gate_1 = 'paused'."""
        _, factory = seeded_db
        session = factory()
        ep = session.query(Episode).filter(Episode.episode_id == "ep_corrected").first()

        review_context = {
            "state": "paused_for_review",
            "review_gate": "review_gate_1",
        }
        sp = _build_stage_progress(session, ep, test_settings, review_context=review_context)
        session.close()

        states = {s["name"]: s["state"] for s in sp["stages"]}
        assert states["review_gate_1"] == "paused"
        assert sp["current_stage"] == "review_gate_1"

    def test_approved_review_gate_state(self, seeded_db, test_settings):
        """Episode with approved review context → review_gate_1 = 'done'."""
        _, factory = seeded_db
        session = factory()
        ep = session.query(Episode).filter(Episode.episode_id == "ep_corrected").first()

        review_context = {
            "state": "review_approved",
            "review_gate": "review_gate_1",
        }
        sp = _build_stage_progress(session, ep, test_settings, review_context=review_context)
        session.close()

        states = {s["name"]: s["state"] for s in sp["stages"]}
        assert states["review_gate_1"] == "done"

    def test_failed_episode_marks_failed_stage(self, seeded_db, test_settings):
        """FAILED episode: first active stage becomes 'failed'."""
        _, factory = seeded_db
        session = factory()
        ep = session.query(Episode).filter(Episode.episode_id == "ep_failed").first()

        sp = _build_stage_progress(session, ep, test_settings, review_context=None)
        session.close()

        states = {s["name"]: s["state"] for s in sp["stages"]}
        # At FAILED status with no successful stages, download should be failed
        # because resolve_pipeline_plan returns "run" for the current position
        failed_stages = [name for name, state in states.items() if state == "failed"]
        assert len(failed_stages) == 1
        assert sp["current_stage"] == failed_stages[0]

        # All stages after the failed one should be pending
        failed_idx = next(i for i, s in enumerate(sp["stages"]) if s["state"] == "failed")
        for s in sp["stages"][failed_idx + 1 :]:
            assert s["state"] == "pending", (
                f"Expected pending after failed stage, got {s['state']} for {s['name']}"
            )

    def test_published_episode_all_done(self, seeded_db, test_settings):
        """PUBLISHED: all stages done."""
        _, factory = seeded_db
        session = factory()
        ep = session.query(Episode).filter(Episode.episode_id == "ep_published").first()

        sp = _build_stage_progress(session, ep, test_settings, review_context=None)
        session.close()

        for s in sp["stages"]:
            assert s["state"] == "done", f"Expected done for {s['name']}, got {s['state']}"
        assert sp["current_stage"] is None
        assert sp["completed_count"] == sp["total_count"]

    def test_duration_attached_from_pipeline_run(self, seeded_db, test_settings):
        """Stage with matching PipelineRun gets duration_seconds and cost_usd."""
        _, factory = seeded_db
        session = factory()
        ep = session.query(Episode).filter(Episode.episode_id == "ep_published").first()

        now = datetime.now(UTC)
        run = PipelineRun(
            episode_id=ep.id,
            stage=PipelineStage.DOWNLOAD,
            status=RunStatus.SUCCESS,
            started_at=now - timedelta(seconds=12),
            completed_at=now,
            estimated_cost_usd=0.0,
        )
        session.add(run)
        session.commit()

        sp = _build_stage_progress(session, ep, test_settings, review_context=None)
        session.close()

        download_stage = next(s for s in sp["stages"] if s["name"] == "download")
        assert download_stage["duration_seconds"] is not None
        assert download_stage["duration_seconds"] >= 11.0  # allow small float variance
        assert download_stage["started_at"] == run.started_at.replace(tzinfo=UTC).isoformat()
        assert download_stage["completed_at"] == run.completed_at.replace(tzinfo=UTC).isoformat()
        assert download_stage["run_status"] == "success"
        assert sp["pipeline_started_at"] == run.started_at.replace(tzinfo=UTC).isoformat()

    def test_active_stage_exposes_start_time(self, seeded_db, test_settings):
        _, factory = seeded_db
        session = factory()
        ep = session.query(Episode).filter(Episode.episode_id == "ep_new").first()
        started = datetime.now(UTC) - timedelta(minutes=3)
        run = PipelineRun(
            episode_id=ep.id,
            stage=PipelineStage.DOWNLOAD,
            status=RunStatus.RUNNING,
            started_at=started,
        )
        session.add(run)
        session.commit()

        sp = _build_stage_progress(session, ep, test_settings, review_context=None)
        session.close()

        download_stage = next(s for s in sp["stages"] if s["name"] == "download")
        assert download_stage["started_at"] == started.isoformat()
        assert download_stage["completed_at"] is None
        assert download_stage["run_status"] == "running"

    def test_retried_stage_shows_full_history(self, seeded_db, test_settings):
        _, factory = seeded_db
        session = factory()
        ep = session.query(Episode).filter(Episode.episode_id == "ep_published").first()
        first_start = datetime.now(UTC) - timedelta(minutes=20)
        first_end = first_start + timedelta(minutes=4)
        second_start = first_end + timedelta(minutes=6)
        second_end = second_start + timedelta(minutes=3)
        session.add_all(
            [
                PipelineRun(
                    episode_id=ep.id,
                    stage=PipelineStage.ADAPT,
                    status=RunStatus.SUCCESS,
                    started_at=first_start,
                    completed_at=first_end,
                    estimated_cost_usd=0.1,
                ),
                PipelineRun(
                    episode_id=ep.id,
                    stage=PipelineStage.ADAPT,
                    status=RunStatus.SUCCESS,
                    started_at=second_start,
                    completed_at=second_end,
                    estimated_cost_usd=0.2,
                ),
            ]
        )
        session.commit()

        sp = _build_stage_progress(session, ep, test_settings, review_context=None)
        session.close()

        adapt = next(s for s in sp["stages"] if s["name"] == "adapt")
        assert adapt["started_at"] == first_start.isoformat()
        assert adapt["completed_at"] == second_end.isoformat()
        assert adapt["duration_seconds"] == pytest.approx(7 * 60)
        assert adapt["cost_usd"] == pytest.approx(0.3)
        assert adapt["attempt_count"] == 2

    def test_gate_stages_have_no_duration(self, seeded_db, test_settings):
        """Review gates always have duration_seconds=None."""
        _, factory = seeded_db
        session = factory()
        ep = session.query(Episode).filter(Episode.episode_id == "ep_published").first()

        sp = _build_stage_progress(session, ep, test_settings, review_context=None)
        session.close()

        for s in sp["stages"]:
            if s["is_gate"]:
                assert s["duration_seconds"] is None, f"Gate {s['name']} should have no duration"
                assert s["cost_usd"] is None, f"Gate {s['name']} should have no cost"

    def test_stage_labels_correct(self, seeded_db, test_settings):
        """Every stage has a non-empty label."""
        _, factory = seeded_db
        session = factory()
        ep = session.query(Episode).filter(Episode.episode_id == "ep_new").first()

        sp = _build_stage_progress(session, ep, test_settings, review_context=None)
        session.close()

        for s in sp["stages"]:
            assert s["label"], f"Stage {s['name']} has empty label"
            assert isinstance(s["label"], str)

    def test_completed_count_and_total(self, seeded_db, test_settings):
        """completed_count and total_count match stage states."""
        _, factory = seeded_db
        session = factory()
        ep = session.query(Episode).filter(Episode.episode_id == "ep_published").first()

        sp = _build_stage_progress(session, ep, test_settings, review_context=None)
        session.close()

        manual_done = sum(1 for s in sp["stages"] if s["state"] in ("done", "skipped"))
        assert sp["completed_count"] == manual_done
        assert sp["total_count"] == len(sp["stages"])


class TestStageLabelConstants:
    def test_stage_labels_dict_complete(self):
        """_STAGE_LABELS covers all expected stages."""
        expected_keys = {
            "download",
            "transcribe",
            "transcript_analyze",
            "transcript_verify",
            "correct",
            "transcript_qa",
            "review_gate_transcript_qa",
            "review_gate_1",
            "segment",
            "translate",
            "adapt",
            "review_gate_2",
            "review_gate_translate",
            "chapterize",
            "frameextract",
            "imagegen",
            "review_gate_stock",
            "tts",
            "sceneplan",
            "anchorgen",
            "review_gate_anchor",
            "render",
            "review_gate_3",
            "publish",
        }
        assert set(_STAGE_LABELS.keys()) == expected_keys

    def test_stage_to_pipeline_stage_no_gates(self):
        """_STAGE_TO_PIPELINE_STAGE does not include review gates."""
        for key in _STAGE_TO_PIPELINE_STAGE:
            assert not key.startswith("review_gate"), (
                f"Review gate {key} should not be in _STAGE_TO_PIPELINE_STAGE"
            )

    def test_stage_to_pipeline_stage_all_map_to_enum(self):
        """All values in _STAGE_TO_PIPELINE_STAGE are PipelineStage enum members."""
        for name, ps in _STAGE_TO_PIPELINE_STAGE.items():
            assert isinstance(ps, PipelineStage), (
                f"Stage {name} maps to {ps!r}, expected PipelineStage"
            )


# ---------------------------------------------------------------------------
# Integration tests: Episode API responses
# ---------------------------------------------------------------------------


class TestEpisodeDetailIncludesStageProgress:
    def test_episode_detail_includes_stage_progress(self, client):
        """GET /episodes/<id> returns stage_progress dict."""
        r = client.get("/api/episodes/ep_new")
        assert r.status_code == 200
        data = r.get_json()

        assert "stage_progress" in data
        sp = data["stage_progress"]
        assert sp is not None
        assert "stages" in sp
        assert "total_count" in sp
        assert "completed_count" in sp
        assert "pipeline_version" in sp
        assert "current_stage" in sp

    def test_episode_detail_stage_list_not_empty(self, client):
        """stage_progress.stages is a non-empty list."""
        data = client.get("/api/episodes/ep_new").get_json()
        assert len(data["stage_progress"]["stages"]) > 0

    def test_episode_detail_each_stage_has_required_keys(self, client):
        """Each stage entry has name, label, state, is_gate, duration_seconds, cost_usd."""
        data = client.get("/api/episodes/ep_new").get_json()
        for s in data["stage_progress"]["stages"]:
            assert "name" in s
            assert "label" in s
            assert "state" in s
            assert "is_gate" in s
            assert "duration_seconds" in s
            assert "cost_usd" in s


class TestEpisodeListIncludesStageProgress:
    def test_episode_list_includes_stage_progress(self, client):
        """GET /episodes returns stage_progress for each episode."""
        r = client.get("/api/episodes")
        assert r.status_code == 200
        data = r.get_json()

        assert len(data) > 0
        for ep in data:
            assert "stage_progress" in ep, f"Episode {ep['episode_id']} missing stage_progress"
            assert ep["stage_progress"] is not None

    def test_stage_progress_uses_v2_stages(self, client):
        """Episodes use the v2 stage list."""
        data2 = client.get("/api/episodes").get_json()
        ep_v2 = next(e for e in data2 if e["episode_id"] == "ep_new")
        assert ep_v2["stage_progress"]["total_count"] == 22

    def test_paused_review_reflected_in_stage_progress(self, client):
        """Paused episode has review gate showing 'paused' in stage_progress."""
        data = client.get("/api/episodes").get_json()
        ep = next(e for e in data if e["episode_id"] == "ep_corrected")

        sp = ep["stage_progress"]
        gate = next(s for s in sp["stages"] if s["name"] == "review_gate_1")
        assert gate["state"] == "paused"
        assert sp["current_stage"] == "review_gate_1"


class TestBatchDurationQueryEfficiency:
    def test_batch_duration_query_efficiency(self, seeded_db, test_settings):
        """10+ episodes returns stage_progress for all without N+1."""
        from btcedu.web.app import create_app

        _, factory = seeded_db
        app = create_app(settings=test_settings)
        app.config["session_factory"] = factory
        app.config["TESTING"] = True

        with app.test_client() as c:
            # Add 10 more episodes
            session = factory()
            for i in range(10):
                session.add(
                    Episode(
                        episode_id=f"ep_batch_{i}",
                        source="youtube_rss",
                        title=f"Batch Episode {i}",
                        url=f"https://youtube.com/watch?v=ep_batch_{i}",
                        status=EpisodeStatus.NEW,
                        pipeline_version=2,
                    )
                )
            session.commit()
            session.close()

            r = c.get("/api/episodes")
            assert r.status_code == 200
            data = r.get_json()

            # All episodes should have stage_progress
            for ep in data:
                assert ep["stage_progress"] is not None, (
                    f"Episode {ep['episode_id']} missing stage_progress"
                )

    def test_duration_in_stage_progress_when_pipeline_run_exists(self, seeded_db, test_settings):
        """PipelineRun duration is reflected in stage_progress from batch query."""
        _, factory = seeded_db
        session = factory()
        ep = session.query(Episode).filter(Episode.episode_id == "ep_published").first()

        now = datetime.now(UTC)
        run = PipelineRun(
            episode_id=ep.id,
            stage=PipelineStage.TRANSCRIBE,
            status=RunStatus.SUCCESS,
            started_at=now - timedelta(seconds=45),
            completed_at=now,
            estimated_cost_usd=0.02,
        )
        session.add(run)
        session.commit()
        session.close()

        from btcedu.web.app import create_app

        app = create_app(settings=test_settings)
        app.config["session_factory"] = factory
        app.config["TESTING"] = True

        with app.test_client() as c:
            data = c.get("/api/episodes").get_json()
            ep_data = next(e for e in data if e["episode_id"] == "ep_published")
            sp = ep_data["stage_progress"]
            transcribe_stage = next(s for s in sp["stages"] if s["name"] == "transcribe")
            assert transcribe_stage["duration_seconds"] is not None
            assert transcribe_stage["duration_seconds"] >= 44.0
            assert transcribe_stage["cost_usd"] == pytest.approx(0.02, abs=0.001)


# ---------------------------------------------------------------------------
# Render progress endpoint + renderer progress file
# ---------------------------------------------------------------------------


class TestRenderProgressEndpoint:
    def test_idle_when_no_progress_file(self, client):
        resp = client.get("/api/episodes/ep_new/render/progress")
        assert resp.status_code == 200
        assert resp.get_json() == {"stage": "idle"}

    def test_unknown_episode_returns_404(self, client):
        resp = client.get("/api/episodes/does_not_exist/render/progress")
        assert resp.status_code == 404

    def test_returns_written_progress(self, client, test_settings):
        import json
        from pathlib import Path

        render_dir = Path(test_settings.outputs_dir) / "ep_new" / "render"
        render_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "stage": "segment_done",
            "current": 3,
            "total": 8,
            "chapter_title": "Iran-USA",
            "progress_pct": 37,
        }
        (render_dir / "progress.json").write_text(json.dumps(payload), encoding="utf-8")

        resp = client.get("/api/episodes/ep_new/render/progress")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["stage"] == "segment_done"
        assert body["current"] == 3
        assert body["total"] == 8
        assert body["progress_pct"] == 37


class TestWriteRenderProgress:
    def test_writes_and_overwrites_atomically(self, tmp_path):
        import json

        from btcedu.core.renderer import _write_render_progress

        render_dir = tmp_path / "render"
        _write_render_progress(render_dir, {"stage": "segment_start", "progress_pct": 0})
        data = json.loads((render_dir / "progress.json").read_text())
        assert data["stage"] == "segment_start"
        assert "updated_at" in data
        assert not (render_dir / "progress.json.tmp").exists()

        _write_render_progress(render_dir, {"stage": "done", "progress_pct": 100})
        data = json.loads((render_dir / "progress.json").read_text())
        assert data["stage"] == "done"
        assert data["progress_pct"] == 100


class TestQaEndpoint:
    def test_qa_endpoint_404_when_absent(self, client):
        r = client.get("/api/episodes/ep_new/qa")
        assert r.status_code == 404
        assert "QA" in r.get_json()["error"]

    def test_qa_endpoint_returns_review(self, client, test_settings):
        import json as _json
        from pathlib import Path as _Path

        qa_dir = _Path(test_settings.outputs_dir) / "ep_new"
        qa_dir.mkdir(parents=True, exist_ok=True)
        (qa_dir / "qa_review.json").write_text(
            _json.dumps(
                {
                    "overall_score": 7.8,
                    "summary": "Solide",
                    "model": "gpt-5.6-sol",
                    "stories": [],
                    "missing_content": [],
                    "hallucinations": ["erfundene Aussage"],
                    "neutralization_gaps": [],
                    "top_fixes": ["Wettersatz korrigieren"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        r = client.get("/api/episodes/ep_new/qa")
        assert r.status_code == 200
        qa = r.get_json()["qa_review"]
        assert qa["overall_score"] == 7.8
        assert qa["model"] == "gpt-5.6-sol"
        assert "Wettersatz korrigieren" in qa["top_fixes"]
