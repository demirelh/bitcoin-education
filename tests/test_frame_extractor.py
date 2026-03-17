"""Tests for the frame extraction pipeline stage."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core.frame_extractor import (
    FrameExtractionResult,
    _assign_frame_to_chapter,
    _build_chapter_timeline,
    _compute_chapters_hash,
    _compute_video_hash,
    _is_extraction_current,
    extract_frames,
)
from btcedu.db import Base
from btcedu.models.episode import Episode, EpisodeStatus, PipelineRun


@pytest.fixture
def engine():
    eng = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    s = sessionmaker(bind=engine)()
    yield s
    s.rollback()
    s.close()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        raw_data_dir=str(tmp_path / "raw"),
        outputs_dir=str(tmp_path / "outputs"),
        frame_extraction_enabled=True,
        frame_extract_scene_threshold=0.3,
        frame_extract_min_interval=2.0,
        frame_extract_max_frames=50,
        frame_extract_style_preset="news_recolor",
        frame_extract_anchor_detection=True,
        frame_extract_crop_anchor=True,
        dry_run=False,
    )


@pytest.fixture
def episode(session):
    ep = Episode(
        episode_id="ep_frame_test",
        source="youtube_rss",
        title="Frame Extraction Test",
        url="https://example.com/video",
        status=EpisodeStatus.CHAPTERIZED,
        pipeline_version=2,
    )
    session.add(ep)
    session.commit()
    return ep


CHAPTERS_DOC = {
    "schema_version": "1.0",
    "episode_id": "ep_frame_test",
    "title": "Test Episode",
    "total_chapters": 3,
    "estimated_duration_seconds": 90,
    "chapters": [
        {
            "chapter_id": "ch01",
            "title": "Intro",
            "order": 1,
            "narration": {
                "text": "Welcome to the news.",
                "word_count": 5,
                "estimated_duration_seconds": 30,
            },
            "visual": {
                "type": "b_roll",
                "description": "News studio",
                "image_prompt": "A professional news studio",
            },
            "overlays": [],
            "transitions": {"in": "fade", "out": "cut"},
        },
        {
            "chapter_id": "ch02",
            "title": "Main Story",
            "order": 2,
            "narration": {
                "text": "Today we discuss economics.",
                "word_count": 5,
                "estimated_duration_seconds": 30,
            },
            "visual": {
                "type": "diagram",
                "description": "Economic chart",
                "image_prompt": "A chart showing economic growth",
            },
            "overlays": [],
            "transitions": {"in": "cut", "out": "cut"},
        },
        {
            "chapter_id": "ch03",
            "title": "Conclusion",
            "order": 3,
            "narration": {
                "text": "Thank you for watching.",
                "word_count": 5,
                "estimated_duration_seconds": 30,
            },
            "visual": {
                "type": "b_roll",
                "description": "Sunset",
                "image_prompt": "A sunset",
            },
            "overlays": [],
            "transitions": {"in": "cut", "out": "fade"},
        },
    ],
}


@pytest.fixture
def setup_files(tmp_path, settings):
    """Create the directory structure and chapters.json."""
    ep_dir = Path(settings.outputs_dir) / "ep_frame_test"
    ep_dir.mkdir(parents=True, exist_ok=True)
    (ep_dir / "chapters.json").write_text(json.dumps(CHAPTERS_DOC), encoding="utf-8")

    # Create a fake video file
    raw_dir = Path(settings.raw_data_dir) / "ep_frame_test"
    raw_dir.mkdir(parents=True, exist_ok=True)
    video_file = raw_dir / "video.mp4"
    video_file.write_bytes(b"\x00" * 1024)

    video_meta = raw_dir / "video_meta.json"
    video_meta.write_text(
        json.dumps({"video_path": str(video_file), "downloaded_at": "2026-01-01T00:00:00"}),
        encoding="utf-8",
    )
    return ep_dir, video_file


class TestHelpers:
    def test_compute_video_hash_deterministic(self, tmp_path):
        f = tmp_path / "test.mp4"
        f.write_bytes(b"hello")
        h1 = _compute_video_hash(f)
        h2 = _compute_video_hash(f)
        assert h1 == h2
        assert len(h1) == 64  # sha256 hex

    def test_compute_chapters_hash(self):
        from btcedu.models.chapter_schema import ChapterDocument

        doc = ChapterDocument(**CHAPTERS_DOC)
        h = _compute_chapters_hash(doc)
        assert len(h) == 64

    def test_build_chapter_timeline(self):
        from btcedu.models.chapter_schema import ChapterDocument

        doc = ChapterDocument(**CHAPTERS_DOC)
        timeline = _build_chapter_timeline(doc)
        assert len(timeline) == 3
        assert timeline[0] == ("ch01", 0.0, 30.0)
        assert timeline[1] == ("ch02", 30.0, 60.0)
        assert timeline[2] == ("ch03", 60.0, 90.0)

    def test_assign_frame_to_chapter(self):
        timeline = [("ch01", 0.0, 30.0), ("ch02", 30.0, 60.0), ("ch03", 60.0, 90.0)]
        assert _assign_frame_to_chapter(5.0, timeline) == "ch01"
        assert _assign_frame_to_chapter(35.0, timeline) == "ch02"
        assert _assign_frame_to_chapter(65.0, timeline) == "ch03"
        # Beyond end → last chapter
        assert _assign_frame_to_chapter(100.0, timeline) == "ch03"

    def test_is_extraction_current_no_files(self, tmp_path):
        assert not _is_extraction_current(
            tmp_path / "manifest.json", tmp_path / "prov.json", "abc", "def"
        )

    def test_is_extraction_current_stale(self, tmp_path):
        manifest = tmp_path / "manifest.json"
        prov = tmp_path / "prov.json"
        manifest.write_text("{}")
        prov.write_text(json.dumps({"video_hash": "abc", "chapters_hash": "def"}))
        # Create stale marker
        (tmp_path / ".stale").write_text("{}")
        assert not _is_extraction_current(manifest, prov, "abc", "def")

    def test_is_extraction_current_valid(self, tmp_path):
        manifest = tmp_path / "frames" / "manifest.json"
        prov = tmp_path / "prov.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text("{}")
        prov.write_text(json.dumps({"video_hash": "abc", "chapters_hash": "def"}))
        assert _is_extraction_current(manifest, prov, "abc", "def")


class TestExtractFrames:
    @patch("btcedu.services.frame_extraction_service.OpenCVFrameAnalyzer")
    @patch("btcedu.services.ffmpeg_service.apply_style_filter")
    @patch("btcedu.services.ffmpeg_service.crop_frame")
    @patch("btcedu.services.ffmpeg_service.extract_keyframes")
    def test_basic_extraction_happy_path(
        self,
        mock_extract,
        mock_crop,
        mock_style,
        mock_analyzer_cls,
        session,
        episode,
        settings,
        setup_files,
    ):
        ep_dir, video_file = setup_files
        styled_dir = ep_dir / "frames" / "styled"
        styled_dir.mkdir(parents=True, exist_ok=True)

        # Mock keyframes — 3 frames at different timestamps
        from btcedu.services.ffmpeg_service import ExtractedFrame

        mock_extract.return_value = [
            ExtractedFrame("f1.png", 10.0, 0.5, 1920, 1080, 1000),
            ExtractedFrame("f2.png", 40.0, 0.7, 1920, 1080, 1000),
            ExtractedFrame("f3.png", 70.0, 0.4, 1920, 1080, 1000),
        ]

        # Mock anchor detection: no anchors
        from btcedu.services.frame_extraction_service import AnchorDetection

        analyzer_instance = MagicMock()
        analyzer_instance.available = True
        analyzer_instance.detect_anchor.return_value = AnchorDetection(False, 0.0, None, 1920, 1080)
        mock_analyzer_cls.return_value = analyzer_instance

        # Mock style filter: create output files
        def fake_style(input_path, output_path, **kwargs):
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"\x89PNG")
            return output_path

        mock_style.side_effect = fake_style

        result = extract_frames(session, "ep_frame_test", settings)

        assert isinstance(result, FrameExtractionResult)
        assert result.total_frames == 3
        assert result.anchor_frames == 0
        assert result.assigned_frames == 3
        assert not result.skipped
        assert episode.status == EpisodeStatus.FRAMES_EXTRACTED

        # Manifest should exist
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest["episode_id"] == "ep_frame_test"
        assert len(manifest["chapter_assignments"]) == 3

    def test_disabled_feature_flag(self, session, episode, settings, setup_files):
        settings.frame_extraction_enabled = False
        result = extract_frames(session, "ep_frame_test", settings)
        assert result.skipped is True
        assert episode.status == EpisodeStatus.FRAMES_EXTRACTED

    @patch("btcedu.services.ffmpeg_service.extract_keyframes")
    @patch("btcedu.services.ffmpeg_service.apply_style_filter")
    @patch("btcedu.services.frame_extraction_service.OpenCVFrameAnalyzer")
    def test_no_video_creates_empty_manifest(
        self, mock_analyzer_cls, mock_style, mock_extract, session, episode, settings, tmp_path
    ):
        # Setup chapters but NO video
        ep_dir = Path(settings.outputs_dir) / "ep_frame_test"
        ep_dir.mkdir(parents=True, exist_ok=True)
        (ep_dir / "chapters.json").write_text(json.dumps(CHAPTERS_DOC), encoding="utf-8")

        raw_dir = Path(settings.raw_data_dir) / "ep_frame_test"
        raw_dir.mkdir(parents=True, exist_ok=True)
        # No video file created

        result = extract_frames(session, "ep_frame_test", settings)
        assert result.total_frames == 0
        assert episode.status == EpisodeStatus.FRAMES_EXTRACTED

        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert manifest.get("error") == "no_video_file"

    @patch("btcedu.services.frame_extraction_service.OpenCVFrameAnalyzer")
    @patch("btcedu.services.ffmpeg_service.apply_style_filter")
    @patch("btcedu.services.ffmpeg_service.crop_frame")
    @patch("btcedu.services.ffmpeg_service.extract_keyframes")
    def test_anchor_detection_and_crop(
        self,
        mock_extract,
        mock_crop,
        mock_style,
        mock_analyzer_cls,
        session,
        episode,
        settings,
        setup_files,
    ):
        ep_dir, _ = setup_files
        styled_dir = ep_dir / "frames" / "styled"
        styled_dir.mkdir(parents=True, exist_ok=True)

        from btcedu.services.ffmpeg_service import ExtractedFrame
        from btcedu.services.frame_extraction_service import AnchorDetection

        mock_extract.return_value = [
            ExtractedFrame("f1.png", 10.0, 0.6, 1920, 1080, 1000),
        ]

        # Mock: anchor detected
        analyzer_instance = MagicMock()
        analyzer_instance.available = True
        analyzer_instance.detect_anchor.return_value = AnchorDetection(
            True, 0.85, (800, 700, 200, 250), 1920, 1080
        )
        analyzer_instance.compute_crop_region.return_value = (0, 0, 1920, 665)
        mock_analyzer_cls.return_value = analyzer_instance

        mock_crop.return_value = "cropped.png"

        def fake_style(input_path, output_path, **kwargs):
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"\x89PNG")
            return output_path

        mock_style.side_effect = fake_style

        result = extract_frames(session, "ep_frame_test", settings)
        assert result.anchor_frames == 1
        assert result.cropped_frames == 1
        mock_crop.assert_called_once()

    @patch("btcedu.services.ffmpeg_service.extract_keyframes")
    @patch("btcedu.services.ffmpeg_service.apply_style_filter")
    @patch("btcedu.services.frame_extraction_service.OpenCVFrameAnalyzer")
    def test_idempotency_skips_when_current(
        self, mock_analyzer_cls, mock_style, mock_extract, session, episode, settings, setup_files
    ):
        ep_dir, video_file = setup_files
        frames_dir = ep_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        # Write existing manifest + provenance with correct hashes
        from btcedu.models.chapter_schema import ChapterDocument

        doc = ChapterDocument(**CHAPTERS_DOC)
        video_hash = _compute_video_hash(video_file)
        chapters_hash = _compute_chapters_hash(doc)

        (frames_dir / "manifest.json").write_text(
            json.dumps({"episode_id": "ep_frame_test", "chapter_assignments": []})
        )
        prov_dir = ep_dir / "provenance"
        prov_dir.mkdir(parents=True, exist_ok=True)
        (prov_dir / "frames_provenance.json").write_text(
            json.dumps({"video_hash": video_hash, "chapters_hash": chapters_hash})
        )

        result = extract_frames(session, "ep_frame_test", settings)
        assert result.skipped is True
        mock_extract.assert_not_called()

    @patch("btcedu.services.ffmpeg_service.extract_keyframes")
    @patch("btcedu.services.ffmpeg_service.apply_style_filter")
    @patch("btcedu.services.frame_extraction_service.OpenCVFrameAnalyzer")
    def test_force_bypasses_idempotency(
        self, mock_analyzer_cls, mock_style, mock_extract, session, episode, settings, setup_files
    ):
        ep_dir, video_file = setup_files
        frames_dir = ep_dir / "frames"
        frames_dir.mkdir(parents=True, exist_ok=True)

        # Write existing provenance
        from btcedu.models.chapter_schema import ChapterDocument

        doc = ChapterDocument(**CHAPTERS_DOC)
        video_hash = _compute_video_hash(video_file)
        chapters_hash = _compute_chapters_hash(doc)

        (frames_dir / "manifest.json").write_text(json.dumps({"chapter_assignments": []}))
        prov_dir = ep_dir / "provenance"
        prov_dir.mkdir(parents=True, exist_ok=True)
        (prov_dir / "frames_provenance.json").write_text(
            json.dumps({"video_hash": video_hash, "chapters_hash": chapters_hash})
        )

        mock_extract.return_value = []
        analyzer_instance = MagicMock()
        analyzer_instance.available = True
        mock_analyzer_cls.return_value = analyzer_instance

        result = extract_frames(session, "ep_frame_test", settings, force=True)
        assert result.skipped is False
        mock_extract.assert_called_once()

    def test_v1_episode_raises(self, session, settings, setup_files):
        ep = Episode(
            episode_id="ep_v1",
            source="youtube_rss",
            title="V1 Episode",
            url="https://example.com",
            status=EpisodeStatus.CHAPTERIZED,
            pipeline_version=1,
        )
        session.add(ep)
        session.commit()

        with pytest.raises(ValueError, match="v1 pipeline"):
            extract_frames(session, "ep_v1", settings)

    def test_wrong_status_raises(self, session, settings, setup_files):
        ep = Episode(
            episode_id="ep_wrong",
            source="youtube_rss",
            title="Wrong Status",
            url="https://example.com",
            status=EpisodeStatus.TRANSLATED,
            pipeline_version=2,
        )
        session.add(ep)
        session.commit()

        with pytest.raises(ValueError, match="status"):
            extract_frames(session, "ep_wrong", settings)

    @patch("btcedu.services.ffmpeg_service.extract_keyframes")
    @patch("btcedu.services.ffmpeg_service.apply_style_filter")
    @patch("btcedu.services.frame_extraction_service.OpenCVFrameAnalyzer")
    def test_pipeline_run_record_created(
        self, mock_analyzer_cls, mock_style, mock_extract, session, episode, settings, setup_files
    ):
        ep_dir, _ = setup_files
        styled_dir = ep_dir / "frames" / "styled"
        styled_dir.mkdir(parents=True, exist_ok=True)

        mock_extract.return_value = []
        analyzer_instance = MagicMock()
        analyzer_instance.available = True
        mock_analyzer_cls.return_value = analyzer_instance

        extract_frames(session, "ep_frame_test", settings)

        runs = session.query(PipelineRun).filter(PipelineRun.stage == "frameextract").all()
        assert len(runs) == 1
        assert runs[0].status == "success"

    @patch("btcedu.services.ffmpeg_service.extract_keyframes")
    @patch("btcedu.services.frame_extraction_service.OpenCVFrameAnalyzer")
    def test_cascade_invalidation(
        self, mock_analyzer_cls, mock_extract, session, episode, settings, setup_files
    ):
        ep_dir, _ = setup_files
        styled_dir = ep_dir / "frames" / "styled"
        styled_dir.mkdir(parents=True, exist_ok=True)

        # Create images directory so stale marker can be written
        images_dir = ep_dir / "images"
        images_dir.mkdir(parents=True, exist_ok=True)

        mock_extract.return_value = []
        analyzer_instance = MagicMock()
        analyzer_instance.available = True
        mock_analyzer_cls.return_value = analyzer_instance

        with patch("btcedu.services.ffmpeg_service.apply_style_filter"):
            extract_frames(session, "ep_frame_test", settings)

        stale = images_dir / ".stale"
        assert stale.exists()
        data = json.loads(stale.read_text())
        assert data["invalidated_by"] == "frameextract"
