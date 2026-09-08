"""Tests for Sprint 9: Renderer implementation."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core.renderer import (
    OVERLAY_STYLES,
    RenderResult,
    _chapter_to_overlay_specs,
    _compute_render_content_hash,
    _is_render_current,
    _resolve_chapter_media,
    render_is_current,
    render_video,
    title_card_texts,
)
from btcedu.db import Base
from btcedu.models import (  # noqa: F401 - register tables render_video reads
    avatar_audio_asset,
    avatar_job,
    avatar_job_audit,
    avatar_provider_breaker,
    avatar_regeneration,
)
from btcedu.models.chapter_schema import ChapterDocument
from btcedu.models.content_artifact import ContentArtifact
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun
from btcedu.models.media_asset import MediaAsset, MediaAssetType


@pytest.fixture
def db_engine():
    """In-memory SQLite with media_assets table for render tests."""
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
    """Database session for render tests."""
    factory = sessionmaker(bind=db_engine)
    session = factory()
    yield session
    session.close()


@pytest.fixture
def settings(tmp_path):
    """Test settings with temp directories."""
    return Settings(
        outputs_dir=str(tmp_path / "outputs"),
        render_resolution="1920x1080",
        render_fps=30,
        render_crf=23,
        render_preset="medium",
        render_audio_bitrate="192k",
        render_font="NotoSans-Bold",
        render_timeout_segment=300,
        render_timeout_concat=600,
        dry_run=False,
    )


def _create_test_chapters_json(episode_id: str, output_dir: Path):
    """Create a minimal chapters.json for testing."""
    chapters_data = {
        "schema_version": "1.0",
        "episode_id": episode_id,
        "title": "Test Episode",
        "total_chapters": 2,
        "estimated_duration_seconds": 120,
        "chapters": [
            {
                "chapter_id": "ch01",
                "title": "Intro",
                "order": 1,
                "narration": {
                    "text": "This is chapter one.",
                    "word_count": 4,
                    "estimated_duration_seconds": 60,
                },
                "visual": {
                    "type": "title_card",
                    "description": "Title card",
                    "image_prompt": None,
                },
                "overlays": [
                    {
                        "type": "lower_third",
                        "text": "Episode Title",
                        "start_offset_seconds": 2.0,
                        "duration_seconds": 5.0,
                    }
                ],
                "transitions": {"in": "fade", "out": "cut"},
            },
            {
                "chapter_id": "ch02",
                "title": "Main",
                "order": 2,
                "narration": {
                    "text": "This is chapter two.",
                    "word_count": 4,
                    "estimated_duration_seconds": 60,
                },
                "visual": {
                    "type": "diagram",
                    "description": "Diagram",
                    "image_prompt": "A diagram",
                },
                "overlays": [],
                "transitions": {"in": "cut", "out": "fade"},
            },
        ],
    }
    chapters_path = output_dir / episode_id / "chapters.json"
    chapters_path.parent.mkdir(parents=True, exist_ok=True)
    chapters_path.write_text(json.dumps(chapters_data, indent=2))
    return chapters_path


def _create_test_image_manifest(episode_id: str, output_dir: Path):
    """Create a minimal image manifest for testing."""
    manifest_data = {
        "episode_id": episode_id,
        "schema_version": "1.0",
        "generated_at": "2025-01-01T00:00:00Z",
        "images": [
            {
                "chapter_id": "ch01",
                "chapter_title": "Intro",
                "visual_type": "title_card",
                "file_path": "images/ch01.png",
                "generation_method": "template",
            },
            {
                "chapter_id": "ch02",
                "chapter_title": "Main",
                "visual_type": "diagram",
                "file_path": "images/ch02.png",
                "generation_method": "dalle3",
            },
        ],
    }
    manifest_path = output_dir / episode_id / "images" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest_data, indent=2))

    # Create dummy image files
    for img in manifest_data["images"]:
        img_path = output_dir / episode_id / img["file_path"]
        img_path.parent.mkdir(parents=True, exist_ok=True)
        img_path.write_bytes(b"fake image")

    return manifest_path


def _create_test_tts_manifest(episode_id: str, output_dir: Path):
    """Create a minimal TTS manifest for testing."""
    manifest_data = {
        "episode_id": episode_id,
        "schema_version": "1.0",
        "generated_at": "2025-01-01T00:00:00Z",
        "total_duration_seconds": 120.0,
        "segments": [
            {
                "chapter_id": "ch01",
                "file_path": "tts/ch01.mp3",
                "duration_seconds": 60.0,
            },
            {
                "chapter_id": "ch02",
                "file_path": "tts/ch02.mp3",
                "duration_seconds": 60.0,
            },
        ],
    }
    manifest_path = output_dir / episode_id / "tts" / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest_data, indent=2))

    # Create dummy audio files
    for seg in manifest_data["segments"]:
        audio_path = output_dir / episode_id / seg["file_path"]
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"fake audio")

    return manifest_path


def test_compute_render_content_hash():
    """Test render content hash computation."""
    chapters_json = {
        "schema_version": "1.0",
        "episode_id": "ep001",
        "title": "Test",
        "total_chapters": 1,
        "estimated_duration_seconds": 60,
        "chapters": [
            {
                "chapter_id": "ch01",
                "title": "Test",
                "order": 1,
                "narration": {"text": "Test", "word_count": 1, "estimated_duration_seconds": 60},
                "visual": {"type": "title_card", "description": "Test"},
                "overlays": [
                    {
                        "type": "lower_third",
                        "text": "Test",
                        "start_offset_seconds": 2.0,
                        "duration_seconds": 5.0,
                    }
                ],
                "transitions": {"in": "fade", "out": "fade"},
            }
        ],
    }
    chapters_doc = ChapterDocument(**chapters_json)

    image_manifest = {
        "images": [
            {
                "chapter_id": "ch01",
                "file_path": "images/ch01.png",
                "generation_method": "template",
            }
        ]
    }

    tts_manifest = {
        "segments": [{"chapter_id": "ch01", "file_path": "tts/ch01.mp3", "duration_seconds": 60.0}]
    }

    hash1 = _compute_render_content_hash(chapters_doc, image_manifest, tts_manifest)
    assert isinstance(hash1, str)
    assert len(hash1) == 64  # SHA-256 hex digest

    # Same inputs should produce same hash
    hash2 = _compute_render_content_hash(chapters_doc, image_manifest, tts_manifest)
    assert hash1 == hash2

    # Different overlay should change hash
    chapters_json["chapters"][0]["overlays"][0]["text"] = "Changed"
    chapters_doc_changed = ChapterDocument(**chapters_json)
    hash3 = _compute_render_content_hash(chapters_doc_changed, image_manifest, tts_manifest)
    assert hash3 != hash1

    chapters_json["chapters"][0]["overlays"][0]["text"] = "Test"
    chapters_json["chapters"][0]["title"] = "Changed title"
    chapters_doc_title_changed = ChapterDocument(**chapters_json)
    assert (
        _compute_render_content_hash(chapters_doc_title_changed, image_manifest, tts_manifest)
        != hash1
    )

    assert (
        _compute_render_content_hash(
            chapters_doc,
            image_manifest,
            tts_manifest,
            {"intro_show_name": "Different show"},
        )
        != hash1
    )


def test_is_render_current_no_files(tmp_path):
    """Test idempotency check when files don't exist."""
    manifest_path = tmp_path / "render_manifest.json"
    provenance_path = tmp_path / "provenance.json"
    draft_path = tmp_path / "draft.mp4"

    assert not _is_render_current(manifest_path, provenance_path, draft_path, "hash123")


