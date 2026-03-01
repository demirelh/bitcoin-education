"""Tests for Sprint 8: TTS stage implementation."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from btcedu.core.tts import (
    TTSResult,
    _compute_chapters_narration_hash,
    _compute_narration_hash,
    _create_silent_mp3,
    _is_tts_current,
    _mark_downstream_stale,
    generate_tts,
)
from btcedu.db import Base
from btcedu.models.chapter_schema import ChapterDocument
from btcedu.models.episode import Episode, EpisodeStatus


# Override conftest fixtures to also create media_assets table
@pytest.fixture
def db_engine():
    """In-memory SQLite with media_assets table for TTS tests."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with engine.connect() as conn:
        conn.execute(
            text(
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts "
                "USING fts5(chunk_id UNINDEXED, episode_id UNINDEXED, text)"
            )
        )
        # Create prompt_versions table (needed for FK in media_assets)
        conn.execute(
            text(
                """CREATE TABLE IF NOT EXISTS prompt_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(64) NOT NULL,
                    version INTEGER NOT NULL,
                    content_hash VARCHAR(64),
                    is_default INTEGER DEFAULT 0,
                    created_at DATETIME
                )"""
            )
        )
        conn.commit()
    # Create media_assets table via its own ORM Base
    # Must register prompt_versions in MediaBase metadata for FK resolution
    from sqlalchemy import Column, DateTime, Integer, String, Table

    from btcedu.models.media_asset import Base as MediaBase

    if "prompt_versions" not in MediaBase.metadata.tables:
        Table(
            "prompt_versions",
            MediaBase.metadata,
            Column("id", Integer, primary_key=True),
            Column("name", String(64)),
            Column("version", Integer),
            Column("content_hash", String(64)),
            Column("is_default", Integer),
            Column("created_at", DateTime),
        )
    MediaBase.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine):
    """Database session for TTS tests."""
    factory = sessionmaker(bind=db_engine)
    session = factory()
    yield session
    session.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

CHAPTERS_JSON = {
    "schema_version": "1.0",
    "episode_id": "ep_tts_test",
    "title": "Test TTS Episode",
    "total_chapters": 2,
    "estimated_duration_seconds": 200,
    "chapters": [
        {
            "chapter_id": "ch01",
            "title": "Giriş",
            "order": 1,
            "narration": {
                "text": "Merhaba dünya, bu bir test bölümüdür.",
                "word_count": 6,
                "estimated_duration_seconds": 100,
            },
            "visual": {
                "type": "title_card",
                "description": "Title card for intro",
            },
            "overlays": [],
            "transitions": {"in": "fade", "out": "fade"},
        },
        {
            "chapter_id": "ch02",
            "title": "Bitcoin Nedir",
            "order": 2,
            "narration": {
                "text": "Bitcoin merkezi olmayan bir dijital para birimidir.",
                "word_count": 7,
                "estimated_duration_seconds": 100,
            },
            "visual": {
                "type": "diagram",
                "description": "Bitcoin diagram",
                "image_prompt": "A Bitcoin network diagram",
            },
            "overlays": [],
            "transitions": {"in": "fade", "out": "fade"},
        },
    ],
}


def _make_chapters_doc():
    return ChapterDocument(**CHAPTERS_JSON)


def _make_settings(tmp_path):
    """Create minimal settings for TTS tests."""
    settings = MagicMock()
    settings.outputs_dir = str(tmp_path / "outputs")
    settings.dry_run = False
    settings.max_episode_cost_usd = 10.0
    settings.elevenlabs_api_key = "test_key"
    settings.elevenlabs_voice_id = "voice_123"
    settings.elevenlabs_model = "eleven_multilingual_v2"
    settings.elevenlabs_stability = 0.5
    settings.elevenlabs_similarity_boost = 0.75
    settings.elevenlabs_style = 0.0
    settings.elevenlabs_use_speaker_boost = True
    return settings


