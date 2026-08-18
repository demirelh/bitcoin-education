"""Phase 8 requirement B (deterministic exact-data visuals) + C (profile TTS).

Deterministic visuals for exact-data categories are rendered locally from their
spec (never a generative model), provider routing for other chapters is
untouched, and TTS honours the profile voice AND model (previously the model was
ignored and the wrong top-level voice was written).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from btcedu.db import Base
from btcedu.models.chapter_schema import Chapter
from btcedu.models.episode import Episode, EpisodeStatus
from btcedu.models.media_asset import Base as MediaBase
from btcedu.profiles import get_registry, reset_registry


@pytest.fixture(autouse=True)
def _leave_real_profiles_loaded():
    """Guarantee the profile-registry singleton holds real profiles after each
    test here, so a local ``reset_registry()`` never leaves it empty for later
    test files (the registry is a process-wide singleton)."""
    from btcedu.config import Settings

    yield
    reset_registry()
    get_registry(Settings())


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    MediaBase.metadata.create_all(engine)  # MediaAsset uses its own declarative_base()
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


def _chapter(visual: dict):
    return Chapter(
        chapter_id="ch01",
        title="Hava Durumu",
        order=1,
        narration={"text": "yarin hava", "word_count": 2, "estimated_duration_seconds": 5},
        visual=visual,
        overlays=[],
        transitions={"in": "fade", "out": "cut"},
    )


# ---------------------------------------------------------------------------
# Deterministic exact-data visuals
# ---------------------------------------------------------------------------


def test_explicit_deterministic_spec_routes_local_not_generative():
    from btcedu.core.image_generator import _should_render_deterministic

    ch = _chapter(
        {
            "type": "diagram",
            "description": "weather map",
            "deterministic": {
                "category": "weather",
                "title": "Hava Durumu",
                "items": [{"label": "Berlin", "value": "25°C"}],
            },
        }
    )
    # Explicit spec -> always deterministic, regardless of profile flags.
    assert _should_render_deterministic(ch.visual, {}) is True


def test_profile_flag_plus_keyword_routes_deterministic_but_default_does_not():
    from btcedu.core.image_generator import _should_render_deterministic

    ch = _chapter({"type": "diagram", "description": "Almanya hava durumu", "image_prompt": "x"})
    # Default (no flag, no spec) keeps generative provider routing.
    assert _should_render_deterministic(ch.visual, {}) is False
    # Opt-in profile flag + detected weather keyword -> deterministic.
    assert _should_render_deterministic(ch.visual, {"deterministic_exact_data": True}) is True


def test_normal_visual_is_never_forced_deterministic():
    from btcedu.core.image_generator import _should_render_deterministic

    ch = _chapter({"type": "b_roll", "description": "a politician speaking", "image_prompt": "p"})
    assert _should_render_deterministic(ch.visual, {"deterministic_exact_data": True}) is False


def test_render_deterministic_visual_uses_exact_spec_values(tmp_path):
    from unittest.mock import patch

    from btcedu.core.image_generator import _render_deterministic_visual

    ch = _chapter(
        {
            "type": "diagram",
            "description": "weather",
            "deterministic": {
                "category": "weather",
                "title": "Hava Durumu",
                "items": [
                    {"label": "Berlin", "value": "25°C"},
                    {"label": "München", "value": "19°C"},
                ],
            },
        }
    )
    out = tmp_path / "images"
    out.mkdir()
    with patch("btcedu.core.weather.renderer._find_chromium", return_value=None):
        entry = _render_deterministic_visual(ch, out)

    assert entry.generation_method == "deterministic"
    assert entry.model is None
    assert entry.metadata["provider"] == "weather_renderer"
    assert entry.metadata["cost_usd"] == 0.0
    assert entry.metadata["category"] == "weather"
    assert (out / Path(entry.file_path).name).exists()


def test_deterministic_spec_satisfies_schema_without_image_prompt():
    """A diagram with a deterministic spec is valid even without image_prompt."""
    ch = _chapter(
        {
            "type": "diagram",
            "description": "election results",
            "deterministic": {"category": "election", "items": []},
        }
    )
    assert ch.visual.image_prompt is None
    assert ch.visual.deterministic["category"] == "election"


# ---------------------------------------------------------------------------
# TTS honours profile voice AND model
# ---------------------------------------------------------------------------


def test_resolve_tts_config_uses_profile_model_and_voice(tmp_path):
    from btcedu.core.tts import _resolve_tts_config

    reset_registry()
    from btcedu.config import Settings

    settings = Settings(outputs_dir=str(tmp_path / "o"))
    episode = MagicMock()
    episode.content_profile = "tagesschau_tr"

    cfg = _resolve_tts_config(episode, settings)
    assert cfg["model"] == "eleven_turbo_v2_5"  # profile model, not the global default
    assert cfg["voice_id"] == "Q2IX97JeHBY3vNGzgM5s"
    assert cfg["speed"] == 1.0
    assert isinstance(cfg["pronunciation_lexicon"], dict)


@patch("btcedu.services.elevenlabs_service.ElevenLabsService")
def test_tts_manifest_writes_resolved_profile_voice_and_model(mock_service, db_session, tmp_path):
    from btcedu.config import Settings
    from btcedu.core.tts import generate_tts

    reset_registry()
    settings = Settings(
        outputs_dir=str(tmp_path / "outputs"), dry_run=True, max_episode_cost_usd=10.0
    )

    ep = Episode(
        episode_id="ep_news_tts",
        source="tagesschau_rss",
        title="tagesschau",
        url="https://x",
        status=EpisodeStatus.IMAGES_GENERATED,
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    db_session.add(ep)
    db_session.commit()

    ep_dir = Path(settings.outputs_dir) / "ep_news_tts"
    ep_dir.mkdir(parents=True)
    (ep_dir / "chapters.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "episode_id": "ep_news_tts",
                "title": "T",
                "total_chapters": 1,
                "estimated_duration_seconds": 40,
                "chapters": [
                    {
                        "chapter_id": "ch01",
                        "title": "C",
                        "order": 1,
                        "narration": {
                            "text": "Haber metni.",
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

    result = generate_tts(db_session, "ep_news_tts", settings)
    manifest = json.loads(result.manifest_path.read_text())
    # Top-level voice/model reflect the PROFILE, not the global settings default.
    assert manifest["voice_id"] == "Q2IX97JeHBY3vNGzgM5s"
    assert manifest["model"] == "eleven_turbo_v2_5"
    assert "voice_config" in manifest


# ---------------------------------------------------------------------------
# render_is_current public validation helper (used by the publish gate)
# ---------------------------------------------------------------------------


def _write_render_scene(settings, episode_id, narration="Bir haber.", duration=40.0):
    base = Path(settings.outputs_dir) / episode_id
    (base / "render").mkdir(parents=True, exist_ok=True)
    (base / "images").mkdir(parents=True, exist_ok=True)
    (base / "tts").mkdir(parents=True, exist_ok=True)
    (base / "provenance").mkdir(parents=True, exist_ok=True)
    (base / "chapters.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "episode_id": episode_id,
                "title": "T",
                "total_chapters": 1,
                "estimated_duration_seconds": 40,
                "chapters": [
                    {
                        "chapter_id": "ch01",
                        "title": "C",
                        "order": 1,
                        "narration": {
                            "text": narration,
                            "word_count": len(narration.split()),
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
    (base / "images" / "manifest.json").write_text(
        json.dumps(
            {
                "images": [
                    {
                        "chapter_id": "ch01",
                        "file_path": "images/ch01.png",
                        "generation_method": "flux",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (base / "tts" / "manifest.json").write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "chapter_id": "ch01",
                        "file_path": "tts/ch01.mp3",
                        "duration_seconds": duration,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (base / "render" / "draft.mp4").write_bytes(b"video bytes")
    (base / "render" / "render_manifest.json").write_text(
        json.dumps({"segments": [{"chapter_id": "ch01"}]}),
        encoding="utf-8",
    )
    return base


def test_render_is_current_true_then_false_after_change(db_session, tmp_path):
    from btcedu.config import Settings
    from btcedu.core.renderer import _current_render_content_hash, render_is_current

    reset_registry()
    settings = Settings(outputs_dir=str(tmp_path / "outputs"))
    ep = Episode(
        episode_id="ep_render",
        source="youtube_rss",
        title="R",
        url="https://x",
        status=EpisodeStatus.RENDERED,
        pipeline_version=2,
    )
    db_session.add(ep)
    db_session.commit()

    base = _write_render_scene(settings, "ep_render")
    content_hash = _current_render_content_hash(db_session, "ep_render", settings)
    (base / "provenance" / "render_provenance.json").write_text(
        json.dumps({"input_content_hash": content_hash}), encoding="utf-8"
    )

    ok, _ = render_is_current(db_session, "ep_render", settings)
    assert ok is True

    # Change a render-relevant input (TTS segment duration) -> not current.
    _write_render_scene(settings, "ep_render", duration=99.0)
    ok2, reason = render_is_current(db_session, "ep_render", settings)
    assert ok2 is False and "changed" in reason


def test_render_is_current_false_when_draft_empty(db_session, tmp_path):
    from btcedu.config import Settings
    from btcedu.core.renderer import render_is_current

    reset_registry()
    settings = Settings(outputs_dir=str(tmp_path / "outputs"))
    ep = Episode(
        episode_id="ep_empty",
        source="youtube_rss",
        title="R",
        url="https://x",
        status=EpisodeStatus.RENDERED,
        pipeline_version=2,
    )
    db_session.add(ep)
    db_session.commit()
    base = _write_render_scene(settings, "ep_empty")
    (base / "render" / "draft.mp4").write_bytes(b"")  # empty draft
    ok, reason = render_is_current(db_session, "ep_empty", settings)
    assert ok is False and "empty" in reason