def test_is_render_current_with_stale_marker(tmp_path):
    """Test idempotency check with .stale marker."""
    manifest_path = tmp_path / "render_manifest.json"
    provenance_path = tmp_path / "provenance.json"
    draft_path = tmp_path / "draft.mp4"

    manifest_path.write_text("{}")
    provenance_path.write_text('{"input_content_hash": "hash123"}')
    draft_path.write_bytes(b"fake video")

    stale_marker = draft_path.with_suffix(".mp4.stale")
    stale_marker.write_text("{}")

    assert not _is_render_current(manifest_path, provenance_path, draft_path, "hash123")


def test_is_render_current_hash_mismatch(tmp_path):
    """Test idempotency check with hash mismatch."""
    manifest_path = tmp_path / "render_manifest.json"
    provenance_path = tmp_path / "provenance.json"
    draft_path = tmp_path / "draft.mp4"

    manifest_path.write_text("{}")
    provenance_path.write_text('{"input_content_hash": "oldhash"}')
    draft_path.write_bytes(b"fake video")

    assert not _is_render_current(manifest_path, provenance_path, draft_path, "newhash")


def test_is_render_current_all_good(tmp_path):
    """Test idempotency check when all is current."""
    manifest_path = tmp_path / "render_manifest.json"
    provenance_path = tmp_path / "provenance.json"
    draft_path = tmp_path / "draft.mp4"

    manifest_path.write_text("{}")
    provenance_path.write_text('{"input_content_hash": "hash123"}')
    draft_path.write_bytes(b"fake video")

    assert _is_render_current(manifest_path, provenance_path, draft_path, "hash123")


def test_chapter_to_overlay_specs():
    """Test conversion of chapter overlays to OverlaySpec."""
    from btcedu.models.chapter_schema import Chapter, Narration, Overlay, Transitions, Visual

    chapter = Chapter(
        chapter_id="ch01",
        title="Test",
        order=1,
        narration=Narration(text="Test", word_count=1, estimated_duration_seconds=60),
        visual=Visual(type="title_card", description="Test"),
        overlays=[
            Overlay(
                type="lower_third",
                text="Lower Third Text",
                start_offset_seconds=2.0,
                duration_seconds=5.0,
            ),
            Overlay(
                type="title",
                text="Title Text",
                start_offset_seconds=10.0,
                duration_seconds=3.0,
            ),
        ],
        transitions=Transitions(**{"in": "fade", "out": "fade"}),
    )

    specs = _chapter_to_overlay_specs(chapter, "TestFont")

    assert len(specs) == 2

    # Check first overlay (lower_third)
    assert specs[0].text == "Lower Third Text"
    assert specs[0].overlay_type == "lower_third"
    assert specs[0].start == 2.0
    assert specs[0].end == 7.0  # 2.0 + 5.0
    assert specs[0].fontsize == OVERLAY_STYLES["lower_third"]["fontsize"]
    assert specs[0].position == "bottom_center"

    # Check second overlay (title)
    assert specs[1].text == "Title Text"
    assert specs[1].overlay_type == "title"
    assert specs[1].start == 10.0
    assert specs[1].end == 13.0  # 10.0 + 3.0
    assert specs[1].fontsize == OVERLAY_STYLES["title"]["fontsize"]
    assert specs[1].position == "center"


