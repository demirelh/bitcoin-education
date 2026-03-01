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
    render_video,
)
from btcedu.db import Base
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

    img, aud, dur = _resolve_chapter_media("ch01", image_manifest, tts_manifest, base_dir)

    assert img == image_path
    assert aud == audio_path
    assert dur == 60.5


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


def test_render_video_v1_pipeline(db_session, settings):
    """Test render rejects v1 pipeline episodes."""
    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TTS_DONE,
        pipeline_version=1,
    )
    db_session.add(episode)
    db_session.commit()

    with pytest.raises(ValueError, match="v1 pipeline"):
        render_video(db_session, "ep001", settings)


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


def test_render_video_error_rollback(db_session, settings, tmp_path):
    """Test that render failure sets PipelineRun to failed and records error.

    When all chapters have missing media, render raises RuntimeError and
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
        pytest.raises(RuntimeError, match="No segments were rendered"),
    ):
        render_video(db_session, "ep001", settings)

    # PipelineRun should be marked as failed
    run = db_session.query(PipelineRun).filter_by(episode_id="ep001", stage="render").first()
    assert run is not None
    assert run.status == "failed"
    assert "No segments were rendered" in run.error_message

    # Episode error_message should be set
    db_session.refresh(episode)
    assert episode.error_message is not None
    assert "No segments were rendered" in episode.error_message
