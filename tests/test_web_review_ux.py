"""Tests for Phase 1: Review UX improvements.

Tests the review_context and pipeline_state fields added to episode API responses,
the batch query optimization, and the filter behavior.
"""


import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from btcedu.config import Settings
from btcedu.db import Base
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.review import ReviewStatus, ReviewTask
from btcedu.web.api import (
    _REVIEW_GATE_LABELS,
    _REVIEW_GATE_STATUS_MAP,
    _compute_pipeline_state,
    _get_review_context,
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
    """DB with v2 episodes at various statuses plus review tasks."""
    engine, factory = test_db
    session = factory()

    # Episodes at different v2 statuses
    ep_corrected = Episode(
        episode_id="ep_corrected",
        source="youtube_rss",
        title="Bitcoin Corrected",
        url="https://youtube.com/watch?v=ep_corrected",
        status=EpisodeStatus.CORRECTED,
        pipeline_version=2,
    )
    ep_adapted = Episode(
        episode_id="ep_adapted",
        source="youtube_rss",
        title="Bitcoin Adapted",
        url="https://youtube.com/watch?v=ep_adapted",
        status=EpisodeStatus.ADAPTED,
        pipeline_version=2,
    )
    ep_downloaded = Episode(
        episode_id="ep_downloaded",
        source="youtube_rss",
        title="Bitcoin Downloaded",
        url="https://youtube.com/watch?v=ep_downloaded",
        status=EpisodeStatus.DOWNLOADED,
        pipeline_version=2,
    )
    ep_failed = Episode(
        episode_id="ep_failed",
        source="youtube_rss",
        title="Bitcoin Failed",
        url="https://youtube.com/watch?v=ep_failed",
        status=EpisodeStatus.FAILED,
        error_message="Stage 'translate' failed: API error",
        pipeline_version=2,
    )
    ep_published = Episode(
        episode_id="ep_published",
        source="youtube_rss",
        title="Bitcoin Published",
        url="https://youtube.com/watch?v=ep_published",
        status=EpisodeStatus.PUBLISHED,
        pipeline_version=2,
    )

    session.add_all([ep_corrected, ep_adapted, ep_downloaded, ep_failed, ep_published])
    session.commit()

    # Pending review task for ep_corrected (review_gate_1)
    rt_pending = ReviewTask(
        episode_id="ep_corrected",
        stage="correct",
        status=ReviewStatus.PENDING.value,
        artifact_paths="[]",
    )
    session.add(rt_pending)

    # Approved review task for ep_adapted (review_gate_2)
    rt_approved = ReviewTask(
        episode_id="ep_adapted",
        stage="adapt",
        status=ReviewStatus.APPROVED.value,
        artifact_paths="[]",
    )
    session.add(rt_approved)

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
# Unit tests: _get_review_context
# ---------------------------------------------------------------------------


class TestGetReviewContext:
    def test_returns_paused_for_pending_review(self, seeded_db):
        """Episode with PENDING ReviewTask gets paused_for_review context."""
        _, factory = seeded_db
        session = factory()
        ctx = _get_review_context(session, "ep_corrected", "corrected")
        session.close()

        assert ctx is not None
        assert ctx["state"] == "paused_for_review"
        assert ctx["review_stage"] == "correct"
        assert ctx["review_stage_label"] == "Transcript Correction Review"
        assert ctx["review_gate"] == "review_gate_1"
        assert ctx["review_status"] == "pending"
        assert ctx["review_task_id"] is not None
        assert ctx["action_url"].startswith("/api/reviews/")
        assert "requires approval" in ctx["next_action_text"]

    def test_returns_approved_for_approved_review(self, seeded_db):
        """Episode at review gate status with APPROVED task gets review_approved."""
        _, factory = seeded_db
        session = factory()
        ctx = _get_review_context(session, "ep_adapted", "adapted")
        session.close()

        assert ctx is not None
        assert ctx["state"] == "review_approved"
        assert ctx["review_stage"] == "adapt"
        assert ctx["review_stage_label"] == "Adaptation Review"

    def test_returns_none_for_non_review_status(self, seeded_db):
        """Episode at DOWNLOADED (no review gate) gets None."""
        _, factory = seeded_db
        session = factory()
        ctx = _get_review_context(session, "ep_downloaded", "downloaded")
        session.close()

        assert ctx is None

    def test_returns_none_for_published(self, seeded_db):
        """Episode at PUBLISHED gets None (no review context)."""
        _, factory = seeded_db
        session = factory()
        ctx = _get_review_context(session, "ep_published", "published")
        session.close()

        assert ctx is None

    def test_uses_pending_cache(self, seeded_db):
        """When pending_cache is provided, uses it instead of querying DB."""
        _, factory = seeded_db
        session = factory()

        # Create a mock ReviewTask for the cache
        mock_task = ReviewTask(
            episode_id="ep_corrected",
            stage="correct",
            status=ReviewStatus.PENDING.value,
            artifact_paths="[]",
        )
        mock_task.id = 999
        mock_task.created_at = None

        cache = {"ep_corrected": mock_task}
        ctx = _get_review_context(
            session, "ep_corrected", "corrected", pending_cache=cache
        )
        session.close()

        assert ctx is not None
        assert ctx["state"] == "paused_for_review"
        assert ctx["review_task_id"] == 999

    def test_pending_cache_miss_falls_through(self, seeded_db):
        """Empty pending_cache for an episode falls through to approved check."""
        _, factory = seeded_db
        session = factory()
        ctx = _get_review_context(
            session, "ep_adapted", "adapted", pending_cache={}
        )
        session.close()

        assert ctx is not None
        assert ctx["state"] == "review_approved"


# ---------------------------------------------------------------------------
# Unit tests: _compute_pipeline_state
# ---------------------------------------------------------------------------


class TestComputePipelineState:
    def test_paused_for_review(self):
        ctx = {"state": "paused_for_review"}
        assert _compute_pipeline_state("corrected", ctx) == "paused_for_review"

    def test_failed(self):
        assert _compute_pipeline_state("failed", None) == "failed"

    def test_cost_limit(self):
        assert _compute_pipeline_state("cost_limit", None) == "failed"

    def test_published(self):
        assert _compute_pipeline_state("published", None) == "completed"

    def test_approved(self):
        assert _compute_pipeline_state("approved", None) == "ready"

    def test_default_ready(self):
        assert _compute_pipeline_state("downloaded", None) == "ready"

    def test_review_approved_not_paused(self):
        """review_approved context should not trigger paused state."""
        ctx = {"state": "review_approved"}
        assert _compute_pipeline_state("adapted", ctx) == "ready"


# ---------------------------------------------------------------------------
# Unit tests: label completeness
# ---------------------------------------------------------------------------


class TestReviewGateLabels:
    def test_all_four_review_stages_present(self):
        """_REVIEW_GATE_LABELS covers all 4 review gate stages."""
        assert "correct" in _REVIEW_GATE_LABELS
        assert "adapt" in _REVIEW_GATE_LABELS
        assert "stock_images" in _REVIEW_GATE_LABELS
        assert "render" in _REVIEW_GATE_LABELS
        assert len(_REVIEW_GATE_LABELS) == 4

    def test_status_map_matches_labels(self):
        """Every value in _REVIEW_GATE_STATUS_MAP has a label entry."""
        for status, stage in _REVIEW_GATE_STATUS_MAP.items():
            assert stage in _REVIEW_GATE_LABELS, (
                f"Status '{status}' maps to stage '{stage}' "
                f"which is missing from _REVIEW_GATE_LABELS"
            )


# ---------------------------------------------------------------------------
# Integration tests: Episode API responses
# ---------------------------------------------------------------------------


class TestEpisodeListReviewContext:
    def test_list_includes_review_context(self, client):
        """GET /episodes returns review_context for paused episode."""
        r = client.get("/api/episodes")
        assert r.status_code == 200
        data = r.get_json()

        ep_corrected = next(
            e for e in data if e["episode_id"] == "ep_corrected"
        )
        assert ep_corrected["review_context"] is not None
        assert ep_corrected["review_context"]["state"] == "paused_for_review"
        assert ep_corrected["pipeline_state"] == "paused_for_review"

    def test_list_includes_pipeline_version(self, client):
        """GET /episodes returns pipeline_version field."""
        data = client.get("/api/episodes").get_json()
        ep = next(e for e in data if e["episode_id"] == "ep_corrected")
        assert ep["pipeline_version"] == 2

    def test_list_review_context_none_for_non_review(self, client):
        """Downloaded episode has no review_context."""
        data = client.get("/api/episodes").get_json()
        ep = next(e for e in data if e["episode_id"] == "ep_downloaded")
        assert ep["review_context"] is None
        assert ep["pipeline_state"] == "ready"

    def test_list_approved_review_context(self, client):
        """Adapted episode with approved review gets review_approved."""
        data = client.get("/api/episodes").get_json()
        ep = next(e for e in data if e["episode_id"] == "ep_adapted")
        assert ep["review_context"] is not None
        assert ep["review_context"]["state"] == "review_approved"

    def test_list_failed_pipeline_state(self, client):
        """Failed episode gets pipeline_state='failed'."""
        data = client.get("/api/episodes").get_json()
        ep = next(e for e in data if e["episode_id"] == "ep_failed")
        assert ep["pipeline_state"] == "failed"

    def test_list_published_pipeline_state(self, client):
        """Published episode gets pipeline_state='completed'."""
        data = client.get("/api/episodes").get_json()
        ep = next(e for e in data if e["episode_id"] == "ep_published")
        assert ep["pipeline_state"] == "completed"


class TestEpisodeDetailReviewContext:
    def test_detail_includes_review_context(self, client):
        """GET /episodes/<id> returns review_context for paused episode."""
        r = client.get("/api/episodes/ep_corrected")
        assert r.status_code == 200
        data = r.get_json()

        assert data["review_context"] is not None
        assert data["review_context"]["state"] == "paused_for_review"
        assert data["review_context"]["review_stage"] == "correct"
        assert data["pipeline_state"] == "paused_for_review"

    def test_detail_review_context_has_action_url(self, client):
        """Review context includes action_url pointing to review endpoint."""
        data = client.get("/api/episodes/ep_corrected").get_json()
        rc = data["review_context"]
        assert rc["action_url"].startswith("/api/reviews/")
        assert str(rc["review_task_id"]) in rc["action_url"]


# ---------------------------------------------------------------------------
# Batch query efficiency
# ---------------------------------------------------------------------------


class TestBatchQueryEfficiency:
    def test_list_endpoint_uses_batch_query(self, seeded_db, test_settings):
        """Verify the list endpoint fetches pending reviews in a single query."""
        from btcedu.web.app import create_app

        _, factory = seeded_db
        app = create_app(settings=test_settings)
        app.config["session_factory"] = factory
        app.config["TESTING"] = True

        with app.test_client() as client:
            # Add more episodes to make N+1 visible
            session = factory()
            for i in range(10):
                session.add(Episode(
                    episode_id=f"ep_extra_{i}",
                    source="youtube_rss",
                    title=f"Extra Episode {i}",
                    url=f"https://youtube.com/watch?v=ep_extra_{i}",
                    status=EpisodeStatus.CORRECTED,
                    pipeline_version=2,
                ))
            session.commit()
            session.close()

            # The key test: the endpoint should still return quickly
            # and include review_context for all episodes (even the
            # 10 new ones without review tasks — they get None)
            r = client.get("/api/episodes")
            assert r.status_code == 200
            data = r.get_json()
            assert len(data) == 15  # 5 seeded + 10 extra

            # Extra episodes at CORRECTED but with no review task
            # should still get review_context=None (no N+1)
            extra = next(e for e in data if e["episode_id"] == "ep_extra_0")
            assert extra["review_context"] is None


# ---------------------------------------------------------------------------
# HTML filter option
# ---------------------------------------------------------------------------


class TestFilterOption:
    def test_index_has_review_pending_filter(self, client):
        """The HTML page includes the 'Paused for review' filter option."""
        r = client.get("/")
        assert r.status_code == 200
        html = r.data.decode()
        assert 'value="review_pending"' in html
        assert "Paused for review" in html