def test_resolve_chapter_media(tmp_path):
    """Test resolving image, audio paths and duration for a chapter."""
    base_dir = tmp_path / "outputs" / "ep001"
    base_dir.mkdir(parents=True)

    # Create test files
    image_path = base_dir / "images" / "ch01.png"
    audio_path = base_dir / "tts" / "ch01.mp3"
    image_path.parent.mkdir(parents=True)
    audio_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"image")
    audio_path.write_bytes(b"audio")

    image_manifest = {
        "images": [
            {
                "chapter_id": "ch01",
                "file_path": "images/ch01.png",
            }
        ]
    }

    tts_manifest = {
        "segments": [
            {
                "chapter_id": "ch01",
                "file_path": "tts/ch01.mp3",
                "duration_seconds": 60.5,
            }
        ]
    }

    img, aud, dur, asset_type = _resolve_chapter_media(
        "ch01", image_manifest, tts_manifest, base_dir
    )

    assert img == image_path
    assert aud == audio_path
    assert dur == 60.5
    assert asset_type == "photo"  # Phase 4: backward compat default


def test_resolve_chapter_media_missing_image(tmp_path):
    """Test resolving media when image is missing from manifest."""
    base_dir = tmp_path / "outputs" / "ep001"

    image_manifest = {"images": []}
    tts_manifest = {
        "segments": [{"chapter_id": "ch01", "file_path": "tts/ch01.mp3", "duration_seconds": 60.0}]
    }

    with pytest.raises(ValueError, match="No image found"):
        _resolve_chapter_media("ch01", image_manifest, tts_manifest, base_dir)


def test_render_video_missing_episode(db_session, settings):
    """Test render with non-existent episode."""
    with pytest.raises(ValueError, match="Episode not found"):
        render_video(db_session, "nonexistent", settings)


def test_render_video_wrong_status(db_session, settings):
    """Test render rejects wrong episode status."""
    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.CHAPTERIZED,  # Not TTS_DONE
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    with pytest.raises(ValueError, match="expected 'tts_done'"):
        render_video(db_session, "ep001", settings)


def test_render_video_missing_inputs(db_session, settings, tmp_path):
    """Test render with missing input files."""
    settings.outputs_dir = str(tmp_path / "outputs")

    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TTS_DONE,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    with pytest.raises(FileNotFoundError, match="Chapters file not found"):
        render_video(db_session, "ep001", settings)


def test_render_video_dry_run(db_session, settings, tmp_path):
    """Test render in dry-run mode."""
    settings.outputs_dir = str(tmp_path / "outputs")
    settings.dry_run = True

    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TTS_DONE,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    # Create test input files
    _create_test_chapters_json("ep001", Path(settings.outputs_dir))
    _create_test_image_manifest("ep001", Path(settings.outputs_dir))
    _create_test_tts_manifest("ep001", Path(settings.outputs_dir))

    # Run render in dry-run mode
    result = render_video(db_session, "ep001", settings)

    assert isinstance(result, RenderResult)
    assert result.episode_id == "ep001"
    assert result.segment_count == 2
    assert not result.skipped
    assert result.draft_path.exists()
    assert result.manifest_path.exists()
    assert result.provenance_path.exists()

    # Check episode status updated
    db_session.refresh(episode)
    assert episode.status == EpisodeStatus.RENDERED


def test_render_video_clears_stale_marker(db_session, settings, tmp_path):
    """A successful render must remove any leftover .stale marker."""
    settings.outputs_dir = str(tmp_path / "outputs")
    settings.dry_run = True

    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TTS_DONE,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    _create_test_chapters_json("ep001", Path(settings.outputs_dir))
    _create_test_image_manifest("ep001", Path(settings.outputs_dir))
    _create_test_tts_manifest("ep001", Path(settings.outputs_dir))

    # Simulate cascade invalidation having written a stale marker.
    render_dir = Path(settings.outputs_dir) / "ep001" / "render"
    render_dir.mkdir(parents=True, exist_ok=True)
    stale_marker = render_dir / "draft.mp4.stale"
    stale_marker.write_text("stale")

    result = render_video(db_session, "ep001", settings)

    assert not result.skipped
    assert not stale_marker.exists()


def test_render_video_idempotent(db_session, settings, tmp_path):
    """Test render idempotency (skip if current)."""
    settings.outputs_dir = str(tmp_path / "outputs")
    settings.dry_run = True

    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TTS_DONE,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    # Create test input files
    _create_test_chapters_json("ep001", Path(settings.outputs_dir))
    _create_test_image_manifest("ep001", Path(settings.outputs_dir))
    _create_test_tts_manifest("ep001", Path(settings.outputs_dir))

    # First run
    result1 = render_video(db_session, "ep001", settings)
    assert not result1.skipped

    # Second run (should skip)
    result2 = render_video(db_session, "ep001", settings)
    assert result2.skipped
    assert result2.segment_count == 2  # From provenance


