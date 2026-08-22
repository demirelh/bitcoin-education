"""Tests for Sprint 8: TTS stage implementation."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core.tts import (
    _adaptive_retry_limit,
    _compute_chapters_narration_hash,
    _compute_narration_hash,
    _compute_tts_content_hash,
    _create_silent_mp3,
    _is_tts_current,
    _mark_downstream_stale,
    _resolve_tts_config,
    _voice_config_signature,
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
    settings.elevenlabs_speed = 1.0
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


def test_clean_voice_history_reduces_retry_ceiling(tmp_path):
    manifest_path = tmp_path / "outputs" / "ep" / "tts" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "metadata": {
                            "speaker_parts": [
                                {
                                    "voice_id": "clean",
                                    "chunks": 12,
                                    "takes": 12,
                                    "noise_floor_db": -75.0,
                                    "noise_floor_limit_db": -55.0,
                                }
                            ]
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(outputs_dir=str(tmp_path / "outputs"), _env_file=None)

    assert _adaptive_retry_limit(settings, "clean", 3, enabled=True) == 2


def test_noisy_voice_keeps_configured_retry_ceiling(tmp_path):
    manifest_path = tmp_path / "outputs" / "ep" / "tts" / "manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "segments": [
                    {
                        "metadata": {
                            "speaker_parts": [
                                {
                                    "voice_id": "noisy",
                                    "chunks": 10,
                                    "takes": 14,
                                    "noise_floor_db": -45.0,
                                    "noise_floor_limit_db": -55.0,
                                }
                            ]
                        }
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    settings = Settings(outputs_dir=str(tmp_path / "outputs"), _env_file=None)

    assert _adaptive_retry_limit(settings, "noisy", 3, enabled=True) == 3


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
        json.dumps({"segments": [{"chapter_id": "ch01", "file_path": "tts/ch01.mp3"}]})
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
        json.dumps({"segments": [{"chapter_id": "ch01", "file_path": "tts/ch01.mp3"}]})
    )
    provenance.write_text(json.dumps({"input_content_hash": "hash"}))

    assert _is_tts_current(manifest, provenance, "hash") is True


def test_is_tts_current_rejects_incomplete_chapter_coverage(tmp_path):
    manifest = tmp_path / "tts" / "manifest.json"
    provenance = tmp_path / "provenance.json"
    manifest.parent.mkdir()
    audio = manifest.parent / "ch01.mp3"
    audio.write_bytes(b"audio")
    manifest.write_text(
        json.dumps({"segments": [{"chapter_id": "ch01", "file_path": "tts/ch01.mp3"}]})
    )
    provenance.write_text(json.dumps({"input_content_hash": "hash"}))

    assert not _is_tts_current(
        manifest,
        provenance,
        "hash",
        {"ch01", "ch02"},
    )


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
    assert manifest["quality_by_voice"]["voice_123"]["chunks"] == 2
    assert manifest["quality_by_voice"]["voice_123"]["takes"] == 2
    assert manifest["quality_by_voice"]["voice_123"]["retries"] == 0


@patch("btcedu.services.elevenlabs_service.ElevenLabsService")
def test_opening_and_closing_are_generated_as_normal_chapters(
    mock_service_cls, db_session, tmp_path
):
    episode = _setup_episode(db_session, tmp_path)
    settings = _make_settings(tmp_path)
    chapters_path = Path(settings.outputs_dir) / episode.episode_id / "chapters.json"
    chapters = json.loads(json.dumps(CHAPTERS_JSON))
    chapters["total_chapters"] = 3
    chapters["estimated_duration_seconds"] = 30
    chapters["chapters"] = [
        {
            "chapter_id": "ch01",
            "title": "Açılış",
            "order": 1,
            "story_type": "opening",
            "narration": {
                "text": "İyi akşamlar, haber bültenimize hoş geldiniz.",
                "word_count": 6,
                "estimated_duration_seconds": 10,
            },
            "visual": {"type": "title_card", "description": "Opening card"},
            "overlays": [],
            "transitions": {"in": "fade", "out": "cut"},
        },
        {
            "chapter_id": "ch02",
            "title": "Günün haberi",
            "order": 2,
            "story_type": "politik",
            "narration": {
                "text": "Günün önemli gelişmesi burada normal şekilde anlatılıyor.",
                "word_count": 7,
                "estimated_duration_seconds": 10,
            },
            "visual": {
                "type": "b_roll",
                "description": "News footage",
                "image_prompt": "Neutral editorial news footage",
            },
            "overlays": [],
            "transitions": {"in": "cut", "out": "cut"},
        },
        {
            "chapter_id": "ch03",
            "title": "Kapanış",
            "order": 3,
            "story_type": "closing",
            "narration": {
                "text": "Bültenimizin sonuna geldik, iyi akşamlar.",
                "word_count": 5,
                "estimated_duration_seconds": 10,
            },
            "visual": {"type": "title_card", "description": "Closing card"},
            "overlays": [],
            "transitions": {"in": "cut", "out": "fade"},
        },
    ]
    chapters_path.write_text(json.dumps(chapters, ensure_ascii=False), encoding="utf-8")

    from btcedu.services.elevenlabs_service import TTSResponse

    mock_service = mock_service_cls.return_value
    mock_service.synthesize.return_value = TTSResponse(
        audio_bytes=b"fresh_audio",
        duration_seconds=10.0,
        sample_rate=44100,
        model="eleven_multilingual_v2",
        voice_id="voice_123",
        character_count=50,
        cost_usd=0.03,
    )

    result = generate_tts(db_session, episode.episode_id, settings)

    assert mock_service.synthesize.call_count == 3
    spoken = [call.args[0].text for call in mock_service.synthesize.call_args_list]
    assert spoken == [
        "İyi akşamlar, haber bültenimize hoş geldiniz.",
        "Günün önemli gelişmesi burada normal şekilde anlatılıyor.",
        "Bültenimizin sonuna geldik, iyi akşamlar.",
    ]
    manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
    assert [segment["chapter_id"] for segment in manifest["segments"]] == [
        "ch01",
        "ch02",
        "ch03",
    ]
    assert all(
        (Path(settings.outputs_dir) / episode.episode_id / segment["file_path"]).exists()
        for segment in manifest["segments"]
    )


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
    """Single chapter generation requires a complete current base manifest."""
    episode = _setup_episode(db_session, tmp_path)
    settings = _make_settings(tmp_path)
    settings.dry_run = True

    with pytest.raises(ValueError, match="manifest entry for ch01 is missing"):
        generate_tts(db_session, episode.episode_id, settings, chapter_id="ch02")


@patch("btcedu.services.elevenlabs_service.ElevenLabsService")
def test_generate_tts_single_chapter_preserves_other_manifest_entries(
    mock_service_cls, db_session, tmp_path
):
    episode = _setup_episode(db_session, tmp_path)
    settings = _make_settings(tmp_path)
    settings.dry_run = True

    first = generate_tts(db_session, episode.episode_id, settings)
    original = json.loads(first.manifest_path.read_text())
    original_ch01 = next(s for s in original["segments"] if s["chapter_id"] == "ch01")

    result = generate_tts(
        db_session,
        episode.episode_id,
        settings,
        chapter_id="ch02",
        force=True,
    )

    manifest = json.loads(result.manifest_path.read_text())
    assert [s["chapter_id"] for s in manifest["segments"]] == ["ch01", "ch02"]
    assert manifest["segments"][0] == original_ch01


@patch("btcedu.services.elevenlabs_service.ElevenLabsService")
def test_generate_tts_single_chapter_rejects_stale_preserved_audio(
    mock_service_cls, db_session, tmp_path
):
    episode = _setup_episode(db_session, tmp_path)
    settings = _make_settings(tmp_path)
    settings.dry_run = True
    generate_tts(db_session, episode.episode_id, settings)

    chapters_path = Path(settings.outputs_dir) / episode.episode_id / "chapters.json"
    chapters = json.loads(chapters_path.read_text())
    chapters["chapters"][0]["narration"]["text"] = "Degismis birinci bolum."
    chapters_path.write_text(json.dumps(chapters), encoding="utf-8")

    with pytest.raises(ValueError, match="ch01 is stale"):
        generate_tts(
            db_session,
            episode.episode_id,
            settings,
            chapter_id="ch02",
            force=True,
        )


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
    from btcedu.services.errors import ErrorCategory, PipelineError

    with pytest.raises(PipelineError) as exc_info:
        generate_tts(db_session, episode.episode_id, settings)
    assert exc_info.value.category == ErrorCategory.PERMANENT_COST_LIMIT
    db_session.refresh(episode)
    assert episode.status == EpisodeStatus.COST_LIMIT


def test_tts_stage_budget_counts_each_successful_provider_call(db_session, tmp_path):
    episode = _setup_episode(db_session, tmp_path)
    settings = _make_settings(tmp_path)
    from btcedu.services.elevenlabs_service import TTSResponse
    from btcedu.services.errors import PipelineError

    resolved = _resolve_tts_config(episode, settings)
    resolved["max_cost_usd"] = 0.015

    class BillingService:
        def __init__(self, *, before_api_call, after_api_call, **_kwargs):
            self.before_api_call = before_api_call
            self.after_api_call = after_api_call

        def synthesize(self, request):
            self.before_api_call(0, len(request.text))
            self.after_api_call(len(request.text))
            return TTSResponse(
                audio_bytes=b"audio",
                duration_seconds=3.0,
                sample_rate=44100,
                model=request.model,
                voice_id=request.voice_id,
                character_count=len(request.text),
                cost_usd=0.30 * len(request.text) / 1000,
            )

    with (
        patch("btcedu.core.tts._resolve_tts_config", return_value=resolved),
        patch("btcedu.services.elevenlabs_service.ElevenLabsService", BillingService),
        pytest.raises(PipelineError, match="TTS credit budget exceeded"),
    ):
        generate_tts(db_session, episode.episode_id, settings)

    db_session.refresh(episode)
    assert episode.status == EpisodeStatus.COST_LIMIT


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


def test_tts_skipped_advances_status(db_session, tmp_path):
    """When TTS is current (skipped), episode status should still advance to TTS_DONE."""
    episode = Episode(
        episode_id="ep_skip_status",
        title="Status advance test",
        url="https://example.com/test",
        status=EpisodeStatus.IMAGES_GENERATED,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    # Create chapters.json, tts dir, manifest, provenance
    ep_dir = tmp_path / "ep_skip_status"
    ep_dir.mkdir()
    chapters_path = ep_dir / "chapters.json"
    chapters_path.write_text(json.dumps(CHAPTERS_JSON), encoding="utf-8")

    tts_dir = ep_dir / "tts"
    tts_dir.mkdir()
    manifest_path = tts_dir / "manifest.json"
    provenance_path = ep_dir / "provenance"
    provenance_path.mkdir()
    prov_file = provenance_path / "tts_provenance.json"

    settings = _make_settings(tmp_path)
    settings.outputs_dir = str(tmp_path)
    settings.dry_run = False

    # Compute the real content hash the stage will use (narration + voice config)
    doc = ChapterDocument(**CHAPTERS_JSON)
    _cfg = _resolve_tts_config(episode, settings)
    chapters_hash = _compute_tts_content_hash(
        doc, _cfg["pronunciation_lexicon"], _voice_config_signature(_cfg)
    )

    segments = []
    for chapter in doc.chapters:
        audio_path = tts_dir / f"{chapter.chapter_id}.mp3"
        audio_path.write_bytes(b"audio")
        segments.append(
            {
                "chapter_id": chapter.chapter_id,
                "file_path": f"tts/{chapter.chapter_id}.mp3",
            }
        )
    manifest_path.write_text(json.dumps({"segments": segments}), encoding="utf-8")
    prov_file.write_text(json.dumps({"input_content_hash": chapters_hash}), encoding="utf-8")

    result = generate_tts(db_session, "ep_skip_status", settings, force=False)
    assert result.skipped is True
    db_session.refresh(episode)
    assert episode.status == EpisodeStatus.TTS_DONE


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