def _setup_episode(db_session, tmp_path, status=EpisodeStatus.IMAGES_GENERATED):
    """Create test episode and chapters.json."""
    episode = Episode(
        episode_id="ep_tts_test",
        source="youtube_rss",
        title="Test TTS Episode",
        url="https://youtube.com/watch?v=ep_tts_test",
        status=status,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    # Write chapters.json
    chapters_dir = tmp_path / "outputs" / "ep_tts_test"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    (chapters_dir / "chapters.json").write_text(
        json.dumps(CHAPTERS_JSON, ensure_ascii=False), encoding="utf-8"
    )

    return episode


# ---------------------------------------------------------------------------
# Hash computation tests
# ---------------------------------------------------------------------------


def test_compute_narration_hash_stable():
    """Same text produces same hash."""
    h1 = _compute_narration_hash("Hello world")
    h2 = _compute_narration_hash("Hello world")
    assert h1 == h2
    assert h1.startswith("sha256:")


def test_compute_narration_hash_changes():
    """Different text produces different hash."""
    h1 = _compute_narration_hash("Hello world")
    h2 = _compute_narration_hash("Hello worlds")
    assert h1 != h2


def test_compute_chapters_narration_hash_stable():
    """Same chapters produce same narration hash."""
    doc = _make_chapters_doc()
    h1 = _compute_chapters_narration_hash(doc)
    h2 = _compute_chapters_narration_hash(doc)
    assert h1 == h2


def test_compute_chapters_narration_hash_changes_on_narration_change():
    """Narration change produces different hash."""
    doc1 = _make_chapters_doc()
    h1 = _compute_chapters_narration_hash(doc1)

    modified = CHAPTERS_JSON.copy()
    modified["chapters"] = [dict(c) for c in CHAPTERS_JSON["chapters"]]
    modified["chapters"][0] = dict(modified["chapters"][0])
    modified["chapters"][0]["narration"] = dict(modified["chapters"][0]["narration"])
    modified["chapters"][0]["narration"]["text"] = "Completely different narration text."

    doc2 = ChapterDocument(**modified)
    h2 = _compute_chapters_narration_hash(doc2)
    assert h1 != h2


def test_compute_chapters_narration_hash_ignores_visual_changes():
    """Visual changes don't affect narration hash."""
    doc1 = _make_chapters_doc()
    h1 = _compute_chapters_narration_hash(doc1)

    modified = json.loads(json.dumps(CHAPTERS_JSON))
    modified["chapters"][0]["visual"]["description"] = "Completely different visual"

    doc2 = ChapterDocument(**modified)
    h2 = _compute_chapters_narration_hash(doc2)
    assert h1 == h2


# ---------------------------------------------------------------------------
# _is_tts_current tests
# ---------------------------------------------------------------------------


def test_is_tts_current_missing_manifest(tmp_path):
    """Missing manifest → not current."""
    manifest = tmp_path / "manifest.json"
    provenance = tmp_path / "provenance.json"
    assert _is_tts_current(manifest, provenance, "hash") is False


def test_is_tts_current_stale_marker(tmp_path):
    """Stale marker → not current."""
    manifest = tmp_path / "manifest.json"
    provenance = tmp_path / "provenance.json"
    manifest.write_text(json.dumps({"segments": []}))
    provenance.write_text(json.dumps({"input_content_hash": "hash"}))

    # Create stale marker
    stale = manifest.with_suffix(".json.stale")
    stale.write_text("stale")

    assert _is_tts_current(manifest, provenance, "hash") is False


def test_is_tts_current_hash_mismatch(tmp_path):
    """Hash mismatch → not current."""
    manifest = tmp_path / "manifest.json"
    provenance = tmp_path / "provenance.json"
    manifest.write_text(json.dumps({"segments": []}))
    provenance.write_text(json.dumps({"input_content_hash": "old_hash"}))

    assert _is_tts_current(manifest, provenance, "new_hash") is False


def test_is_tts_current_missing_mp3(tmp_path):
    """Missing MP3 file → not current."""
    tts_dir = tmp_path / "tts"
    tts_dir.mkdir()
    manifest = tts_dir / "manifest.json"
    provenance = tmp_path / "provenance.json"

    manifest.write_text(
        json.dumps(
            {
                "segments": [
                    {"chapter_id": "ch01", "file_path": "tts/ch01.mp3"}
                ]
            }
        )
    )
    provenance.write_text(json.dumps({"input_content_hash": "hash"}))

    # mp3 doesn't exist
    assert _is_tts_current(manifest, provenance, "hash") is False


def test_is_tts_current_all_good(tmp_path):
    """All checks pass → current."""
    tts_dir = tmp_path / "tts"
    tts_dir.mkdir()
    manifest = tts_dir / "manifest.json"
    provenance = tmp_path / "provenance.json"

    # Create MP3 file
    (tmp_path / "tts" / "ch01.mp3").write_bytes(b"fake_mp3")

    manifest.write_text(
        json.dumps(
            {
                "segments": [
                    {"chapter_id": "ch01", "file_path": "tts/ch01.mp3"}
                ]
            }
        )
    )
    provenance.write_text(json.dumps({"input_content_hash": "hash"}))

    assert _is_tts_current(manifest, provenance, "hash") is True


# ---------------------------------------------------------------------------
# _mark_downstream_stale tests
# ---------------------------------------------------------------------------


def test_mark_downstream_stale_no_render(tmp_path):
    """No render file → no stale marker created."""
    _mark_downstream_stale("ep1", tmp_path)
    assert not (tmp_path / "ep1" / "render" / "draft.mp4.stale").exists()


def test_mark_downstream_stale_with_render(tmp_path):
    """Existing render file → stale marker created."""
    render_dir = tmp_path / "ep1" / "render"
    render_dir.mkdir(parents=True)
    (render_dir / "draft.mp4").write_bytes(b"fake_video")

    _mark_downstream_stale("ep1", tmp_path)

    stale_marker = render_dir / "draft.mp4.stale"
    assert stale_marker.exists()
    data = json.loads(stale_marker.read_text())
    assert data["invalidated_by"] == "tts"
    assert data["reason"] == "audio_changed"


# ---------------------------------------------------------------------------
# _create_silent_mp3
# ---------------------------------------------------------------------------


def test_create_silent_mp3():
    """Silent MP3 is non-empty bytes."""
    data = _create_silent_mp3()
    assert isinstance(data, bytes)
    assert len(data) > 0
    # Starts with MP3 sync word
    assert data[0] == 0xFF
    assert (data[1] & 0xE0) == 0xE0


# ---------------------------------------------------------------------------
# generate_tts integration tests
# ---------------------------------------------------------------------------


def test_generate_tts_episode_not_found(db_session, tmp_path):
    """Missing episode raises ValueError."""
    settings = _make_settings(tmp_path)
    with pytest.raises(ValueError, match="Episode not found"):
        generate_tts(db_session, "nonexistent", settings)


def test_generate_tts_v1_rejected(db_session, tmp_path):
    """V1 episode raises ValueError."""
    episode = Episode(
        episode_id="ep_v1",
        source="youtube_rss",
        title="V1 Episode",
        url="https://youtube.com/watch?v=ep_v1",
        status=EpisodeStatus.IMAGES_GENERATED,
        pipeline_version=1,
    )
    db_session.add(episode)
    db_session.commit()

    settings = _make_settings(tmp_path)
    with pytest.raises(ValueError, match="v1 pipeline"):
        generate_tts(db_session, "ep_v1", settings)


def test_generate_tts_wrong_status(db_session, tmp_path):
    """Wrong status raises ValueError."""
    episode = Episode(
        episode_id="ep_wrong",
        source="youtube_rss",
        title="Wrong Status",
        url="https://youtube.com/watch?v=ep_wrong",
        status=EpisodeStatus.CHAPTERIZED,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    settings = _make_settings(tmp_path)
    with pytest.raises(ValueError, match="expected 'images_generated'"):
        generate_tts(db_session, "ep_wrong", settings)


@patch("btcedu.services.elevenlabs_service.ElevenLabsService")
def test_generate_tts_dry_run(mock_service_cls, db_session, tmp_path):
    """Dry-run produces manifest with silent MP3 placeholders."""
    episode = _setup_episode(db_session, tmp_path)
    settings = _make_settings(tmp_path)
    settings.dry_run = True

    result = generate_tts(db_session, episode.episode_id, settings)

    assert not result.skipped
    assert result.segment_count == 2
    assert result.cost_usd == 0.0  # Dry-run is free

    # Manifest was written
    assert result.manifest_path.exists()
    manifest = json.loads(result.manifest_path.read_text())
    assert len(manifest["segments"]) == 2

    # MP3 files exist
    tts_dir = tmp_path / "outputs" / "ep_tts_test" / "tts"
    assert (tts_dir / "ch01.mp3").exists()
    assert (tts_dir / "ch02.mp3").exists()

    # Episode status updated
    db_session.refresh(episode)
    assert episode.status == EpisodeStatus.TTS_DONE

    # TTS service synthesize was NOT called (dry-run)
    if mock_service_cls.return_value.synthesize.called:
        raise AssertionError("synthesize() should not be called in dry-run mode")


@patch("btcedu.services.elevenlabs_service.ElevenLabsService")
def test_generate_tts_happy_path(mock_service_cls, db_session, tmp_path):
    """Full TTS generation with mocked service."""
    episode = _setup_episode(db_session, tmp_path)
    settings = _make_settings(tmp_path)

    # Mock the TTS service
    mock_service = MagicMock()
    mock_service_cls.return_value = mock_service

    from btcedu.services.elevenlabs_service import TTSResponse

    mock_service.synthesize.return_value = TTSResponse(
        audio_bytes=b"fake_mp3_data_here",
        duration_seconds=10.5,
        sample_rate=44100,
        model="eleven_multilingual_v2",
        voice_id="voice_123",
        character_count=100,
        cost_usd=0.03,
    )

    result = generate_tts(db_session, episode.episode_id, settings)

    assert not result.skipped
    assert result.segment_count == 2
    assert result.cost_usd == pytest.approx(0.06)  # 2 chapters * 0.03
    assert result.total_duration_seconds == pytest.approx(21.0)

    # Manifest written
    manifest = json.loads(result.manifest_path.read_text())
    assert manifest["episode_id"] == "ep_tts_test"
    assert len(manifest["segments"]) == 2
    assert manifest["segments"][0]["chapter_id"] == "ch01"

    # Provenance written
    assert result.provenance_path.exists()
    provenance = json.loads(result.provenance_path.read_text())
    assert provenance["stage"] == "tts"
    assert provenance["model"] == "elevenlabs"

    # Episode status updated
    db_session.refresh(episode)
    assert episode.status == EpisodeStatus.TTS_DONE

    # MediaAsset records created
    from btcedu.models.media_asset import MediaAsset, MediaAssetType

    assets = (
        db_session.query(MediaAsset)
        .filter(MediaAsset.episode_id == "ep_tts_test")
        .filter(MediaAsset.asset_type == MediaAssetType.AUDIO)
        .all()
    )
    assert len(assets) == 2


@patch("btcedu.services.elevenlabs_service.ElevenLabsService")
def test_generate_tts_idempotency(mock_service_cls, db_session, tmp_path):
    """Second run with unchanged content is skipped."""
    episode = _setup_episode(db_session, tmp_path)
    settings = _make_settings(tmp_path)
    settings.dry_run = True

    # First run
    result1 = generate_tts(db_session, episode.episode_id, settings)
    assert not result1.skipped

    # Second run should be skipped (same content, manifest + provenance exist)
    result2 = generate_tts(db_session, episode.episode_id, settings)
    assert result2.skipped


@patch("btcedu.services.elevenlabs_service.ElevenLabsService")
def test_generate_tts_single_chapter(mock_service_cls, db_session, tmp_path):
    """Single chapter regeneration via chapter_id parameter."""
    episode = _setup_episode(db_session, tmp_path)
    settings = _make_settings(tmp_path)
    settings.dry_run = True

    result = generate_tts(db_session, episode.episode_id, settings, chapter_id="ch02")

    assert not result.skipped
    assert result.segment_count == 1

    manifest = json.loads(result.manifest_path.read_text())
    assert len(manifest["segments"]) == 1
    assert manifest["segments"][0]["chapter_id"] == "ch02"


@patch("btcedu.services.elevenlabs_service.ElevenLabsService")
def test_generate_tts_cost_limit(mock_service_cls, db_session, tmp_path):
    """Cost limit enforcement stops generation."""
    episode = _setup_episode(db_session, tmp_path)
    settings = _make_settings(tmp_path)
    settings.max_episode_cost_usd = 0.001  # Very low limit

    mock_service = MagicMock()
    mock_service_cls.return_value = mock_service

    from btcedu.services.elevenlabs_service import TTSResponse

    mock_service.synthesize.return_value = TTSResponse(
        audio_bytes=b"fake",
        duration_seconds=10.0,
        sample_rate=44100,
        model="eleven_multilingual_v2",
        voice_id="voice_123",
        character_count=1000,
        cost_usd=5.0,  # Exceeds limit
    )

    # First chapter exceeds limit, so second should be blocked
    # But the first chapter itself triggers cost_usd from the response,
    # which isn't checked until after generation. The cost guard checks
    # cumulative _before_ generation, so if episode total + total_cost > limit:
    # With max=0.001 and no prior costs, the first chapter itself will exceed
    # when the second chapter is about to start.
    # Actually cost guard checks BEFORE generating each chapter.
    # Since _get_episode_total_cost returns 0 for a fresh episode,
    # and total_cost starts at 0, the first chapter passes.
    # After first chapter, total_cost = 5.0 > 0.001, so second is blocked.
    with pytest.raises(RuntimeError, match="cost limit exceeded"):
        generate_tts(db_session, episode.episode_id, settings)


# ---------------------------------------------------------------------------
# Pipeline V2 stages include TTS
# ---------------------------------------------------------------------------


def test_v2_stages_include_tts():
    """Pipeline _V2_STAGES includes TTS entry."""
    from btcedu.core.pipeline import _V2_STAGES

    stage_names = [name for name, _ in _V2_STAGES]
    assert "tts" in stage_names

    # Find TTS entry
    tts_entry = next((name, status) for name, status in _V2_STAGES if name == "tts")
    assert tts_entry[1] == EpisodeStatus.IMAGES_GENERATED


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