def test_render_video_force_rerender(db_session, settings, tmp_path):
    """Test forced re-render."""
    settings.outputs_dir = str(tmp_path / "outputs")
    settings.dry_run = True

    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.RENDERED,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    # Create test input files
    _create_test_chapters_json("ep001", Path(settings.outputs_dir))
    _create_test_image_manifest("ep001", Path(settings.outputs_dir))
    _create_test_tts_manifest("ep001", Path(settings.outputs_dir))

    # First run
    result1 = render_video(db_session, "ep001", settings)
    assert not result1.skipped

    # Force re-render
    result2 = render_video(db_session, "ep001", settings, force=True)
    assert not result2.skipped


def _mock_segment_result(output_path, **kwargs):
    """Create a mock SegmentResult."""
    from btcedu.services.ffmpeg_service import SegmentResult

    # Create a non-empty file to simulate ffmpeg output
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_bytes(b"\x00" * 1024)
    return SegmentResult(
        segment_path=output_path,
        duration_seconds=kwargs.get("duration", 60.0),
        size_bytes=1024,
        ffmpeg_command=["ffmpeg", "-i", "input"],
        returncode=0,
        stderr="",
    )


def _mock_concat_result(output_path, **kwargs):
    """Create a mock ConcatResult."""
    from btcedu.services.ffmpeg_service import ConcatResult

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_bytes(b"\x00" * 2048)
    return ConcatResult(
        output_path=output_path,
        duration_seconds=120.0,
        size_bytes=2048,
        segment_count=2,
        ffmpeg_command=["ffmpeg", "-f", "concat"],
        returncode=0,
        stderr="",
    )


def test_render_video_non_dry_run(db_session, settings, tmp_path):
    """Test non-dry-run render with mocked ffmpeg service.

    Verifies the full render path: segment creation, concatenation,
    PipelineRun, ContentArtifact, and MediaAsset records.
    """
    settings.outputs_dir = str(tmp_path / "outputs")
    settings.dry_run = False

    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TTS_DONE,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    _create_test_chapters_json("ep001", Path(settings.outputs_dir))
    _create_test_image_manifest("ep001", Path(settings.outputs_dir))
    _create_test_tts_manifest("ep001", Path(settings.outputs_dir))

    def mock_create_segment(image_path, audio_path, output_path, duration, **kw):
        return _mock_segment_result(output_path, duration=duration)

    def mock_concatenate_segments(segment_paths, output_path, **kw):
        return _mock_concat_result(output_path, segment_count=len(segment_paths))

    with (
        patch(
            "btcedu.services.ffmpeg_service.create_segment",
            side_effect=mock_create_segment,
        ),
        patch(
            "btcedu.services.ffmpeg_service.concatenate_segments",
            side_effect=mock_concatenate_segments,
        ),
        patch(
            "btcedu.services.ffmpeg_service.get_ffmpeg_version",
            return_value="ffmpeg version 6.0-mock",
        ),
    ):
        result = render_video(db_session, "ep001", settings)

    assert isinstance(result, RenderResult)
    assert result.episode_id == "ep001"
    assert result.segment_count == 2
    assert not result.skipped
    assert result.total_size_bytes > 0

    # Episode status updated
    db_session.refresh(episode)
    assert episode.status == EpisodeStatus.RENDERED

    # PipelineRun record exists with success
    run = db_session.query(PipelineRun).filter_by(episode_id="ep001", stage="render").first()
    assert run is not None
    assert run.status == "success"
    assert run.completed_at is not None

    # ContentArtifact record exists
    artifact = (
        db_session.query(ContentArtifact)
        .filter_by(episode_id="ep001", artifact_type="render")
        .first()
    )
    assert artifact is not None
    assert artifact.model == "ffmpeg"

    # MediaAsset record exists (non-dry-run)
    asset = (
        db_session.query(MediaAsset)
        .filter_by(episode_id="ep001", asset_type=MediaAssetType.VIDEO)
        .first()
    )
    assert asset is not None
    assert asset.size_bytes > 0


