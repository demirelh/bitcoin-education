"""Phase 8 requirement F: corrected episode-cost FK helpers + cost-guard.

The tts/image_generator/anchor_generator ``_get_episode_total_cost`` helpers
previously filtered the integer ``PipelineRun.episode_id`` FK with the *string*
episode_id and so counted nothing — the cost guard and dashboard undercounted.
They now resolve the string to ``episode.id`` and count every prior stage.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from btcedu.core.anchor_generator import _get_episode_total_cost as anchor_cost
from btcedu.core.image_generator import _get_episode_total_cost as image_cost
from btcedu.core.tts import _get_episode_total_cost as tts_cost
from btcedu.db import Base
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun, RunStatus


@pytest.fixture
def db_session():
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
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


def _episode_with_costs(db_session, costs):
    ep = Episode(
        episode_id="ep_cost",
        source="youtube_rss",
        title="Cost",
        url="https://x",
        status=EpisodeStatus.IMAGES_GENERATED,
        pipeline_version=2,
    )
    db_session.add(ep)
    db_session.commit()
    for i, c in enumerate(costs):
        db_session.add(
            PipelineRun(
                episode_id=ep.id,  # integer FK — the correct binding
                stage=f"stage{i}",
                status=RunStatus.SUCCESS.value,
                estimated_cost_usd=c,
            )
        )
    db_session.commit()
    return ep


@pytest.mark.parametrize("helper", [tts_cost, image_cost, anchor_cost])
def test_cost_helper_counts_upstream_int_keyed_runs(db_session, helper):
    _episode_with_costs(db_session, [3.0, 4.0, 0.5])
    # Resolves "ep_cost" -> episode.id and sums every prior stage.
    assert helper(db_session, "ep_cost") == pytest.approx(7.5)


@pytest.mark.parametrize("helper", [tts_cost, image_cost, anchor_cost])
def test_cost_helper_zero_for_unknown_episode(db_session, helper):
    assert helper(db_session, "does_not_exist") == 0.0


def test_cost_helpers_agree_with_dashboard_int_filter(db_session):
    """All three paid-stage helpers agree with an int-FK sum (dashboard parity)."""
    from sqlalchemy import func

    ep = _episode_with_costs(db_session, [2.0, 2.5])
    dashboard = float(
        db_session.query(func.coalesce(func.sum(PipelineRun.estimated_cost_usd), 0.0))
        .filter(PipelineRun.episode_id == ep.id)
        .scalar()
    )
    assert dashboard == pytest.approx(4.5)
    for helper in (tts_cost, image_cost, anchor_cost):
        assert helper(db_session, "ep_cost") == pytest.approx(dashboard)


def _tts_settings(tmp_path):
    s = MagicMock()
    s.outputs_dir = str(tmp_path / "outputs")
    s.dry_run = True
    s.max_episode_cost_usd = 10.0
    s.elevenlabs_api_key = "k"
    s.elevenlabs_voice_id = "v"
    s.elevenlabs_model = "eleven_x"
    s.elevenlabs_stability = 0.5
    s.elevenlabs_similarity_boost = 0.75
    s.elevenlabs_style = 0.0
    s.elevenlabs_use_speaker_boost = True
    s.elevenlabs_speed = 1.0
    return s


@patch("btcedu.services.elevenlabs_service.ElevenLabsService")
def test_tts_cost_guard_counts_upstream_over_budget(mock_service, db_session, tmp_path):
    """With the FK fix, an over-budget UPSTREAM (int-keyed) cost blocks TTS."""
    from btcedu.core.tts import generate_tts

    settings = _tts_settings(tmp_path)
    ep = Episode(
        episode_id="ep_guard",
        source="youtube_rss",
        title="Guard",
        url="https://x",
        status=EpisodeStatus.IMAGES_GENERATED,
        pipeline_version=2,
    )
    db_session.add(ep)
    db_session.commit()
    # Prior stage already blew the budget (int-keyed — invisible to the OLD helper).
    db_session.add(
        PipelineRun(
            episode_id=ep.id,
            stage="translate",
            status=RunStatus.SUCCESS.value,
            estimated_cost_usd=10.01,
        )
    )
    db_session.commit()

    ep_dir = Path(settings.outputs_dir) / "ep_guard"
    ep_dir.mkdir(parents=True)
    (ep_dir / "chapters.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "episode_id": "ep_guard",
                "title": "T",
                "total_chapters": 1,
                "estimated_duration_seconds": 40,
                "chapters": [
                    {
                        "chapter_id": "ch01",
                        "title": "C",
                        "order": 1,
                        "narration": {
                            "text": "Bir metin.",
                            "word_count": 2,
                            "estimated_duration_seconds": 40,
                        },
                        "visual": {"type": "b_roll", "description": "d", "image_prompt": "p"},
                        "overlays": [],
                        "transitions": {"in": "fade", "out": "cut"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    from btcedu.services.errors import ErrorCategory, PipelineError

    with pytest.raises(PipelineError) as exc_info:
        generate_tts(db_session, "ep_guard", settings)
    assert exc_info.value.category == ErrorCategory.PERMANENT_COST_LIMIT
    db_session.refresh(ep)
    assert ep.status == EpisodeStatus.COST_LIMIT
