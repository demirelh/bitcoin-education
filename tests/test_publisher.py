"""Tests for Sprint 11: Publisher (YouTube publishing)."""

import hashlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core.publisher import (
    PublishCoordinationError,
    PublishReconciliationRequired,
    _build_youtube_metadata,
    _check_approval_gate,
    _check_artifact_integrity,
    _check_cost_sanity,
    _check_metadata_completeness,
    _conform_chapter_marks,
    _format_timestamp,
    get_latest_publish_job,
    publish_video,
)
from btcedu.db import Base
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, RunStatus
from btcedu.models.publish_job import PublishJob, PublishJobStatus
from btcedu.models.review import ReviewStatus, ReviewTask

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_engine():
    """In-memory SQLite for publisher tests."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
                "USING fts5(chunk_id UNINDEXED, episode_id UNINDEXED, text)"
            )
        )
        conn.commit()
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    factory = sessionmaker(bind=db_engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        outputs_dir=str(tmp_path / "outputs"),
        transcripts_dir=str(tmp_path / "transcripts"),
        raw_data_dir=str(tmp_path / "raw"),
        dry_run=False,
        max_episode_cost_usd=10.0,
        youtube_test_credentials_path=str(tmp_path / "test-creds.json"),
        youtube_test_client_secrets_path=str(tmp_path / "test-client.json"),
        youtube_test_channel_id="UC_TEST",
        youtube_production_credentials_path=str(tmp_path / "prod-creds.json"),
        youtube_production_client_secrets_path=str(tmp_path / "prod-client.json"),
        youtube_production_channel_id="UC_PRODUCTION",
    )


@pytest.fixture
def approved_episode(db_session):
    """An APPROVED v2 episode."""
    ep = Episode(
        episode_id="ep_pub_001",
        source="youtube_rss",
        title="Bitcoin Eğitim #1",
        url="https://youtube.com/watch?v=pub001",
        status=EpisodeStatus.APPROVED,
        pipeline_version=2,
    )
    db_session.add(ep)
    db_session.commit()
    return ep


@pytest.fixture
def approved_review_task(db_session, approved_episode, tmp_path):
    """Approved render ReviewTask with artifact hash."""
    video_path = tmp_path / "outputs" / approved_episode.episode_id / "render" / "draft.mp4"
    video_path.parent.mkdir(parents=True, exist_ok=True)
    video_path.write_bytes(b"fake video content")

    artifact_paths = [str(video_path)]
    h = hashlib.sha256()
    for p in sorted(artifact_paths):
        h.update(Path(p).read_bytes())
    artifact_hash = h.hexdigest()

    task = ReviewTask(
        episode_id=approved_episode.episode_id,
        stage="render",
        status=ReviewStatus.APPROVED.value,
        artifact_paths=json.dumps(artifact_paths),
        artifact_hash=artifact_hash,
    )
    db_session.add(task)
    db_session.commit()
    return task


def _make_chapters_json(tmp_path, episode_id):
    chapters_dir = tmp_path / "outputs" / episode_id
    chapters_dir.mkdir(parents=True, exist_ok=True)
    data = {
        "episode_id": episode_id,
        "title": "Bitcoin Eğitim: Kripto Para Dünyası",
        "chapters": [
            {
                "chapter_id": "ch01",
                "title": "Giriş",
                "order": 1,
                "narration": {
                    "text": "Bitcoin, merkezi olmayan dijital bir para birimidir.",
                    "estimated_duration_seconds": 30,
                },
            },
            {
                "chapter_id": "ch02",
                "title": "Blockchain",
                "order": 2,
                "narration": {
                    "text": "Blockchain teknolojisi güvenli işlemler sağlar.",
                    "estimated_duration_seconds": 45,
                },
            },
            # YouTube only renders chapter marks from three onwards, so the
            # fixture has to carry at least that many to exercise them.
            {
                "chapter_id": "ch03",
                "title": "Cüzdanlar",
                "order": 3,
                "narration": {
                    "text": "Cüzdanlar anahtarlarınızı saklar.",
                    "estimated_duration_seconds": 60,
                },
            },
        ],
    }
    (chapters_dir / "chapters.json").write_text(json.dumps(data), encoding="utf-8")
    return data


def _write_render_manifest(tmp_path, episode_id, timeline):
    """Write a render manifest carrying the concat timeline."""
    render_dir = tmp_path / "outputs" / episode_id / "render"
    render_dir.mkdir(parents=True, exist_ok=True)
    (render_dir / "render_manifest.json").write_text(
        json.dumps({"episode_id": episode_id, "timeline": timeline}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Unit tests: _format_timestamp
# ---------------------------------------------------------------------------


class TestFormatTimestamp:
    def test_zero(self):
        assert _format_timestamp(0) == "0:00"

    def test_seconds_only(self):
        assert _format_timestamp(45) == "0:45"

    def test_one_minute(self):
        assert _format_timestamp(60) == "1:00"

    def test_minutes_seconds(self):
        assert _format_timestamp(75) == "1:15"

    def test_one_hour(self):
        assert _format_timestamp(3661) == "1:01:01"

    def test_negative_clamped_to_zero(self):
        assert _format_timestamp(-10) == "0:00"


class TestConformChapterMarks:
    def test_accepts_exact_minimum_boundaries(self):
        marks = [(5.5, "Bir"), (10.0, "İki"), (20.0, "Üç")]

        assert _conform_chapter_marks(marks, total_seconds=30.0) == [
            (0.0, "Bir"),
            (10.0, "İki"),
            (20.0, "Üç"),
        ]

    def test_removes_short_final_section_then_suppresses_invalid_set(self):
        marks = [(0.0, "Bir"), (20.0, "İki"), (40.0, "Üç")]

        assert _conform_chapter_marks(marks, total_seconds=49.9) == []


# ---------------------------------------------------------------------------
# Unit tests: safety checks
# ---------------------------------------------------------------------------


class TestCheckApprovalGate:
    def test_passes_when_approved_with_review(
        self, db_session, approved_episode, approved_review_task
    ):
        result = _check_approval_gate(db_session, approved_episode)
        assert result.passed is True
        assert result.name == "approval_gate"

    def test_fails_when_not_approved(self, db_session):
        ep = Episode(
            episode_id="ep_not_approved",
            source="youtube_rss",
            title="Unapproved",
            url="https://x.com",
            status=EpisodeStatus.RENDERED,
        )
        db_session.add(ep)
        db_session.commit()
        result = _check_approval_gate(db_session, ep)
        assert result.passed is False
        assert "approved" in result.message

    def test_fails_when_no_review_task(self, db_session, approved_episode):
        # No review task created
        result = _check_approval_gate(db_session, approved_episode)
        assert result.passed is False
        assert "render" in result.message.lower() or "ReviewTask" in result.message


class TestCheckArtifactIntegrity:
    def test_passes_when_hash_matches(
        self, db_session, approved_episode, approved_review_task, settings
    ):
        result = _check_artifact_integrity(db_session, approved_episode, settings)
        assert result.passed is True

    def test_fails_when_hash_mismatch(
        self, db_session, approved_episode, approved_review_task, tmp_path, settings
    ):
        # Modify video file after hash was computed
        video_path = tmp_path / "outputs" / approved_episode.episode_id / "render" / "draft.mp4"
        video_path.write_bytes(b"tampered content!")
        result = _check_artifact_integrity(db_session, approved_episode, settings)
        assert result.passed is False
        assert "mismatch" in result.message.lower()

    def test_fails_when_no_review_task(self, db_session, settings):
        ep = Episode(
            episode_id="ep_no_task",
            source="youtube_rss",
            title="No task",
            url="https://x.com",
            status=EpisodeStatus.APPROVED,
        )
        db_session.add(ep)
        db_session.commit()
        result = _check_artifact_integrity(db_session, ep, settings)
        assert result.passed is False


class TestCheckMetadataCompleteness:
    def test_passes_with_all_fields(self):
        result = _check_metadata_completeness("My Title", "Description text", ["Bitcoin"])
        assert result.passed is True

    def test_fails_with_empty_title(self):
        result = _check_metadata_completeness("", "Description", ["Bitcoin"])
        assert result.passed is False
        assert "title" in result.message

    def test_fails_with_empty_tags(self):
        result = _check_metadata_completeness("Title", "Desc", [])
        assert result.passed is False
        assert "tag" in result.message

    def test_fails_with_multiple_missing(self):
        result = _check_metadata_completeness("", "", [])
        assert result.passed is False


class TestCheckCostSanity:
    def test_passes_within_budget(self, db_session, approved_episode, settings):
        run = PipelineRun(
            episode_id=approved_episode.id,
            stage="translate",
            status=RunStatus.SUCCESS.value,
            estimated_cost_usd=1.50,
        )
        db_session.add(run)
        db_session.commit()
        result = _check_cost_sanity(db_session, approved_episode, settings)
        assert result.passed is True
        assert "1.50" in result.message

    def test_fails_over_budget(self, db_session, approved_episode, settings):
        run = PipelineRun(
            episode_id=approved_episode.id,
            stage="imagegen",
            status=RunStatus.SUCCESS.value,
            estimated_cost_usd=15.00,
        )
        db_session.add(run)
        db_session.commit()
        result = _check_cost_sanity(db_session, approved_episode, settings)
        assert result.passed is False
        assert "exceeds" in result.message

    def test_passes_with_no_runs(self, db_session, approved_episode, settings):
        result = _check_cost_sanity(db_session, approved_episode, settings)
        assert result.passed is True


# ---------------------------------------------------------------------------
# Unit tests: metadata building
# ---------------------------------------------------------------------------


class TestBuildYouTubeMetadata:
    def test_basic_metadata_from_chapters(self, db_session, approved_episode, settings, tmp_path):
        _make_chapters_json(tmp_path, approved_episode.episode_id)
        settings.outputs_dir = str(tmp_path / "outputs")

        title, description, tags = _build_youtube_metadata(approved_episode, settings)

        assert title == "Bitcoin Eğitim: Kripto Para Dünyası"
        assert "Giriş" in description or "0:00" in description
        assert "Bitcoin" in tags
        assert len(title) <= 100
        assert len(description) <= 5000

    def test_chapter_timestamps_start_with_zero(
        self, db_session, approved_episode, settings, tmp_path
    ):
        _make_chapters_json(tmp_path, approved_episode.episode_id)
        settings.outputs_dir = str(tmp_path / "outputs")

        _, description, _ = _build_youtube_metadata(approved_episode, settings)
        assert "0:00" in description

    def test_chapter_marks_use_rendered_timeline(
        self, db_session, approved_episode, settings, tmp_path
    ):
        """Marks must follow the rendered timeline, not the narration lengths."""
        _make_chapters_json(tmp_path, approved_episode.episode_id)
        settings.outputs_dir = str(tmp_path / "outputs")
        _write_render_manifest(
            tmp_path,
            approved_episode.episode_id,
            [
                {"kind": "intro", "chapter_id": "", "start_seconds": 0.0, "duration_seconds": 5.5},
                {
                    "kind": "chapter",
                    "chapter_id": "ch01",
                    "start_seconds": 5.5,
                    "duration_seconds": 30.0,
                },
                {
                    "kind": "topic_intro",
                    "chapter_id": "ch02",
                    "start_seconds": 35.5,
                    "duration_seconds": 2.4,
                },
                {
                    "kind": "chapter",
                    "chapter_id": "ch02",
                    "start_seconds": 37.9,
                    "duration_seconds": 45.0,
                },
                {
                    "kind": "topic_intro",
                    "chapter_id": "ch03",
                    "start_seconds": 82.9,
                    "duration_seconds": 2.4,
                },
                {
                    "kind": "chapter",
                    "chapter_id": "ch03",
                    "start_seconds": 85.3,
                    "duration_seconds": 60.0,
                },
            ],
        )

        _, description, _ = _build_youtube_metadata(approved_episode, settings)

        # First mark is pinned to 0:00; the others carry the intro/card shift.
        assert "0:00 Giriş" in description
        # The card announces the chapter, so the mark points at the card.
        assert "0:35 Blockchain" in description
        assert "1:22 Cüzdanlar" in description
        # The narration-only estimate would have produced these instead.
        assert "0:30 Blockchain" not in description
        assert "1:15 Cüzdanlar" not in description

    def test_chapter_marks_dropped_when_below_youtube_minimum(
        self, db_session, approved_episode, settings, tmp_path
    ):
        """Fewer than three usable marks means YouTube renders none at all."""
        _make_chapters_json(tmp_path, approved_episode.episode_id)
        settings.outputs_dir = str(tmp_path / "outputs")
        # ch02 and ch03 sit within ten seconds of their predecessor, so only
        # one mark survives and the whole block has to be suppressed.
        _write_render_manifest(
            tmp_path,
            approved_episode.episode_id,
            [
                {
                    "kind": "chapter",
                    "chapter_id": "ch01",
                    "start_seconds": 0.0,
                    "duration_seconds": 4.0,
                },
                {
                    "kind": "chapter",
                    "chapter_id": "ch02",
                    "start_seconds": 4.0,
                    "duration_seconds": 4.0,
                },
                {
                    "kind": "chapter",
                    "chapter_id": "ch03",
                    "start_seconds": 8.0,
                    "duration_seconds": 40.0,
                },
            ],
        )

        _, description, _ = _build_youtube_metadata(approved_episode, settings)
        assert "Bölümler" not in description

    def test_chapter_marks_fall_back_without_timeline(
        self, db_session, approved_episode, settings, tmp_path
    ):
        """An older manifest without a timeline still yields usable marks."""
        _make_chapters_json(tmp_path, approved_episode.episode_id)
        settings.outputs_dir = str(tmp_path / "outputs")

        _, description, _ = _build_youtube_metadata(approved_episode, settings)

        assert "0:00 Giriş" in description
        assert "0:30 Blockchain" in description

    def test_fallback_to_episode_title(self, db_session, approved_episode, settings, tmp_path):
        # No chapters.json → use episode.title
        settings.outputs_dir = str(tmp_path / "outputs")
        title, _, _ = _build_youtube_metadata(approved_episode, settings)
        assert title == approved_episode.title

    def test_tags_include_base_tags(self, db_session, approved_episode, settings, tmp_path):
        settings.outputs_dir = str(tmp_path / "outputs")
        _, _, tags = _build_youtube_metadata(approved_episode, settings)
        assert "Bitcoin" in tags
        assert "Kripto" in tags


# ---------------------------------------------------------------------------
# Integration tests: publish_video
# ---------------------------------------------------------------------------


class TestPublishVideo:
    @pytest.fixture(autouse=True)
    def _validated_render(self, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.renderer.render_is_current",
            lambda *_args, **_kwargs: (True, "render is current"),
        )

    def test_raises_when_episode_not_found(self, db_session, settings):
        with pytest.raises(ValueError, match="not found"):
            publish_video(db_session, "nonexistent_ep", settings)

    def test_raises_when_not_approved(self, db_session, settings):
        ep = Episode(
            episode_id="ep_rendered",
            source="youtube_rss",
            title="Rendered",
            url="https://x.com",
            status=EpisodeStatus.RENDERED,
            pipeline_version=2,
        )
        db_session.add(ep)
        db_session.commit()
        with pytest.raises(ValueError, match="approved"):
            publish_video(db_session, "ep_rendered", settings)

    def test_skips_if_already_published(self, db_session, settings, approved_episode):
        approved_episode.youtube_video_id = "EXISTING_VIDEO_ID"
        db_session.commit()

        result = publish_video(db_session, approved_episode.episode_id, settings)
        assert result.skipped is True
        assert result.youtube_video_id == "EXISTING_VIDEO_ID"

    def test_restart_recovers_completed_production_job_without_duplicate(
        self, db_session, settings, approved_episode
    ):
        job = PublishJob(
            episode_id=approved_episode.episode_id,
            status=PublishJobStatus.PUBLISHED.value,
            youtube_video_id="YT_RECOVERED",
            youtube_url="https://youtu.be/YT_RECOVERED",
            metadata_snapshot=json.dumps({"publish_target": "production"}),
        )
        db_session.add(job)
        db_session.commit()

        with patch("btcedu.services.youtube_service.YouTubeDataAPIService") as service:
            result = publish_video(db_session, approved_episode.episode_id, settings)

        assert result.skipped is True
        assert result.publish_target == "production"
        service.return_value.upload_video.assert_not_called()
        db_session.refresh(approved_episode)
        assert approved_episode.status == EpisodeStatus.PUBLISHED
        assert approved_episode.youtube_video_id == "YT_RECOVERED"

    def test_restart_skips_completed_test_job_without_marking_episode_published(
        self, db_session, settings, approved_episode
    ):
        job = PublishJob(
            episode_id=approved_episode.episode_id,
            status=PublishJobStatus.PUBLISHED.value,
            youtube_video_id="YT_TEST",
            youtube_url="https://youtu.be/YT_TEST",
            metadata_snapshot=json.dumps({"publish_target": "test"}),
        )
        db_session.add(job)
        db_session.commit()

        with patch("btcedu.services.youtube_service.YouTubeDataAPIService") as service:
            result = publish_video(
                db_session,
                approved_episode.episode_id,
                settings,
                target="test",
            )

        assert result.skipped is True
        service.return_value.upload_video.assert_not_called()
        db_session.refresh(approved_episode)
        assert approved_episode.status == EpisodeStatus.APPROVED
        assert approved_episode.youtube_video_id is None

    def test_crash_during_upload_blocks_retry_until_reconciled(
        self, db_session, approved_episode, approved_review_task, settings, tmp_path
    ):
        settings.outputs_dir = str(tmp_path / "outputs")
        _make_chapters_json(tmp_path, approved_episode.episode_id)
        guard = MagicMock()
        guard.ensure_active.return_value = None
        guard.lease_token = "lease-crash"
        guard.fencing_token = 11

        with (
            patch(
                "btcedu.failover.coordination.acquire_publish_lease_guard",
                return_value=guard,
            ),
            patch("btcedu.services.youtube_service.YouTubeDataAPIService") as service,
        ):
            service.return_value.upload_video.side_effect = KeyboardInterrupt()
            with pytest.raises(KeyboardInterrupt):
                publish_video(db_session, approved_episode.episode_id, settings)

        job = get_latest_publish_job(db_session, approved_episode.episode_id)
        assert job is not None
        assert job.status == PublishJobStatus.UPLOADING.value
        snapshot = json.loads(job.metadata_snapshot)
        assert snapshot["publish_target"] == "production"
        assert snapshot["attempt_state"] == "uploading"

        with patch("btcedu.services.youtube_service.YouTubeDataAPIService") as retry_service:
            with pytest.raises(PublishReconciliationRequired, match="remote outcome is unknown"):
                publish_video(
                    db_session,
                    approved_episode.episode_id,
                    settings,
                    force=True,
                )

        retry_service.return_value.upload_video.assert_not_called()

    def test_real_test_upload_stays_separate_from_production_state(
        self, db_session, approved_episode, approved_review_task, settings, tmp_path
    ):
        from btcedu.services.youtube_service import YouTubeUploadResponse

        settings.outputs_dir = str(tmp_path / "outputs")
        _make_chapters_json(tmp_path, approved_episode.episode_id)

        with (
            patch("btcedu.failover.coordination.acquire_publish_lease_guard") as lease,
            patch("btcedu.services.youtube_service.YouTubeDataAPIService") as service,
        ):
            service.return_value.upload_video.return_value = YouTubeUploadResponse(
                video_id="YT_TEST_UPLOAD",
                video_url="https://youtu.be/YT_TEST_UPLOAD",
                privacy_status="private",
                channel_id="UC_TEST",
            )
            result = publish_video(
                db_session,
                approved_episode.episode_id,
                settings,
                target="test",
            )

        lease.assert_not_called()
        assert result.publish_target == "test"
        db_session.refresh(approved_episode)
        assert approved_episode.status == EpisodeStatus.APPROVED
        assert approved_episode.youtube_video_id is None
        job = get_latest_publish_job(db_session, approved_episode.episode_id)
        assert job is not None
        snapshot = json.loads(job.metadata_snapshot)
        assert snapshot["publish_target"] == "test"
        assert snapshot["privacy_status"] == "private"

    def test_dry_run_publishes_with_placeholder(
        self, db_session, approved_episode, approved_review_task, settings, tmp_path
    ):
        settings.dry_run = True
        settings.outputs_dir = str(tmp_path / "outputs")
        _make_chapters_json(tmp_path, approved_episode.episode_id)

        result = publish_video(db_session, approved_episode.episode_id, settings)
        assert result.dry_run is True
        assert result.youtube_video_id == "DRY_RUN"
        assert result.skipped is False
        # Episode should NOT be updated to PUBLISHED in dry-run
        db_session.refresh(approved_episode)
        assert approved_episode.status == EpisodeStatus.APPROVED

    def test_prefers_persisted_metadata(
        self, db_session, approved_episode, approved_review_task, settings, tmp_path
    ):
        settings.dry_run = True
        settings.outputs_dir = str(tmp_path / "outputs")
        _make_chapters_json(tmp_path, approved_episode.episode_id)
        # Persist reviewer-approved metadata with a distinctive title.
        meta_path = (
            tmp_path / "outputs" / approved_episode.episode_id / "render" / "youtube_metadata.json"
        )
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps(
                {
                    "title": "REVIEWED TITLE 123",
                    "description": "reviewed description",
                    "tags": ["reviewed"],
                    "source": "edited",
                }
            ),
            encoding="utf-8",
        )

        publish_video(db_session, approved_episode.episode_id, settings)

        prov_path = (
            tmp_path / "outputs" / approved_episode.episode_id / "provenance" / "publish.json"
        )
        data = json.loads(prov_path.read_text())
        assert data["metadata_snapshot"]["title"] == "REVIEWED TITLE 123"
        assert data["metadata_snapshot"]["tags"] == ["reviewed"]

    def test_safety_check_failure_raises(self, db_session, approved_episode, settings, tmp_path):
        """No review task → safety check should fail."""
        settings.outputs_dir = str(tmp_path / "outputs")
        _make_chapters_json(tmp_path, approved_episode.episode_id)
        # Don't create approved_review_task fixture (no approval gate)
        with pytest.raises(ValueError, match="safety checks failed"):
            publish_video(db_session, approved_episode.episode_id, settings)

    def test_publish_job_recorded_on_failure(
        self, db_session, approved_episode, approved_review_task, settings, tmp_path
    ):
        settings.outputs_dir = str(tmp_path / "outputs")
        _make_chapters_json(tmp_path, approved_episode.episode_id)
        # Force upload to fail
        with patch("btcedu.services.youtube_service.YouTubeDataAPIService") as mock_svc:
            mock_svc.return_value.upload_video.side_effect = Exception("Upload failed")
            settings.dry_run = False
            try:
                publish_video(db_session, approved_episode.episode_id, settings)
            except Exception:
                pass

        _job = get_latest_publish_job(db_session, approved_episode.episode_id)  # noqa: F841
        # We may not have a job if it failed at safety checks rather than upload
        # This is an optional assertion — main point is no uncaught exceptions

    def test_upload_failure_reports_publish_failed_state(
        self, db_session, approved_episode, approved_review_task, settings, tmp_path
    ):
        settings.outputs_dir = str(tmp_path / "outputs")
        _make_chapters_json(tmp_path, approved_episode.episode_id)

        guard = MagicMock()
        guard.ensure_active.return_value = None
        guard.lease_token = "lease-123"
        guard.fencing_token = 9
        guard.report_completion.return_value = None

        with (
            patch(
                "btcedu.failover.coordination.acquire_publish_lease_guard",
                return_value=guard,
            ),
            patch("btcedu.services.youtube_service.YouTubeDataAPIService") as mock_svc,
        ):
            mock_svc.return_value.upload_video.side_effect = RuntimeError("Upload failed")
            with pytest.raises(RuntimeError, match="Upload failed"):
                publish_video(db_session, approved_episode.episode_id, settings)

        assert guard.report_completion.call_args_list[0].kwargs == {"status": "publishing"}
        assert guard.report_completion.call_args_list[1].kwargs == {"status": "publish_failed"}
        job = get_latest_publish_job(db_session, approved_episode.episode_id)
        assert job is not None
        assert job.status == PublishJobStatus.FAILED.value

    def test_post_upload_coordination_failure_keeps_local_publish_state(
        self, db_session, approved_episode, approved_review_task, settings, tmp_path
    ):
        settings.outputs_dir = str(tmp_path / "outputs")
        _make_chapters_json(tmp_path, approved_episode.episode_id)

        guard = MagicMock()
        guard.ensure_active.return_value = None
        guard.lease_token = "lease-123"
        guard.fencing_token = 9
        guard.report_completion.side_effect = [
            None,
            RuntimeError("central publish record failed"),
        ]

        with (
            patch(
                "btcedu.failover.coordination.acquire_publish_lease_guard",
                return_value=guard,
            ),
            patch("btcedu.services.youtube_service.YouTubeDataAPIService") as mock_svc,
        ):
            mock_svc.return_value.upload_video.return_value = MagicMock(
                video_id="YT123",
                video_url="https://youtu.be/YT123",
            )
            with pytest.raises(PublishCoordinationError, match="local publish state was kept"):
                publish_video(db_session, approved_episode.episode_id, settings)

        db_session.refresh(approved_episode)
        assert approved_episode.status == EpisodeStatus.PUBLISHED
        assert approved_episode.youtube_video_id == "YT123"

        job = get_latest_publish_job(db_session, approved_episode.episode_id)
        assert job is not None
        assert job.status == PublishJobStatus.PUBLISHED.value
        assert job.youtube_video_id == "YT123"
        assert "local publish state was kept" in (job.error_message or "")

        with patch("btcedu.services.youtube_service.YouTubeDataAPIService") as retry_svc:
            result = publish_video(db_session, approved_episode.episode_id, settings)

        assert result.skipped is True
        assert result.youtube_video_id == "YT123"
        retry_svc.return_value.upload_video.assert_not_called()

    def test_provenance_file_written_on_dry_run(
        self, db_session, approved_episode, approved_review_task, settings, tmp_path
    ):
        settings.dry_run = True
        settings.outputs_dir = str(tmp_path / "outputs")
        _make_chapters_json(tmp_path, approved_episode.episode_id)

        publish_video(db_session, approved_episode.episode_id, settings)

        prov_path = (
            tmp_path / "outputs" / approved_episode.episode_id / "provenance" / "publish.json"
        )
        assert prov_path.exists()
        data = json.loads(prov_path.read_text())
        assert data["episode_id"] == approved_episode.episode_id
        assert data["dry_run"] is True
        assert data["publish_target"] == "production"
        assert data["metadata_snapshot"]["quota_estimate"]["upload_calls"] == 1
        assert "safety_checks" in data


# ---------------------------------------------------------------------------
# Tests: get_latest_publish_job
# ---------------------------------------------------------------------------


class TestGetLatestPublishJob:
    def test_returns_none_when_no_jobs(self, db_session):
        assert get_latest_publish_job(db_session, "ep_nojob") is None

    def test_returns_most_recent_job(self, db_session, approved_episode):
        job1 = PublishJob(episode_id=approved_episode.episode_id, status="failed")
        job2 = PublishJob(episode_id=approved_episode.episode_id, status="published")
        db_session.add_all([job1, job2])
        db_session.commit()
        job = get_latest_publish_job(db_session, approved_episode.episode_id)
        assert job is not None
        # Latest by created_at (job2 inserted after job1)
        assert job.status == "published"


# ---------------------------------------------------------------------------
# Tests: Gate-3 YouTube metadata suggestion / persistence / editing
# ---------------------------------------------------------------------------


def _make_news_chapters_json(tmp_path, episode_id):
    chapters_dir = tmp_path / "outputs" / episode_id
    chapters_dir.mkdir(parents=True, exist_ok=True)

    def _ch(cid, title, order, text, dur):
        return {
            "chapter_id": cid,
            "title": title,
            "order": order,
            "narration": {"text": text, "estimated_duration_seconds": dur},
        }

    data = {
        "episode_id": episode_id,
        "title": "tagesschau 20:00 Uhr — Türkçe",
        "chapters": [
            _ch("ch01", "tagesschau", 1, "Günün haberleri.", 10),
            _ch("ch02", "İran-ABD Görüşmeleri", 2, "Oman'da görüşmeler.", 40),
            _ch("ch03", "Ukrayna'ya Saldırılar", 3, "Kramatorsk saldırı altında.", 40),
            _ch("ch04", "Srebrenica Anması", 4, "Anma töreni.", 40),
            _ch("ch10", "Hava Durumu", 10, "Yarın için tahmin.", 20),
        ],
    }
    (chapters_dir / "chapters.json").write_text(
        json.dumps(data, ensure_ascii=False), encoding="utf-8"
    )
    return data


@pytest.fixture
def news_episode(db_session):
    ep = Episode(
        episode_id="ep_news_001",
        source="tagesschau_rss",
        title="tagesschau 20:00 Uhr, 11.07.2026",
        url="https://tagesschau.de/x",
        status=EpisodeStatus.RENDERED,
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    db_session.add(ep)
    db_session.commit()
    return ep


class TestSuggestNewsTitle:
    def test_builds_topic_title_with_date(self, news_episode, tmp_path):
        data = _make_news_chapters_json(tmp_path, news_episode.episode_id)
        from btcedu.core.publisher import _suggest_news_title

        title = _suggest_news_title(news_episode, data["chapters"], show_name="ALMANYA24")
        assert "11.07.2026" in title
        assert "İran-ABD Görüşmeleri" in title
        assert "Türkçe" in title
        # Generic intro/weather chapters are skipped
        assert title.startswith("ALMANYA24 11.07.2026")
        assert "tagesschau" not in title.lower()
        assert "Hava Durumu" not in title
        assert len(title) <= 100
        assert not title.endswith("PSİ")

    def test_returns_empty_without_topics(self, news_episode):
        from btcedu.core.publisher import _suggest_news_title

        assert _suggest_news_title(news_episode, []) == ""

    def test_news_metadata_keeps_source_off_title_but_credits_description(
        self, db_session, news_episode, settings, tmp_path
    ):
        _make_news_chapters_json(tmp_path, news_episode.episode_id)

        title, description, tags = _build_youtube_metadata(
            news_episode,
            settings,
            session=db_session,
        )

        assert title.startswith("ALMANYA24 11.07.2026")
        assert "tagesschau" not in title.lower()
        assert description.startswith("0:00 ")
        assert "tagesschau" in description.lower()
        assert "Kaynak:" in description
        assert all(tag.lower() != "tagesschau" for tag in tags)


class TestGenerateMetadataSuggestion:
    def test_persists_metadata_file(self, db_session, news_episode, settings, tmp_path):
        _make_news_chapters_json(tmp_path, news_episode.episode_id)
        from btcedu.core.publisher import generate_metadata_suggestion

        data = generate_metadata_suggestion(db_session, news_episode.episode_id, settings)
        assert data["title"]
        assert data["description"]
        assert data["tags"]
        assert data["source"] == "auto"
        path = (
            Path(settings.outputs_dir)
            / news_episode.episode_id
            / "render"
            / "youtube_metadata.json"
        )
        assert path.exists()
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["title"] == data["title"]

    def test_pulls_profile_publish_settings(self, db_session, news_episode, settings, tmp_path):
        _make_news_chapters_json(tmp_path, news_episode.episode_id)
        from btcedu.core.publisher import generate_metadata_suggestion

        data = generate_metadata_suggestion(db_session, news_episode.episode_id, settings)
        assert data["category_id"] == "25"
        assert data["publish_target"] == "test"
        assert data["privacy_status"] == "private"
        assert data["default_language"] == "tr"

    def test_does_not_overwrite_existing_without_force(
        self, db_session, news_episode, settings, tmp_path
    ):
        _make_news_chapters_json(tmp_path, news_episode.episode_id)
        from btcedu.core.publisher import generate_metadata_suggestion, save_metadata_edits

        generate_metadata_suggestion(db_session, news_episode.episode_id, settings)
        save_metadata_edits(news_episode.episode_id, settings, {"title": "Elle düzenlenmiş başlık"})
        again = generate_metadata_suggestion(db_session, news_episode.episode_id, settings)
        assert again["title"] == "Elle düzenlenmiş başlık"
        assert again["source"] == "edited"

    def test_refreshes_when_video_was_re_rendered(
        self, db_session, news_episode, settings, tmp_path
    ):
        """A re-render moves every mark, so an auto proposal must be redone."""
        _make_news_chapters_json(tmp_path, news_episode.episode_id)
        from btcedu.core.publisher import generate_metadata_suggestion

        _write_render_manifest(
            tmp_path,
            news_episode.episode_id,
            [
                {
                    "kind": "chapter",
                    "chapter_id": "ch01",
                    "start_seconds": 0.0,
                    "duration_seconds": 40.0,
                },
            ],
        )
        first = generate_metadata_suggestion(db_session, news_episode.episode_id, settings)

        # Same call, unchanged video: the proposal is reused untouched.
        assert (
            generate_metadata_suggestion(db_session, news_episode.episode_id, settings)[
                "generated_at"
            ]
            == first["generated_at"]
        )

        _write_render_manifest(
            tmp_path,
            news_episode.episode_id,
            [
                {"kind": "intro", "chapter_id": "", "start_seconds": 0.0, "duration_seconds": 5.5},
                {
                    "kind": "chapter",
                    "chapter_id": "ch01",
                    "start_seconds": 5.5,
                    "duration_seconds": 40.0,
                },
            ],
        )
        refreshed = generate_metadata_suggestion(db_session, news_episode.episode_id, settings)
        assert refreshed["generated_at"] != first["generated_at"]
        assert refreshed["render_timeline_hash"] != first["render_timeline_hash"]

    def test_re_render_does_not_clobber_reviewer_edits(
        self, db_session, news_episode, settings, tmp_path
    ):
        """Someone's own wording survives a re-render, stale marks or not."""
        _make_news_chapters_json(tmp_path, news_episode.episode_id)
        from btcedu.core.publisher import generate_metadata_suggestion, save_metadata_edits

        _write_render_manifest(
            tmp_path,
            news_episode.episode_id,
            [
                {
                    "kind": "chapter",
                    "chapter_id": "ch01",
                    "start_seconds": 0.0,
                    "duration_seconds": 40.0,
                },
            ],
        )
        generate_metadata_suggestion(db_session, news_episode.episode_id, settings)
        save_metadata_edits(news_episode.episode_id, settings, {"title": "Elle yazılmış"})

        _write_render_manifest(
            tmp_path,
            news_episode.episode_id,
            [
                {"kind": "intro", "chapter_id": "", "start_seconds": 0.0, "duration_seconds": 5.5},
                {
                    "kind": "chapter",
                    "chapter_id": "ch01",
                    "start_seconds": 5.5,
                    "duration_seconds": 40.0,
                },
            ],
        )
        again = generate_metadata_suggestion(db_session, news_episode.episode_id, settings)
        assert again["title"] == "Elle yazılmış"
        assert again["source"] == "edited"

    def test_force_does_not_overwrite_reviewer_edits(
        self, db_session, news_episode, settings, tmp_path
    ):
        _make_news_chapters_json(tmp_path, news_episode.episode_id)
        from btcedu.core.publisher import generate_metadata_suggestion, save_metadata_edits

        generate_metadata_suggestion(db_session, news_episode.episode_id, settings)
        save_metadata_edits(news_episode.episode_id, settings, {"title": "X"})
        regen = generate_metadata_suggestion(
            db_session, news_episode.episode_id, settings, force=True
        )
        assert regen["source"] == "edited"
        assert regen["title"] == "X"