def test_render_weather_video_uses_actual_tts_duration(db_session, settings, tmp_path):
    """Timed weather scenes are planned from the TTS manifest, not chapter estimates."""
    from btcedu.core.weather.models import WeatherRenderResult

    settings.outputs_dir = str(tmp_path / "outputs")
    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TTS_DONE,
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    db_session.add(episode)
    db_session.commit()

    chapters_path = _create_test_chapters_json("ep001", Path(settings.outputs_dir))
    chapters_data = json.loads(chapters_path.read_text())
    chapters_data["chapters"][0]["narration"]["estimated_duration_seconds"] = 17.0
    chapters_data["estimated_duration_seconds"] = 77.0
    chapters_path.write_text(json.dumps(chapters_data))

    image_manifest_path = _create_test_image_manifest("ep001", Path(settings.outputs_dir))
    image_manifest = json.loads(image_manifest_path.read_text())
    image_manifest["images"][0]["metadata"] = {
        "category": "weather",
        "weather_data_path": "images/ch01_weather.json",
    }
    image_manifest_path.write_text(json.dumps(image_manifest))

    tts_manifest_path = _create_test_tts_manifest("ep001", Path(settings.outputs_dir))
    tts_manifest = json.loads(tts_manifest_path.read_text())
    tts_manifest["segments"][0]["duration_seconds"] = 42.5
    tts_manifest_path.write_text(json.dumps(tts_manifest))

    base = Path(settings.outputs_dir) / "ep001"
    fixture = Path(__file__).parent / "fixtures" / "weather_fixture_data.json"
    (base / "images" / "ch01_weather.json").write_text(fixture.read_text())
    captured = {}

    def mock_weather_video(weather_data, scene_plan, output_path, **kwargs):
        captured["duration"] = scene_plan.duration_seconds
        Path(output_path).write_bytes(b"weather video")
        return WeatherRenderResult(success=True, output_path=str(output_path))

    def mock_create_segment(image_path, audio_path, output_path, duration, **kwargs):
        return _mock_segment_result(output_path, duration=duration)

    def mock_create_video_segment(video_path, audio_path, output_path, duration, **kwargs):
        from btcedu.core.render_guard import inspect

        inspect(["ffmpeg", "-i", str(video_path), str(output_path)])
        captured["video_path"] = video_path
        return _mock_segment_result(output_path, duration=duration)

    with (
        patch(
            "btcedu.core.weather.renderer.render_weather_scene_video",
            side_effect=mock_weather_video,
        ),
        patch(
            "btcedu.services.ffmpeg_service.create_segment",
            side_effect=mock_create_segment,
        ),
        patch(
            "btcedu.services.ffmpeg_service.create_video_segment",
            side_effect=mock_create_video_segment,
        ),
        patch(
            "btcedu.services.ffmpeg_service.concatenate_segments",
            side_effect=lambda segment_paths, output_path, **kwargs: _mock_concat_result(
                output_path, segment_count=len(segment_paths)
            ),
        ),
        patch(
            "btcedu.services.ffmpeg_service.get_ffmpeg_version",
            return_value="ffmpeg version 6.0-mock",
        ),
    ):
        render_video(db_session, "ep001", settings)

    assert captured["duration"] == 42.5
    assert captured["video_path"].endswith("images/ch01_weather.mp4")
    scene_plan = json.loads((base / "images" / "ch01_weather_scenes.json").read_text())
    assert scene_plan["duration_seconds"] == 42.5
    assert render_is_current(db_session, "ep001", settings) == (True, "render is current")


def test_render_weather_video_failure_uses_static_card(db_session, settings, tmp_path):
    """A failed timed weather render falls back to the persisted weather PNG."""
    from btcedu.core.weather.models import WeatherRenderResult

    settings.outputs_dir = str(tmp_path / "outputs")
    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TTS_DONE,
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    db_session.add(episode)
    db_session.commit()

    _create_test_chapters_json("ep001", Path(settings.outputs_dir))
    image_manifest_path = _create_test_image_manifest("ep001", Path(settings.outputs_dir))
    image_manifest = json.loads(image_manifest_path.read_text())
    image_manifest["images"][0]["metadata"] = {
        "category": "weather",
        "weather_data_path": "images/ch01_weather.json",
    }
    image_manifest_path.write_text(json.dumps(image_manifest))
    _create_test_tts_manifest("ep001", Path(settings.outputs_dir))

    base = Path(settings.outputs_dir) / "ep001"
    fixture = Path(__file__).parent / "fixtures" / "weather_fixture_data.json"
    (base / "images" / "ch01_weather.json").write_text(fixture.read_text())
    rendered_images = []

    def mock_create_segment(image_path, audio_path, output_path, duration, **kwargs):
        rendered_images.append(image_path)
        return _mock_segment_result(output_path, duration=duration)

    with (
        patch(
            "btcedu.core.weather.renderer.render_weather_scene_video",
            return_value=WeatherRenderResult(success=False),
        ),
        patch(
            "btcedu.services.ffmpeg_service.create_segment",
            side_effect=mock_create_segment,
        ),
        patch(
            "btcedu.services.ffmpeg_service.create_video_segment",
        ) as create_video_segment,
        patch(
            "btcedu.services.ffmpeg_service.concatenate_segments",
            side_effect=lambda segment_paths, output_path, **kwargs: _mock_concat_result(
                output_path, segment_count=len(segment_paths)
            ),
        ),
        patch(
            "btcedu.services.ffmpeg_service.get_ffmpeg_version",
            return_value="ffmpeg version 6.0-mock",
        ),
    ):
        render_video(db_session, "ep001", settings)

    assert str(base / "images" / "ch01.png") in rendered_images
    create_video_segment.assert_not_called()


