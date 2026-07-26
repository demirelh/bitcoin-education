from datetime import UTC, datetime, timedelta
from unittest.mock import patch

from btcedu.config import Settings
from btcedu.core.pipeline import StageResult
from btcedu.core.regression_runner import run_recent_episode_regression
from btcedu.db import get_session_factory, init_db
from btcedu.models.episode import Episode, EpisodeStatus


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
        calls.append((stage_name, episode.episode_id))
        return StageResult(stage_name, "success", 0.0, detail="ok")

    with patch("btcedu.core.pipeline._run_stage", side_effect=fake_run_stage):
        result = run_recent_episode_regression(
            session,
            settings,
            from_stage="chapterize",
        )

    assert result.episode_ids == episode_ids
    assert result.stages == ["chapterize"]
    assert calls == [("chapterize", episode_id) for episode_id in episode_ids]
    for episode_id in episode_ids:
        assert (outputs / episode_id / "marker.txt").read_text(encoding="utf-8") == "production"
    session.close()
