from datetime import UTC, datetime, timedelta
from pathlib import Path

from btcedu.config import Settings
from btcedu.core.retention import prune_expired_episodes
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.dead_letter import DeadLetterEntry
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus
from btcedu.models.media_asset import Base as MediaBase
from btcedu.models.media_asset import MediaAsset, MediaAssetType
from btcedu.models.publish_job import PublishJob
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
    assert db_session.query(PublishJob).filter_by(episode_id=old.episode_id).count() == 0
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