def test_render_rerenders_stale_segments(db_session, settings, tmp_path):
    """Regression: a pre-existing segment must be re-rendered when its image/audio
    inputs are newer than the segment file. Previously the idempotency guard skipped
    any segment that merely existed, so regenerated images/TTS were silently ignored
    and the final video kept the old content.
    """
    import os

    settings.outputs_dir = str(tmp_path / "outputs")
    settings.dry_run = False

    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TTS_DONE,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    base = Path(settings.outputs_dir) / "ep001"
    _create_test_chapters_json("ep001", Path(settings.outputs_dir))
    _create_test_image_manifest("ep001", Path(settings.outputs_dir))
    _create_test_tts_manifest("ep001", Path(settings.outputs_dir))

    # Pre-create STALE segment files (non-empty) with an old mtime.
    seg_dir = base / "render" / "segments"
    seg_dir.mkdir(parents=True, exist_ok=True)
    old = 1_000_000.0
    for cid in ("ch01", "ch02"):
        p = seg_dir / f"{cid}.mp4"
        p.write_bytes(b"\x00" * 1024)
        os.utime(p, (old, old))

    # Make image + audio inputs NEWER than the stale segments.
    new = old + 10_000.0
    for rel in ("images/ch01.png", "images/ch02.png", "tts/ch01.mp3", "tts/ch02.mp3"):
        f = base / rel
        os.utime(f, (new, new))

    rendered = []

    def mock_create_segment(image_path, audio_path, output_path, duration, **kw):
        rendered.append(Path(output_path).stem)
        return _mock_segment_result(output_path, duration=duration)

    def mock_concatenate_segments(segment_paths, output_path, **kw):
        return _mock_concat_result(output_path, segment_count=len(segment_paths))

    with (
        patch(
            "btcedu.services.ffmpeg_service.create_segment",
            side_effect=mock_create_segment,
        ),
        patch(
            "btcedu.services.ffmpeg_service.concatenate_segments",
            side_effect=mock_concatenate_segments,
        ),
        patch(
            "btcedu.services.ffmpeg_service.probe_media",
            return_value=None,
        ),
        patch(
            "btcedu.services.ffmpeg_service.get_ffmpeg_version",
            return_value="ffmpeg version 6.0-mock",
        ),
    ):
        render_video(db_session, "ep001", settings, force=True)

    # Both stale segments must have been re-rendered.
    assert "ch01" in rendered
    assert "ch02" in rendered


def test_render_skips_fresh_segments(db_session, settings, tmp_path):
    """Companion: a segment NEWER than its inputs is reused (not re-rendered)."""
    import os

    settings.outputs_dir = str(tmp_path / "outputs")
    settings.dry_run = False

    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TTS_DONE,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    base = Path(settings.outputs_dir) / "ep001"
    _create_test_chapters_json("ep001", Path(settings.outputs_dir))
    _create_test_image_manifest("ep001", Path(settings.outputs_dir))
    _create_test_tts_manifest("ep001", Path(settings.outputs_dir))

    rendered = []

    def mock_create_segment(image_path, audio_path, output_path, duration, **kw):
        rendered.append(Path(output_path).stem)
        return _mock_segment_result(output_path, duration=duration)

    def mock_concatenate_segments(segment_paths, output_path, **kw):
        return _mock_concat_result(output_path, segment_count=len(segment_paths))

    with (
        patch(
            "btcedu.services.ffmpeg_service.create_segment",
            side_effect=mock_create_segment,
        ),
        patch(
            "btcedu.services.ffmpeg_service.concatenate_segments",
            side_effect=mock_concatenate_segments,
        ),
        patch(
            "btcedu.services.ffmpeg_service.probe_media",
            return_value=None,
        ),
        patch(
            "btcedu.services.ffmpeg_service.get_ffmpeg_version",
            return_value="ffmpeg version 6.0-mock",
        ),
    ):
        render_video(db_session, "ep001", settings, force=True)

        # The first render produced the segments; now age the inputs so the
        # segments are unambiguously fresh and render again.
        rendered.clear()
        old = 1_000_000.0
        for rel in ("images/ch01.png", "images/ch02.png", "tts/ch01.mp3", "tts/ch02.mp3"):
            os.utime(base / rel, (old, old))

        render_video(db_session, "ep001", settings, force=True)

    # Fresh segments reused → create_segment not called.
    assert rendered == []


def test_render_rebuilds_segments_when_render_settings_change(db_session, settings, tmp_path):
    """A changed render setting leaves the inputs untouched — segments must still go.

    Switching Ken Burns off used to leave every cached segment in place, because
    the freshness check only compared mtimes, so the change never reached the
    finished video.
    """
    import os

    settings.outputs_dir = str(tmp_path / "outputs")
    settings.dry_run = False
    settings.render_crf = 23

    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TTS_DONE,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    base = Path(settings.outputs_dir) / "ep001"
    _create_test_chapters_json("ep001", Path(settings.outputs_dir))
    _create_test_image_manifest("ep001", Path(settings.outputs_dir))
    _create_test_tts_manifest("ep001", Path(settings.outputs_dir))

    rendered = []

    def mock_create_segment(image_path, audio_path, output_path, duration, **kw):
        rendered.append(Path(output_path).stem)
        return _mock_segment_result(output_path, duration=duration)

    def mock_concatenate_segments(segment_paths, output_path, **kw):
        return _mock_concat_result(output_path, segment_count=len(segment_paths))

    with (
        patch(
            "btcedu.services.ffmpeg_service.create_segment",
            side_effect=mock_create_segment,
        ),
        patch(
            "btcedu.services.ffmpeg_service.concatenate_segments",
            side_effect=mock_concatenate_segments,
        ),
        patch(
            "btcedu.services.ffmpeg_service.probe_media",
            return_value=None,
        ),
        patch(
            "btcedu.services.ffmpeg_service.get_ffmpeg_version",
            return_value="ffmpeg version 6.0-mock",
        ),
    ):
        render_video(db_session, "ep001", settings, force=True)

        rendered.clear()
        old = 1_000_000.0
        for rel in ("images/ch01.png", "images/ch02.png", "tts/ch01.mp3", "tts/ch02.mp3"):
            os.utime(base / rel, (old, old))

        settings.render_crf = 20
        render_video(db_session, "ep001", settings, force=True)

    assert rendered == ["ch01", "ch02"]


