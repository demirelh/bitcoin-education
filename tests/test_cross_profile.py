"""Cross-profile isolation and metadata tests (Phase 4)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core.pipeline import _get_stages, run_pending
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.profiles import get_registry, reset_registry

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url="sqlite:///:memory:",
        outputs_dir=str(tmp_path / "outputs"),
        pipeline_version=2,
        profiles_dir="btcedu/profiles",
    )


@pytest.fixture
def db_session():
    from btcedu.db import Base
    from btcedu.models.media_asset import Base as MediaBase  # separate declarative_base

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    MediaBase.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    session = factory()
    yield session
    session.close()
    engine.dispose()


def _make_episode(session, episode_id: str, profile: str = "bitcoin_podcast") -> Episode:
    ep = Episode(
        episode_id=episode_id,
        title=f"Episode {episode_id}",
        url=f"https://example.com/{episode_id}",
        status=EpisodeStatus.NEW,
        detected_at=datetime.now(UTC),
        pipeline_version=2,
        content_profile=profile,
    )
    session.add(ep)
    session.commit()
    return ep


# ---------------------------------------------------------------------------
# Stage list tests
# ---------------------------------------------------------------------------


def _dummy_ep(pipeline_version: int = 2, content_profile: str = "bitcoin_podcast"):
    """Create a minimal episode-like object for stage testing (no ORM)."""
    return SimpleNamespace(pipeline_version=pipeline_version, content_profile=content_profile)


def test_bitcoin_and_tagesschau_stages_are_different(settings):
    """Verify the two profiles produce distinct stage lists."""
    reset_registry()

    # Bitcoin episode: has adapt, review_gate_2, no segment
    bitcoin_stages = [s[0] for s in _get_stages(settings, _dummy_ep(2, "bitcoin_podcast"))]

    assert "adapt" in bitcoin_stages
    assert "review_gate_2" in bitcoin_stages
    assert "segment" not in bitcoin_stages
    assert "review_gate_translate" not in bitcoin_stages

    # Tagesschau episode: adds segment while retaining the shared v2 gates
    ts_stages = [s[0] for s in _get_stages(settings, _dummy_ep(2, "tagesschau_tr"))]

    assert "segment" in ts_stages
    assert "adapt" in ts_stages
    assert "review_gate_2" in ts_stages

    # They must be different
    assert bitcoin_stages != ts_stages


def test_segment_stage_position_before_translate(settings):
    """segment stage must come before translate in tagesschau pipeline."""
    reset_registry()
    stages = [s[0] for s in _get_stages(settings, _dummy_ep(2, "tagesschau_tr"))]

    seg_idx = stages.index("segment")
    trans_idx = stages.index("translate")
    assert seg_idx < trans_idx


# ---------------------------------------------------------------------------
# Profile isolation tests
# ---------------------------------------------------------------------------


def test_profile_episode_fields_are_independent(db_session, settings):
    """Bitcoin and tagesschau episodes store separate profile fields."""
    reset_registry()
    btc = _make_episode(db_session, "btc-001", "bitcoin_podcast")
    ts = _make_episode(db_session, "ts-001", "tagesschau_tr")

    assert btc.content_profile == "bitcoin_podcast"
    assert ts.content_profile == "tagesschau_tr"


def test_run_pending_profile_filter(db_session, settings):
    """run_pending with profile= only returns episodes with that profile."""
    reset_registry()
    _make_episode(db_session, "btc-001", "bitcoin_podcast")
    _make_episode(db_session, "ts-001", "tagesschau_tr")

    # Mock pipeline execution to avoid actual processing
    with patch("btcedu.core.pipeline.run_episode_pipeline") as mock_run:
        mock_run.return_value = MagicMock(success=True, stages=[], total_cost_usd=0.0)

        run_pending(db_session, settings, profile="tagesschau_tr")

    # Only tagesschau episode should have been processed
    assert mock_run.call_count == 1
    called_episode = mock_run.call_args[0][1]
    assert called_episode.content_profile == "tagesschau_tr"


def test_run_pending_no_filter_processes_all(db_session, settings):
    """run_pending without profile filter processes all profiles."""
    reset_registry()
    _make_episode(db_session, "btc-002", "bitcoin_podcast")
    _make_episode(db_session, "ts-002", "tagesschau_tr")

    with patch("btcedu.core.pipeline.run_episode_pipeline") as mock_run:
        mock_run.return_value = MagicMock(success=True, stages=[], total_cost_usd=0.0)
        # Patch has_pending_review at the reviewer module (lazy imported in pipeline)
        with patch("btcedu.core.reviewer.has_pending_review", return_value=False):
            run_pending(db_session, settings)

    assert mock_run.call_count == 2


# ---------------------------------------------------------------------------
# YouTube metadata tests
# ---------------------------------------------------------------------------


def test_youtube_metadata_differs_by_profile(settings, db_session, tmp_path):
    """Publisher builds different tags/category for each profile."""
    from btcedu.core.publisher import _build_youtube_metadata

    reset_registry()

    # Bitcoin episode
    btc = _make_episode(db_session, "btc-003", "bitcoin_podcast")
    title_btc, desc_btc, tags_btc = _build_youtube_metadata(btc, settings, session=db_session)
    assert "bitcoin" in tags_btc or "Bitcoin" in tags_btc

    # Tagesschau episode
    ts = _make_episode(db_session, "ts-003", "tagesschau_tr")
    title_ts, desc_ts, tags_ts = _build_youtube_metadata(ts, settings, session=db_session)
    assert "haberler" in tags_ts or "tagesschau" in tags_ts

    # Tags must be different
    assert set(tags_btc) != set(tags_ts)


def test_news_description_includes_attribution(settings, db_session, tmp_path):
    """Tagesschau episodes get source attribution in description."""
    from btcedu.core.publisher import _build_youtube_metadata

    reset_registry()
    ts = _make_episode(db_session, "ts-004", "tagesschau_tr")
    _, desc, _ = _build_youtube_metadata(ts, settings, session=db_session)
    assert "tagesschau" in desc.lower() or "ARD" in desc


# ---------------------------------------------------------------------------
# Stock image domain tag tests
# ---------------------------------------------------------------------------


def test_stock_domain_tag_differs_by_profile(settings, db_session):
    """_load_episode_profile returns correct domain per profile."""
    from btcedu.core.stock_images import _load_episode_profile

    reset_registry()
    _make_episode(db_session, "btc-005", "bitcoin_podcast")
    _make_episode(db_session, "ts-005", "tagesschau_tr")

    btc_profile = _load_episode_profile(db_session, "btc-005", settings)
    ts_profile = _load_episode_profile(db_session, "ts-005", settings)

    assert btc_profile is not None
    assert ts_profile is not None
    assert btc_profile.domain == "cryptocurrency"
    assert ts_profile.domain == "news"


# ---------------------------------------------------------------------------
# Renderer accent color tests
# ---------------------------------------------------------------------------


def test_accent_color_from_profile():
    """Bitcoin profile has orange, tagesschau has blue accent color."""
    reset_registry()
    settings = Settings(profiles_dir="btcedu/profiles", pipeline_version=2)
    registry = get_registry(settings)

    btc = registry.get("bitcoin_podcast")
    ts = registry.get("tagesschau_tr")

    btc_accent = btc.stage_config.get("render", {}).get("accent_color", "#F7931A")
    ts_accent = ts.stage_config.get("render", {}).get("accent_color", "#F7931A")

    assert btc_accent == "#F7931A"
    assert ts_accent == "#004B87"
    assert btc_accent != ts_accent


# ---------------------------------------------------------------------------
# TTS profile config tests
# ---------------------------------------------------------------------------


def test_tts_profile_config_values():
    """Tagesschau profile declares tuned TTS voice settings.

    The news voice was retuned for a livelier, less monotone delivery
    (lower stability + some style + slightly faster speed), so it no longer
    uses the very high stability that produced a flat, tiring narration.
    """
    reset_registry()
    settings = Settings(profiles_dir="btcedu/profiles", pipeline_version=2)
    registry = get_registry(settings)

    ts = registry.get("tagesschau_tr")
    tts_cfg = ts.stage_config.get("tts", {})

    assert tts_cfg.get("voice_id")  # explicit news voice (Irem)
    assert "stability" in tts_cfg
    # High stability keeps the emphasis even from one take to the next. At 0.45
    # the variants ElevenLabs produced stressed different words, which is how
    # some evenings came out sounding wrong while others were fine.
    assert 0.5 <= tts_cfg["stability"] <= 0.8
    assert tts_cfg.get("style", 0.0) > 0.0  # some expressiveness
    # Not faster than normal. An anchor lands the sentence and leaves a beat;
    # at speed 1.08 the sentences ran into each other and sounded rushed.
    assert tts_cfg.get("speed", 1.0) <= 1.0


def test_bitcoin_profile_has_tts_voice():
    """Bitcoin profile declares TTS voice settings."""
    reset_registry()
    settings = Settings(profiles_dir="btcedu/profiles", pipeline_version=2)
    registry = get_registry(settings)

    btc = registry.get("bitcoin_podcast")
    tts_cfg = btc.stage_config.get("tts", {})

    assert tts_cfg.get("voice_id")
