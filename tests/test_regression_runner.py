import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from btcedu.config import Settings
from btcedu.core.pipeline import StageResult
from btcedu.core.regression_runner import run_recent_episode_regression
from btcedu.core.reviewer import _compute_artifact_hash, has_approved_review_for_artifacts
from btcedu.db import get_session_factory, init_db
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.review import ReviewStatus, ReviewTask


def test_regression_run_is_stage_major_and_isolated(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'production.db'}"
    init_db(database_url)
    session = get_session_factory(database_url)()
    outputs = tmp_path / "outputs"
    transcripts = tmp_path / "transcripts"
    raw = tmp_path / "raw"
    reports = tmp_path / "reports"
    now = datetime.now(UTC)

    episode_ids = ["ep-new", "ep-middle", "ep-old"]
    for index, episode_id in enumerate(episode_ids):
        session.add(
            Episode(
                episode_id=episode_id,
                source="youtube_rss",
                title=episode_id,
                url=f"https://example.com/{episode_id}",
                published_at=now - timedelta(days=index),
                status=EpisodeStatus.ADAPTED,
                pipeline_version=2,
                content_profile="tagesschau_tr",
            )
        )
        episode_dir = outputs / episode_id
        episode_dir.mkdir(parents=True)
        (episode_dir / "marker.txt").write_text("production", encoding="utf-8")
        gate_path = episode_dir / "translation_quality_gate.json"
        narration_path = episode_dir / "script.adapted.tr.md"
        gate_path.write_text('{"status":"red"}', encoding="utf-8")
        narration_path.write_text("Onaylı anlatım.", encoding="utf-8")
        artifacts = [str(gate_path), str(narration_path)]
        session.add(
            ReviewTask(
                episode_id=episode_id,
                stage="translation_qa",
                status=ReviewStatus.APPROVED.value,
                artifact_paths=json.dumps(artifacts),
                artifact_hash=_compute_artifact_hash(artifacts),
            )
        )
    session.commit()

    settings = Settings(
        database_url=database_url,
        outputs_dir=str(outputs),
        transcripts_dir=str(transcripts),
        raw_data_dir=str(raw),
        reports_dir=str(reports),
    )
    calls: list[tuple[str, str]] = []

    def fake_run_stage(isolated_session, episode, isolated_settings, stage_name, force=False):
        assert isolated_settings.outputs_dir != str(outputs)
        assert force is True
        isolated_artifacts = [
            str(
                Path(isolated_settings.outputs_dir)
                / episode.episode_id
                / "translation_quality_gate.json"
            ),
            str(Path(isolated_settings.outputs_dir) / episode.episode_id / "script.adapted.tr.md"),
        ]
        assert has_approved_review_for_artifacts(
            isolated_session,
            episode.episode_id,
            "translation_qa",
            isolated_artifacts,
        )
        calls.append((stage_name, episode.episode_id))
        return StageResult(stage_name, "success", 0.0, detail="ok")

    with patch("btcedu.core.pipeline._run_stage", side_effect=fake_run_stage):
        result = run_recent_episode_regression(
            session,
            settings,
            from_stage="render",
            only_stage=True,
        )

    assert result.episode_ids == episode_ids
    assert result.stages == ["render"]
    assert calls == [("render", episode_id) for episode_id in episode_ids]
    for episode_id in episode_ids:
        assert (outputs / episode_id / "marker.txt").read_text(encoding="utf-8") == "production"
    session.close()


def test_regression_run_can_target_one_stage_after_chapterize(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'production.db'}"
    init_db(database_url)
    session = get_session_factory(database_url)()
    now = datetime.now(UTC)
    episode_ids = ["ep-new", "ep-middle", "ep-old"]

    for index, episode_id in enumerate(episode_ids):
        session.add(
            Episode(
                episode_id=episode_id,
                source="youtube_rss",
                title=episode_id,
                url=f"https://example.com/{episode_id}",
                published_at=now - timedelta(days=index),
                status=EpisodeStatus.RENDERED,
                pipeline_version=2,
                content_profile="tagesschau_tr",
            )
        )
    session.commit()

    settings = Settings(
        database_url=database_url,
        outputs_dir=str(tmp_path / "outputs"),
        transcripts_dir=str(tmp_path / "transcripts"),
        raw_data_dir=str(tmp_path / "raw"),
        reports_dir=str(tmp_path / "reports"),
    )
    calls: list[tuple[str, str]] = []

    def fake_run_stage(isolated_session, episode, isolated_settings, stage_name, force=False):
        calls.append((stage_name, episode.episode_id))
        return StageResult(stage_name, "success", 0.0, detail="ok")

    with patch("btcedu.core.pipeline._run_stage", side_effect=fake_run_stage):
        result = run_recent_episode_regression(
            session,
            settings,
            from_stage="render",
            only_stage=True,
        )

    assert result.stages == ["render"]
    assert calls == [("render", episode_id) for episode_id in episode_ids]
    session.close()


def test_publish_only_regression_forces_dry_run(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'production.db'}"
    init_db(database_url)
    session = get_session_factory(database_url)()
    now = datetime.now(UTC)

    for index in range(3):
        episode_id = f"ep-{index}"
        session.add(
            Episode(
                episode_id=episode_id,
                source="youtube_rss",
                title=episode_id,
                url=f"https://example.com/{episode_id}",
                published_at=now - timedelta(days=index),
                status=EpisodeStatus.APPROVED,
                pipeline_version=2,
                content_profile="tagesschau_tr",
            )
        )
    session.commit()

    settings = Settings(
        database_url=database_url,
        outputs_dir=str(tmp_path / "outputs"),
        transcripts_dir=str(tmp_path / "transcripts"),
        raw_data_dir=str(tmp_path / "raw"),
        reports_dir=str(tmp_path / "reports"),
        dry_run=False,
    )

    def fake_run_stage(isolated_session, episode, isolated_settings, stage_name, force=False):
        assert isolated_settings.dry_run is True
        return StageResult(stage_name, "success", 0.0, detail="dry-run")

    with patch("btcedu.core.pipeline._run_stage", side_effect=fake_run_stage):
        result = run_recent_episode_regression(
            session,
            settings,
            from_stage="publish",
            only_stage=True,
        )

    assert result.stages == ["publish"]
    session.close()
