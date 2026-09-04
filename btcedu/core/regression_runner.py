"""Isolated regression runs for recent episodes."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from btcedu.config import Settings
from btcedu.db import get_session_factory
from btcedu.models.episode import Episode
from btcedu.models.review import ReviewTask


@dataclass(frozen=True)
class RegressionStageResult:
    stage: str
    episode_id: str
    status: str
    detail: str


@dataclass(frozen=True)
class RegressionRunResult:
    episode_ids: list[str]
    stages: list[str]
    results: list[RegressionStageResult]


def _sqlite_path(database_url: str) -> Path:
    prefix = "sqlite:///"
    if not database_url.startswith(prefix) or database_url.endswith(":memory:"):
        raise ValueError("regression-run currently requires a file-backed SQLite database")
    return Path(database_url.removeprefix(prefix)).resolve()


def _copy_episode_entries(source_root: str, target_root: Path, episode_ids: list[str]) -> None:
    source = Path(source_root).resolve()
    target_root.mkdir(parents=True, exist_ok=True)
    if not source.exists():
        return

    for episode_id in episode_ids:
        for entry in source.glob(f"{episode_id}*"):
            destination = target_root / entry.name
            if entry.is_dir():
                shutil.copytree(entry, destination, dirs_exist_ok=True)
            else:
                shutil.copy2(entry, destination)


def _clone_database(source_url: str, target_path: Path) -> str:
    source_path = _sqlite_path(source_url)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(source_path) as source, sqlite3.connect(target_path) as target:
        source.backup(target)
    return f"sqlite:///{target_path}"


def _rebind_review_artifacts(
    session: Session,
    source_settings: Settings,
    isolated_settings: Settings,
) -> None:
    """Rebind copied review tasks to equivalent files in the isolated workspace."""
    from btcedu.core.reviewer import _compute_artifact_hash

    roots = [
        (Path(source_settings.outputs_dir).resolve(), Path(isolated_settings.outputs_dir)),
        (Path(source_settings.transcripts_dir).resolve(), Path(isolated_settings.transcripts_dir)),
        (Path(source_settings.raw_data_dir).resolve(), Path(isolated_settings.raw_data_dir)),
        (Path(source_settings.reports_dir).resolve(), Path(isolated_settings.reports_dir)),
    ]

    for task in session.query(ReviewTask).all():
        try:
            artifact_paths = json.loads(task.artifact_paths or "[]")
        except (json.JSONDecodeError, TypeError):
            continue
        rebound: list[str] = []
        changed = False
        for artifact_path in artifact_paths:
            resolved = Path(artifact_path).resolve()
            mapped = None
            for source_root, isolated_root in roots:
                try:
                    relative = resolved.relative_to(source_root)
                except ValueError:
                    continue
                mapped = isolated_root / relative
                break
            if mapped is None:
                rebound.append(artifact_path)
                continue
            rebound.append(str(mapped))
            changed = True
        if not changed or not all(Path(path).exists() for path in rebound):
            continue
        task.artifact_paths = json.dumps(rebound)
        task.artifact_hash = _compute_artifact_hash(rebound)
    session.commit()


def _recent_episode_ids(
    session: Session,
    *,
    profile: str,
    count: int,
) -> list[str]:
    episodes = (
        session.query(Episode)
        .filter(Episode.content_profile == profile)
        .order_by(Episode.published_at.desc())
        .limit(count)
        .all()
    )
    if len(episodes) != count:
        raise ValueError(
            f"Expected {count} episodes for profile {profile!r}, found {len(episodes)}"
        )
    return [episode.episode_id for episode in episodes]


def _regression_stages(
    settings: Settings,
    episodes: list[Episode],
    from_stage: str,
    *,
    only_stage: bool = False,
) -> list[str]:
    from btcedu.core.pipeline import _get_stages

    plans = [[name for name, _ in _get_stages(settings, episode)] for episode in episodes]
    if any(plan != plans[0] for plan in plans[1:]):
        raise ValueError("Selected episodes do not share the same profile-aware pipeline plan")

    full_plan = plans[0]
    if from_stage not in full_plan:
        raise ValueError(
            f"Unknown start stage {from_stage!r}; choose one of: " + ", ".join(full_plan)
        )
    if only_stage:
        return [from_stage]

    if "chapterize" not in full_plan:
        raise ValueError("Selected pipeline plan has no chapterize stage")
    plan = full_plan[: full_plan.index("chapterize") + 1]
    if from_stage not in plan:
        raise ValueError(
            f"Start stage {from_stage!r} is after chapterize; use --only-stage to run it"
        )
    return plan[plan.index(from_stage) :]


def run_recent_episode_regression(
    production_session: Session,
    settings: Settings,
    *,
    from_stage: str,
    profile: str = "tagesschau_tr",
    count: int = 3,
    only_stage: bool = False,
) -> RegressionRunResult:
    """Run recent episodes in an isolated copy."""
    from btcedu.core.pipeline import _run_stage

    episode_ids = _recent_episode_ids(production_session, profile=profile, count=count)

    workspace_root = Path(settings.outputs_dir).resolve() / ".regression-workspaces"
    workspace_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="btcedu-regression-",
        dir=workspace_root,
    ) as workspace_str:
        workspace = Path(workspace_str)
        database_url = _clone_database(settings.database_url, workspace / "btcedu.db")
        isolated_settings = settings.model_copy(
            update={
                "database_url": database_url,
                "raw_data_dir": str(workspace / "raw"),
                "transcripts_dir": str(workspace / "transcripts"),
                "outputs_dir": str(workspace / "outputs"),
                "reports_dir": str(workspace / "reports"),
                "logs_dir": str(workspace / "logs"),
            }
        )

        _copy_episode_entries(settings.raw_data_dir, workspace / "raw", episode_ids)
        _copy_episode_entries(settings.transcripts_dir, workspace / "transcripts", episode_ids)
        _copy_episode_entries(settings.outputs_dir, workspace / "outputs", episode_ids)
        _copy_episode_entries(settings.reports_dir, workspace / "reports", episode_ids)

        session = get_session_factory(database_url)()
        try:
            _rebind_review_artifacts(session, settings, isolated_settings)
            episode_by_id = {
                episode.episode_id: episode
                for episode in session.query(Episode)
                .filter(Episode.episode_id.in_(episode_ids))
                .all()
            }
            episodes = [episode_by_id[episode_id] for episode_id in episode_ids]
            stages = _regression_stages(
                isolated_settings,
                episodes,
                from_stage,
                only_stage=only_stage,
            )

            for episode in episodes:
                episode.error_message = None
                episode.retry_count = 0
            session.commit()

            results: list[RegressionStageResult] = []
            for stage_name in stages:
                stage_failed = False
                for episode in episodes:
                    result = _run_stage(
                        session,
                        episode,
                        isolated_settings,
                        stage_name,
                        force=True,
                    )
                    detail = result.error or result.detail or ""
                    results.append(
                        RegressionStageResult(
                            stage=stage_name,
                            episode_id=episode.episode_id,
                            status=result.status,
                            detail=detail,
                        )
                    )
                    if result.status == "failed":
                        stage_failed = True
                if stage_failed:
                    return RegressionRunResult(episode_ids, stages, results)

            return RegressionRunResult(episode_ids, stages, results)
        finally:
            session.close()