def test_render_video_error_rollback(db_session, settings, tmp_path):
    """Test that render failure sets PipelineRun to failed and records error.

    When any chapter has missing media, render fails closed and
    the PipelineRun and episode error_message are updated accordingly.
    """
    settings.outputs_dir = str(tmp_path / "outputs")
    settings.dry_run = False

    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TTS_DONE,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    # Create chapters.json referencing 2 chapters but with manifests
    # that point to non-existent image/audio files (no dummy files created)
    chapters_data = {
        "schema_version": "1.0",
        "episode_id": "ep001",
        "title": "Test Episode",
        "total_chapters": 2,
        "estimated_duration_seconds": 120,
        "chapters": [
            {
                "chapter_id": "ch01",
                "title": "Intro",
                "order": 1,
                "narration": {
                    "text": "Chapter one.",
                    "word_count": 2,
                    "estimated_duration_seconds": 60,
                },
                "visual": {
                    "type": "title_card",
                    "description": "Title",
                    "image_prompt": None,
                },
                "overlays": [],
                "transitions": {"in": "cut", "out": "cut"},
            },
            {
                "chapter_id": "ch02",
                "title": "Main",
                "order": 2,
                "narration": {
                    "text": "Chapter two.",
                    "word_count": 2,
                    "estimated_duration_seconds": 60,
                },
                "visual": {
                    "type": "title_card",
                    "description": "Title card",
                    "image_prompt": None,
                },
                "overlays": [],
                "transitions": {"in": "cut", "out": "cut"},
            },
        ],
    }
    ep_dir = Path(settings.outputs_dir) / "ep001"
    chapters_path = ep_dir / "chapters.json"
    chapters_path.parent.mkdir(parents=True, exist_ok=True)
    chapters_path.write_text(json.dumps(chapters_data))

    # Image manifest references files that don't exist
    image_manifest = {
        "episode_id": "ep001",
        "schema_version": "1.0",
        "images": [
            {"chapter_id": "ch01", "file_path": "images/ch01.png", "generation_method": "template"},
            {"chapter_id": "ch02", "file_path": "images/ch02.png", "generation_method": "template"},
        ],
    }
    img_manifest_path = ep_dir / "images" / "manifest.json"
    img_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    img_manifest_path.write_text(json.dumps(image_manifest))
    # NOTE: no actual image files created — _resolve_chapter_media will raise ValueError

    # TTS manifest
    tts_manifest = {
        "episode_id": "ep001",
        "schema_version": "1.0",
        "segments": [
            {"chapter_id": "ch01", "file_path": "tts/ch01.mp3", "duration_seconds": 60.0},
            {"chapter_id": "ch02", "file_path": "tts/ch02.mp3", "duration_seconds": 60.0},
        ],
    }
    tts_manifest_path = ep_dir / "tts" / "manifest.json"
    tts_manifest_path.parent.mkdir(parents=True, exist_ok=True)
    tts_manifest_path.write_text(json.dumps(tts_manifest))
    # NOTE: no actual audio files created

    with (
        patch(
            "btcedu.services.ffmpeg_service.get_ffmpeg_version",
            return_value="ffmpeg version 6.0-mock",
        ),
        pytest.raises(ValueError, match="Cannot render complete episode"),
    ):
        render_video(db_session, "ep001", settings)

    # PipelineRun should be marked as failed
    run = db_session.query(PipelineRun).filter_by(episode_id="ep001", stage="render").first()
    assert run is not None
    assert run.status == "failed"
    assert "Cannot render complete episode" in run.error_message

    # Episode error_message should be set
    db_session.refresh(episode)
    assert episode.error_message is not None
    assert "Cannot render complete episode" in episode.error_message


