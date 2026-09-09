from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from btcedu.config import Settings
from btcedu.core.retention import prune_expired_episodes
from btcedu.models.avatar_job import AvatarJob, AvatarJobStatus
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.dead_letter import DeadLetterEntry
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus
from btcedu.models.media_asset import Base as MediaBase
from btcedu.models.media_asset import MediaAsset, MediaAssetType
from btcedu.models.publish_job import PublishJob, PublishJobStatus
from btcedu.models.review import ReviewTask


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        raw_data_dir=str(tmp_path / "raw"),
        transcripts_dir=str(tmp_path / "transcripts"),
        outputs_dir=str(tmp_path / "outputs"),
        reports_dir=str(tmp_path / "reports"),
        logs_dir=str(tmp_path / "logs"),
        episode_retention_days=0,
    )


def _episode(episode_id: str, published_at: datetime) -> Episode:
    return Episode(
        episode_id=episode_id,
        source="youtube_rss",
        title=f"tagesschau 20:00 Uhr, {published_at:%d.%m.%Y}",
        url=f"https://example.com/{episode_id}",
        published_at=published_at,
        status=EpisodeStatus.NEW,
        content_profile="tagesschau_tr",
        pipeline_version=2,
    )


def test_prune_deletes_expired_episode_records_and_files(db_session, tmp_path):
    settings = _settings(tmp_path)
    MediaBase.metadata.create_all(db_session.bind)
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    old = _episode("old-episode", now - timedelta(days=11))
    recent = _episode("recent-episode", now - timedelta(days=9))
    db_session.add_all([old, recent])
    db_session.flush()

    db_session.add_all(
        [
            ReviewTask(episode_id=old.episode_id, stage="translation_qa"),
            ContentArtifact(
                episode_id=old.episode_id,
                artifact_type="chapters",
                file_path="chapters.json",
                model="test",
                prompt_hash="hash",
            ),
            DeadLetterEntry(
                episode_id=old.episode_id,
                stage="render",
                error_category="permanent",
                error_message="failed",
                suggestion="review",
            ),
            PublishJob(episode_id=old.episode_id),
            MediaAsset(
                episode_id=old.episode_id,
                asset_type=MediaAssetType.IMAGE,
                file_path="images/one.png",
                mime_type="image/png",
                size_bytes=1,
            ),
        ]
    )
    db_session.commit()

    for root in (
        settings.raw_data_dir,
        settings.transcripts_dir,
        settings.outputs_dir,
        settings.reports_dir,
    ):
        episode_dir = Path(root) / old.episode_id
        episode_dir.mkdir(parents=True)
        (episode_dir / "artifact.txt").write_text("test", encoding="utf-8")
    log_path = Path(settings.logs_dir) / "episodes" / f"{old.episode_id}.log"
    log_path.parent.mkdir(parents=True)
    log_path.write_text("test", encoding="utf-8")

    result = prune_expired_episodes(db_session, settings, now=now)

    assert result.deleted == 1
    assert result.protected == 0
    assert db_session.query(Episode).filter_by(episode_id=old.episode_id).first() is None
    assert db_session.query(Episode).filter_by(episode_id=recent.episode_id).one()
    assert db_session.query(ReviewTask).filter_by(episode_id=old.episode_id).count() == 0
    assert db_session.query(ContentArtifact).filter_by(episode_id=old.episode_id).count() == 0
    assert db_session.query(DeadLetterEntry).filter_by(episode_id=old.episode_id).count() == 0
    assert db_session.query(PublishJob).filter_by(episode_id=old.episode_id).count() == 1
    assert db_session.query(MediaAsset).filter_by(episode_id=old.episode_id).count() == 0
    assert not log_path.exists()
    assert not (Path(settings.outputs_dir) / old.episode_id).exists()


