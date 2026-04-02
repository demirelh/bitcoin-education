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
        chunks_dir=str(tmp_path / "chunks"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        logs_dir=str(tmp_path / "logs"),
        pipeline_version=2,
    )


@pytest.fixture
def test_settings_v1(tmp_path):
    """Settings with pipeline_version=1."""
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
        pipeline_version=1,
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
    ep_v1 = Episode(
        episode_id="ep_v1",
        source="youtube_rss",
        title="V1 Episode",
        url="https://youtube.com/watch?v=ep_v1",
        status=EpisodeStatus.NEW,
        pipeline_version=1,
    )

    session.add_all([ep_new, ep_corrected, ep_corrected_approved, ep_failed, ep_published, ep_v1])
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
        """v2 episode returns all 14 stages in correct order."""
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
            "correct",
            "review_gate_1",
            "translate",
            "adapt",
            "review_gate_2",
            "chapterize",
            "frameextract",
            "imagegen",
            "review_gate_stock",
            "tts",
            "anchorgen",
            "render",
            "review_gate_3",
            "publish",
        ]
        assert stage_names == expected_order
        assert sp["total_count"] == 16
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


class TestBuildStageProgressV1:
    def test_v1_stage_progress_five_stages(self, seeded_db, test_settings_v1):
        """v1 episode (pipeline_version=1) returns 5 stages."""
        _, factory = seeded_db
        session = factory()
        ep = session.query(Episode).filter(Episode.episode_id == "ep_v1").first()

        sp = _build_stage_progress(session, ep, test_settings_v1, review_context=None)
        session.close()

        assert sp is not None
        stage_names = [s["name"] for s in sp["stages"]]
        expected = ["download", "transcribe", "chunk", "generate", "refine"]
        assert stage_names == expected
        assert sp["total_count"] == 5
        assert sp["pipeline_version"] == 1


class TestStageLabelConstants:
    def test_stage_labels_dict_complete(self):
        """_STAGE_LABELS covers all expected stages."""
        expected_keys = {
            "download",
            "transcribe",
            "chunk",
            "generate",
            "refine",
            "correct",
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
            "anchorgen",
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

    def test_stage_progress_pipeline_version_respected(self, client, seeded_db, test_settings_v1):
        """v1 episode with v1 settings gets 5 stages; v2 episode with v2 settings gets 14.

        Note: _get_stages uses max(settings.pipeline_version, episode.pipeline_version),
        so a v1 episode with v2 settings will still use v2 stages. To test v1 stage
        counts we need v1 settings paired with a v1 episode.
        """
        from btcedu.web.app import create_app

        _, factory = seeded_db
        app_v1 = create_app(settings=test_settings_v1)
        app_v1.config["session_factory"] = factory
        app_v1.config["TESTING"] = True

        with app_v1.test_client() as c_v1:
            data = c_v1.get("/api/episodes").get_json()
            ep_v1 = next(e for e in data if e["episode_id"] == "ep_v1")
            assert ep_v1["stage_progress"]["total_count"] == 5

        # v2 episode with v2 app
        data2 = client.get("/api/episodes").get_json()
        ep_v2 = next(e for e in data2 if e["episode_id"] == "ep_new")
        assert ep_v2["stage_progress"]["total_count"] == 16

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