def test_title_card_texts_prefers_the_profile(settings, db_session):
    """The dashboard must read the same card texts the render puts on screen."""
    episode = Episode(
        episode_id="ep001",
        source="youtube_rss",
        title="Stored title",
        url="https://example.com/ep001",
        status=EpisodeStatus.CHAPTERIZED,
        content_profile="tagesschau_tr",
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    texts = title_card_texts(episode, settings)

    assert texts["show_name"] == "ALMANYA24"
    assert texts["episode_title"] == "Almanya Gündemi"
    assert texts["slogan"] == "Almanya'nın nabzı burada atıyor."
    assert texts["topic_intro_enabled"] is True
    assert texts["topic_intro_label"] == "GÜNDEM"
    assert texts["outro_text"].startswith("ALMANYA24")


def test_title_card_texts_falls_back_to_the_episode_title(settings, db_session):
    """A profile that names no episode title shows the episode's own title."""
    episode = Episode(
        episode_id="ep002",
        source="youtube_rss",
        title="Stored title",
        url="https://example.com/ep002",
        status=EpisodeStatus.CHAPTERIZED,
        content_profile="bitcoin_podcast",
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    texts = title_card_texts(episode, settings)

    assert texts["episode_title"] == "Stored title"


def test_topic_cards_skip_the_opening_and_closing(db_session, settings, tmp_path):
    """The greeting and the goodbye are not topics and get no 'GÜNDEM x/y' card."""
    settings.outputs_dir = str(tmp_path / "outputs")
    settings.dry_run = False

    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TTS_DONE,
        content_profile="tagesschau_tr",
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    chapters_path = _create_test_chapters_json("ep001", Path(settings.outputs_dir))
    doc = json.loads(chapters_path.read_text())
    doc["chapters"][0]["story_type"] = "opening"
    doc["chapters"][1]["story_type"] = "politik"
    chapters_path.write_text(json.dumps(doc))
    _create_test_image_manifest("ep001", Path(settings.outputs_dir))
    _create_test_tts_manifest("ep001", Path(settings.outputs_dir))

    topic_titles: list[str] = []

    def mock_topic_intro(output_path, topic_title, topic_index, total_topics, **kw):
        topic_titles.append(f"{topic_index}/{total_topics} {topic_title}")
        Path(output_path).write_bytes(b"x")
        return _mock_segment_result(output_path, duration=2.4)

    with (
        patch(
            "btcedu.services.ffmpeg_service.create_segment",
            side_effect=lambda image_path, audio_path, output_path, duration, **kw: (
                _mock_segment_result(output_path, duration=duration)
            ),
        ),
        patch(
            "btcedu.services.ffmpeg_service.concatenate_segments",
            side_effect=lambda segment_paths, output_path, **kw: _mock_concat_result(
                output_path, segment_count=len(segment_paths)
            ),
        ),
        patch(
            "btcedu.services.ffmpeg_service.get_ffmpeg_version",
            return_value="ffmpeg version 6.0-mock",
        ),
        patch(
            "btcedu.services.ffmpeg_service.create_topic_intro_segment",
            side_effect=mock_topic_intro,
        ),
        patch("btcedu.services.ffmpeg_service.create_intro_segment"),
        patch("btcedu.services.ffmpeg_service.create_outro_segment"),
    ):
        render_video(db_session, "ep001", settings)

    assert topic_titles == ["1/1 Main"]


def test_manifest_records_the_concat_timeline(db_session, settings, tmp_path):
    """The manifest must carry where each part really starts in the video.

    Publish-time chapter marks read these offsets; summing chapter audio alone
    would ignore the intro and the topic cards and drift ever further.
    """
    settings.outputs_dir = str(tmp_path / "outputs")
    settings.dry_run = False

    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TTS_DONE,
        content_profile="tagesschau_tr",
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    chapters_path = _create_test_chapters_json("ep001", Path(settings.outputs_dir))
    doc = json.loads(chapters_path.read_text())
    doc["chapters"][0]["story_type"] = "opening"
    doc["chapters"][1]["story_type"] = "politik"
    chapters_path.write_text(json.dumps(doc))
    _create_test_image_manifest("ep001", Path(settings.outputs_dir))
    _create_test_tts_manifest("ep001", Path(settings.outputs_dir))

    with (
        patch(
            "btcedu.services.ffmpeg_service.create_segment",
            side_effect=lambda image_path, audio_path, output_path, duration, **kw: (
                _mock_segment_result(output_path, duration=duration)
            ),
        ),
        patch(
            "btcedu.services.ffmpeg_service.concatenate_segments",
            side_effect=lambda segment_paths, output_path, **kw: _mock_concat_result(
                output_path, segment_count=len(segment_paths)
            ),
        ),
        patch(
            "btcedu.services.ffmpeg_service.get_ffmpeg_version",
            return_value="ffmpeg version 6.0-mock",
        ),
        patch(
            "btcedu.services.ffmpeg_service.create_topic_intro_segment",
            side_effect=lambda output_path, **kw: (
                Path(output_path).write_bytes(b"x"),
                _mock_segment_result(output_path, duration=2.4),
            )[1],
        ),
        patch("btcedu.services.ffmpeg_service.create_intro_segment"),
        patch("btcedu.services.ffmpeg_service.create_outro_segment"),
    ):
        result = render_video(db_session, "ep001", settings)

    manifest = json.loads(
        (Path(settings.outputs_dir) / "ep001" / "render" / "render_manifest.json").read_text()
    )
    timeline = manifest["timeline"]

    # Every entry starts exactly where its predecessor ended.
    cursor = 0.0
    for entry in timeline:
        assert entry["start_seconds"] == pytest.approx(cursor, abs=0.01)
        cursor += entry["duration_seconds"]
    assert cursor == pytest.approx(manifest["total_duration_seconds"], abs=0.01)
    assert cursor == pytest.approx(result.total_duration_seconds, abs=0.01)

    kinds = [e["kind"] for e in timeline]
    assert kinds[0] == "intro"
    # The card announcing a topic sits immediately before that topic.
    card_index = kinds.index("topic_intro")
    assert timeline[card_index]["chapter_id"] == timeline[card_index + 1]["chapter_id"]
    assert timeline[card_index + 1]["kind"] == "chapter"

    # The opening chapter is pushed back by the intro, never left at zero.
    first_chapter = next(e for e in timeline if e["kind"] == "chapter")
    assert first_chapter["start_seconds"] > 0