def test_prune_protects_episode_with_running_stage(db_session, tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    old = _episode("active-old-episode", now - timedelta(days=11))
    db_session.add(old)
    db_session.flush()
    db_session.add(
        PipelineRun(
            episode_id=old.id,
            stage=PipelineStage.RENDER,
            status=RunStatus.RUNNING,
        )
    )
    db_session.commit()

    result = prune_expired_episodes(db_session, settings, now=now)

    assert result.deleted == 0
    assert result.protected == 1
    assert db_session.query(Episode).filter_by(episode_id=old.episode_id).one()


def test_prune_keeps_database_record_when_file_deletion_is_blocked(
    db_session, tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    old = _episode("blocked-old-episode", now - timedelta(days=11))
    db_session.add(old)
    db_session.commit()

    def deny_delete(*args, **kwargs):
        raise PermissionError("root-owned artifact")

    monkeypatch.setattr("btcedu.core.retention._delete_episode_files", deny_delete)

    result = prune_expired_episodes(db_session, settings, now=now)

    assert result.deleted == 0
    assert result.blocked == 1
    assert db_session.query(Episode).filter_by(episode_id=old.episode_id).one()


def _avatar_job(episode_id: str, status: AvatarJobStatus) -> AvatarJob:
    return AvatarJob(
        episode_id=episode_id,
        scene_id="scene-1",
        chapter_id="chapter-1",
        content_hash=f"hash-{status.value}",
        provider="heygen",
        status=status.value,
        duration_seconds=10,
        cost_usd=0.2,
    )


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (AvatarJobStatus.RESERVED, "avatar_reserved"),
        (AvatarJobStatus.SUBMITTED, "avatar_submitted"),
        (AvatarJobStatus.RECONCILE_REQUIRED, "avatar_reconcile_required"),
    ],
)
def test_prune_protects_episode_with_unresolved_avatar_job(
    db_session, tmp_path, status, reason
):
    settings = _settings(tmp_path)
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    old = _episode(f"avatar-{status.value}", now - timedelta(days=11))
    db_session.add_all([old, _avatar_job(old.episode_id, status)])
    db_session.commit()

    result = prune_expired_episodes(db_session, settings, now=now)

    assert result.deleted == 0
    assert result.protected == 1
    assert result.holds[0].reasons == (reason,)
    assert db_session.query(Episode).filter_by(episode_id=old.episode_id).one()


@pytest.mark.parametrize(
    "status",
    [
        AvatarJobStatus.COMPLETED,
        AvatarJobStatus.FAILED,
        AvatarJobStatus.ABANDONED,
    ],
)
def test_prune_keeps_resolved_avatar_audit_after_episode_cleanup(
    db_session, tmp_path, status
):
    settings = _settings(tmp_path)
    MediaBase.metadata.create_all(db_session.bind)
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    old = _episode(f"resolved-{status.value}", now - timedelta(days=11))
    job = _avatar_job(old.episode_id, status)
    db_session.add_all([old, job])
    db_session.commit()

    result = prune_expired_episodes(db_session, settings, now=now)

    assert result.deleted == 1
    assert db_session.query(Episode).filter_by(episode_id=old.episode_id).first() is None
    assert db_session.query(AvatarJob).filter_by(id=job.id).one()


def test_prune_protects_episode_with_upload_in_progress(db_session, tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    old = _episode("uploading", now - timedelta(days=11))
    db_session.add_all(
        [
            old,
            PublishJob(
                episode_id=old.episode_id,
                status=PublishJobStatus.UPLOADING.value,
            ),
        ]
    )
    db_session.commit()

    result = prune_expired_episodes(db_session, settings, now=now)

    assert result.deleted == 0
    assert result.holds[0].reasons == ("publish_uploading",)
    assert db_session.query(Episode).filter_by(episode_id=old.episode_id).one()


def test_prune_reports_all_hold_reasons_and_is_repeatable(db_session, tmp_path):
    settings = _settings(tmp_path)
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    old = _episode("multiple-holds", now - timedelta(days=11))
    db_session.add(old)
    db_session.flush()
    db_session.add_all(
        [
            PipelineRun(
                episode_id=old.id,
                stage=PipelineStage.RENDER,
                status=RunStatus.RUNNING,
            ),
            _avatar_job(old.episode_id, AvatarJobStatus.SUBMITTED),
            PublishJob(
                episode_id=old.episode_id,
                status=PublishJobStatus.UPLOADING.value,
            ),
        ]
    )
    db_session.commit()

    first = prune_expired_episodes(db_session, settings, now=now)
    second = prune_expired_episodes(db_session, settings, now=now)

    expected = ("pipeline_running", "avatar_submitted", "publish_uploading")
    assert first.holds[0].reasons == expected
    assert second.holds[0].reasons == expected
    assert db_session.query(Episode).filter_by(episode_id=old.episode_id).one()


def test_retention_dry_run_reports_holds_and_candidates_without_deleting(
    db_session, tmp_path, monkeypatch
):
    settings = _settings(tmp_path)
    now = datetime(2026, 7, 21, 12, tzinfo=UTC)
    candidate = _episode("candidate", now - timedelta(days=11))
    held = _episode("held", now - timedelta(days=12))
    db_session.add_all(
        [
            candidate,
            held,
            _avatar_job(held.episode_id, AvatarJobStatus.RECONCILE_REQUIRED),
        ]
    )
    db_session.commit()
    monkeypatch.setattr(
        "btcedu.core.retention._delete_episode_files",
        lambda *_args, **_kwargs: pytest.fail("dry-run must not delete files"),
    )

    result = prune_expired_episodes(db_session, settings, now=now, dry_run=True)

    assert result.deleted == 0
    assert result.would_delete == 1
    assert result.protected == 1
    assert len(result.holds) == 1
    assert result.holds[0].episode_id == "held"
    assert result.holds[0].reasons == ("avatar_reconcile_required",)
    assert db_session.query(Episode).count() == 2