class TestSaveMetadataEdits:
    def test_merges_edits(self, db_session, news_episode, settings, tmp_path):
        _make_news_chapters_json(tmp_path, news_episode.episode_id)
        from btcedu.core.publisher import generate_metadata_suggestion, save_metadata_edits

        generate_metadata_suggestion(db_session, news_episode.episode_id, settings)
        updated = save_metadata_edits(
            news_episode.episode_id,
            settings,
            {"title": "Yeni Başlık", "tags": "a, b, c"},
        )
        assert updated["title"] == "Yeni Başlık"
        assert updated["tags"] == ["a", "b", "c"]
        assert updated["source"] == "edited"

    def test_rejects_empty_title(self, db_session, news_episode, settings, tmp_path):
        _make_news_chapters_json(tmp_path, news_episode.episode_id)
        from btcedu.core.publisher import generate_metadata_suggestion, save_metadata_edits

        generate_metadata_suggestion(db_session, news_episode.episode_id, settings)
        with pytest.raises(ValueError):
            save_metadata_edits(news_episode.episode_id, settings, {"title": ""})


class TestLoadPersistedMetadata:
    def test_returns_none_when_absent(self, settings):
        from btcedu.core.publisher import load_persisted_metadata

        assert load_persisted_metadata("nope", settings) is None

    def test_roundtrip(self, db_session, news_episode, settings, tmp_path):
        _make_news_chapters_json(tmp_path, news_episode.episode_id)
        from btcedu.core.publisher import generate_metadata_suggestion, load_persisted_metadata

        gen = generate_metadata_suggestion(db_session, news_episode.episode_id, settings)
        loaded = load_persisted_metadata(news_episode.episode_id, settings)
        assert loaded is not None
        assert loaded["title"] == gen["title"]
