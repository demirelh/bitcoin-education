"""Episode retention and artifact cleanup."""

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.dead_letter import DeadLetterEntry
from btcedu.models.episode import Episode, PipelineRun, RunStatus
from btcedu.models.media_asset import MediaAsset
from btcedu.models.publish_job import PublishJob
from btcedu.models.review import ReviewTask


@dataclass
class RetentionResult:
    deleted: int = 0
    protected: int = 0


def retention_days(settings: Settings, profile_name: str | None = None) -> int:
    if profile_name:
        from btcedu.profiles import ProfileNotFoundError, get_registry

        try:
            profile = get_registry(settings).get(profile_name)
        except ProfileNotFoundError:
            profile = None
        if profile is not None:
            configured = (profile.ingest or {}).get("retention_days")
            if configured is not None:
                return max(0, int(configured))
    return max(0, settings.episode_retention_days)


def retention_cutoff(
    settings: Settings,
    *,
    profile_name: str | None = None,
    now: datetime | None = None,
) -> datetime | None:
    days = retention_days(settings, profile_name)
    if days == 0:
        return None
    reference = _as_utc(now or datetime.now(UTC))
    return reference - timedelta(days=days)


def episode_is_expired(
    episode: Episode,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> bool:
    cutoff = retention_cutoff(
        settings,
        profile_name=episode.content_profile,
        now=now,
    )
    if cutoff is None:
        return False
    episode_date = _as_utc(episode.published_at or episode.detected_at)
    return episode_date < cutoff


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def prune_expired_episodes(
    session: Session,
    settings: Settings,
    *,
    now: datetime | None = None,
) -> RetentionResult:
    """Delete expired episode records and their local artifacts.

    Episodes with a running pipeline stage are retained until a later cleanup
    pass so files cannot disappear underneath an active worker.
    """
    expired = [
        episode
        for episode in session.query(Episode).all()
        if episode_is_expired(episode, settings, now=now)
    ]
    result = RetentionResult()

    for episode in expired:
        has_running_stage = (
            session.query(PipelineRun.id)
            .filter(
                PipelineRun.episode_id == episode.id,
                PipelineRun.status == RunStatus.RUNNING,
            )
            .first()
            is not None
        )
        if has_running_stage:
            result.protected += 1
            continue

        _delete_episode_files(settings, episode.episode_id)
        _delete_episode_records(session, episode)
        result.deleted += 1

    session.commit()
    return result


def _delete_episode_files(settings: Settings, episode_id: str) -> None:
    for root in (
        settings.raw_data_dir,
        settings.transcripts_dir,
        settings.outputs_dir,
        settings.reports_dir,
    ):
        path = _safe_child_path(root, episode_id)
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    log_path = _safe_child_path(Path(settings.logs_dir) / "episodes", f"{episode_id}.log")
    if log_path.exists():
        log_path.unlink()


def _safe_child_path(root: str | Path, child: str) -> Path:
    root_path = Path(root).resolve()
    child_path = (root_path / child).resolve()
    if child_path.parent != root_path:
        raise ValueError(f"Unsafe episode artifact path: {child_path}")
    return child_path


def _delete_episode_records(session: Session, episode: Episode) -> None:
    episode_id = episode.episode_id
    for task in session.query(ReviewTask).filter(ReviewTask.episode_id == episode_id).all():
        session.delete(task)

    session.query(ContentArtifact).filter(ContentArtifact.episode_id == episode_id).delete(
        synchronize_session=False
    )
    session.query(DeadLetterEntry).filter(DeadLetterEntry.episode_id == episode_id).delete(
        synchronize_session=False
    )
    session.query(MediaAsset).filter(MediaAsset.episode_id == episode_id).delete(
        synchronize_session=False
    )
    session.query(PublishJob).filter(PublishJob.episode_id == episode_id).delete(
        synchronize_session=False
    )
    session.delete(episode)
