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

    # Tagesschau episode: has segment, review_gate_translate, no adapt, no review_gate_2
    ts_stages = [s[0] for s in _get_stages(settings, _dummy_ep(2, "tagesschau_tr"))]

    assert "segment" in ts_stages
    assert "review_gate_translate" in ts_stages
    assert "adapt" not in ts_stages
    assert "review_gate_2" not in ts_stages

    # They must be different
    assert bitcoin_stages != ts_stages


def test_segment_stage_position_before_translate(settings):
    """segment stage must come before translate in tagesschau pipeline."""
    reset_registry()
    stages = [s[0] for s in _get_stages(settings, _dummy_ep(2, "tagesschau_tr"))]

    seg_idx = stages.index("segment")
    trans_idx = stages.index("translate")
    assert seg_idx < trans_idx


def test_v1_episode_gets_v1_stages():
    """V1 episodes always get v1 stage list regardless of profile."""
    reset_registry()
    # Use settings with pipeline_version=1 so v1 stages are returned
    v1_settings = Settings(profiles_dir="btcedu/profiles", pipeline_version=1)
    stages = [s[0] for s in _get_stages(v1_settings, _dummy_ep(1, "tagesschau_tr"))]
    assert "download" in stages
    assert "chunk" in stages
    assert "segment" not in stages
    assert "adapt" not in stages


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

        reports = run_pending(db_session, settings, profile="tagesschau_tr")

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
            reports = run_pending(db_session, settings)

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
    btc = _make_episode(db_session, "btc-005", "bitcoin_podcast")
    ts = _make_episode(db_session, "ts-005", "tagesschau_tr")

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
    """Tagesschau profile has higher TTS stability than default."""
    reset_registry()
    settings = Settings(profiles_dir="btcedu/profiles", pipeline_version=2)
    registry = get_registry(settings)

    ts = registry.get("tagesschau_tr")
    tts_cfg = ts.stage_config.get("tts", {})

    assert "stability" in tts_cfg
    assert tts_cfg["stability"] >= 0.6  # news requires higher stability


def test_bitcoin_profile_has_no_tts_override():
    """Bitcoin profile does not override TTS settings."""
    reset_registry()
    settings = Settings(profiles_dir="btcedu/profiles", pipeline_version=2)
    registry = get_registry(settings)

    btc = registry.get("bitcoin_podcast")
    tts_cfg = btc.stage_config.get("tts", {})

    # Bitcoin profile should not have a tts block or have empty voice_id
    assert not tts_cfg or not tts_cfg.get("voice_id")
