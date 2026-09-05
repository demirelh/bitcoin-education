from datetime import UTC, datetime, timedelta

from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, PipelineStage, RunStatus
from btcedu.services.credits_service import (
    _apply_openai_runway,
    _query_usage_only,
    save_openai_credit_snapshot,
)


def _add_episode_cost(db_session, episode_id: str, cost: float, offset_minutes: int) -> None:
    episode = Episode(
        episode_id=episode_id,
        source="local_recorder",
        title=episode_id,
        url=f"/tmp/{episode_id}.mp4",
        status=EpisodeStatus.TRANSCRIBED,
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    db_session.add(episode)
    db_session.flush()
    completed_at = datetime.now(UTC) + timedelta(minutes=offset_minutes)
    db_session.add(
        PipelineRun(
            episode_id=episode.id,
            stage=PipelineStage.TRANSCRIBE,
            status=RunStatus.SUCCESS,
            started_at=completed_at - timedelta(minutes=1),
            completed_at=completed_at,
            estimated_cost_usd=cost,
        )
    )
    db_session.commit()


def _openai_status(db_session):
    status = _query_usage_only(
        db_session,
        provider="openai",
        display_name="OpenAI",
        dashboard_url="",
        stage_match=["transcribe", "transcript_verify"],
    )
    _apply_openai_runway(db_session, status)
    return status


def test_openai_runway_warns_two_episodes_before_balance_ends(db_session):
    for index in range(3):
        _add_episode_cost(db_session, f"ep-{index}", 0.1, index)
    save_openai_credit_snapshot(db_session, 0.25)

    status = _openai_status(db_session)

    assert status.balance_usd == 0.25
    assert status.average_episode_cost_usd == 0.1
    assert status.estimated_episodes_remaining == 2
    assert status.status == "warn"


def test_openai_runway_becomes_critical_with_one_episode_left(db_session):
    for index in range(3):
        _add_episode_cost(db_session, f"ep-{index}", 0.1, index)
    save_openai_credit_snapshot(db_session, 0.25)
    _add_episode_cost(db_session, "ep-new", 0.1, 10)

    status = _openai_status(db_session)

    assert status.balance_usd == 0.15
    assert status.estimated_episodes_remaining == 1
    assert status.status == "critical"


def test_openai_quota_failure_is_critical_without_snapshot(db_session):
    _add_episode_cost(db_session, "ep-cost", 0.1, 0)
    episode = Episode(
        episode_id="ep-failed",
        source="local_recorder",
        title="Failed",
        url="/tmp/failed.mp4",
        status=EpisodeStatus.DOWNLOADED,
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    db_session.add(episode)
    db_session.flush()
    db_session.add(
        PipelineRun(
            episode_id=episode.id,
            stage=PipelineStage.TRANSCRIBE,
            status=RunStatus.FAILED,
            completed_at=datetime.now(UTC),
            error_message="credit_balance_exhausted: You have no credits remaining",
        )
    )
    db_session.commit()

    status = _openai_status(db_session)

    assert status.quota_exhausted is True
    assert status.estimated_episodes_remaining == 0
    assert status.status == "critical"


def test_zero_balance_is_critical_without_cost_history(db_session):
    save_openai_credit_snapshot(db_session, 0.0)

    status = _openai_status(db_session)

    assert status.balance_usd == 0.0
    assert status.estimated_episodes_remaining == 0
    assert status.status == "critical"
