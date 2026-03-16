"""Tests for Phase 4 video normalization failure fallback in finalize_selections."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.core.stock_images import finalize_selections
from btcedu.models.episode import Episode, EpisodeStatus


@pytest.fixture
def video_episode(db_session, tmp_path):
    """Episode with a video candidate selected in candidates_manifest."""
    episode = Episode(
        episode_id="ep_vid",
        source="youtube_rss",
        title="Video Normalization Test",
        url="https://youtube.com/watch?v=ep_vid",
        status=EpisodeStatus.IMAGES_GENERATED,
        pipeline_version=2,
    )
    db_session.add(episode)
    db_session.commit()

    # Create directory structure
    ep_dir = tmp_path / "ep_vid"
    candidates_dir = ep_dir / "images" / "candidates" / "ch01"
    candidates_dir.mkdir(parents=True)

    # Create a minimal (empty) video file — normalization is mocked, so content doesn't matter
    video_file = candidates_dir / "pexels_v_99999.mp4"
    video_file.write_bytes(b"fake-video-data")

    # Write candidates manifest (schema 3.1) with one video candidate selected
    manifest = {
        "episode_id": "ep_vid",
        "schema_version": "3.1",
        "searched_at": "2026-03-16T00:00:00+00:00",
        "chapters_hash": "test_hash",
        "chapters": {
            "ch01": {
                "search_query": "bitcoin technology",
                "candidates": [
                    {
                        "pexels_id": 99999,
                        "photographer": "Test Photographer",
                        "photographer_url": "https://www.pexels.com/@test",
                        "source_url": "https://www.pexels.com/video/99999/",
                        "download_url": "https://videos.pexels.com/99999/hd.mp4",
                        "local_path": f"images/candidates/ch01/pexels_v_99999.mp4",
                        "alt_text": "",
                        "width": 1920,
                        "height": 1080,
                        "size_bytes": 15,
                        "duration_seconds": 8,
                        "downloaded_at": "2026-03-16T00:00:00+00:00",
                        "selected": True,
                        "locked": False,
                        "asset_type": "video",
                    }
                ],
            }
        },
    }
    manifest_path = ep_dir / "images" / "candidates" / "candidates_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    return {"episode_id": "ep_vid", "tmp_path": tmp_path, "ep_dir": ep_dir}


@pytest.fixture
def settings_for_video(tmp_path):
    """Minimal settings mock for finalize_selections video tests."""
    s = MagicMock()
    s.outputs_dir = str(tmp_path)
    s.render_resolution = "1920x1080"
    s.render_fps = 30
    s.render_crf = 23
    s.render_preset = "medium"
    s.render_timeout_segment = 300
    s.dry_run = False
    return s


class TestFinalizeVideoNormalizationFailure:
    @patch("btcedu.core.stock_images._create_placeholder_entry")
    @patch("btcedu.core.stock_images._load_chapters")
    def test_normalization_failure_creates_placeholder(
        self,
        mock_load_chapters,
        mock_placeholder,
        db_session,
        video_episode,
        settings_for_video,
        caplog,
    ):
        """When normalize_video_clip() raises, finalize_selections uses placeholder instead of crashing."""
        import logging

        episode_id = video_episode["episode_id"]

        # Set up mock chapter matching the video candidate
        ch1 = MagicMock()
        ch1.chapter_id = "ch01"
        ch1.title = "Bitcoin Teknolojisi"
        ch1.visual = MagicMock()
        ch1.visual.type = "b_roll"
        ch1.visual.description = "Bitcoin network nodes"

        mock_doc = MagicMock()
        mock_doc.chapters = [ch1]
        mock_doc.schema_version = "1.0"
        mock_load_chapters.return_value = mock_doc

        # Placeholder mock returns a valid photo entry
        mock_placeholder.return_value = {
            "chapter_id": "ch01",
            "chapter_title": "Bitcoin Teknolojisi",
            "visual_type": "b_roll",
            "asset_type": "photo",
            "file_path": "images/ch01_placeholder.png",
            "prompt": None,
            "generation_method": "template",
            "model": None,
            "size": "1920x1080",
            "mime_type": "image/png",
            "size_bytes": 0,
        }

        # Patch normalize_video_clip at the source module — it's lazily imported
        with patch(
            "btcedu.services.ffmpeg_service.normalize_video_clip",
            side_effect=RuntimeError("ffmpeg normalization failed: codec not supported"),
        ):
            with caplog.at_level(logging.WARNING, logger="btcedu.core.stock_images"):
                result = finalize_selections(db_session, episode_id, settings_for_video)

        # No exception raised — graceful degradation
        assert result is not None
        assert result.placeholder_count == 1
        assert result.selected_count == 0

        # Placeholder entry was used, not a video entry
        mock_placeholder.assert_called_once()
        call_args = mock_placeholder.call_args[0]
        assert call_args[0] is ch1  # first arg is the chapter object

        # Warning was logged
        assert any(
            "normalization failed" in record.message.lower()
            or "placeholder" in record.message.lower()
            for record in caplog.records
        )

    @patch("btcedu.core.stock_images._create_placeholder_entry")
    @patch("btcedu.core.stock_images._load_chapters")
    def test_normalization_failure_manifest_written(
        self,
        mock_load_chapters,
        mock_placeholder,
        db_session,
        video_episode,
        settings_for_video,
    ):
        """Even after normalization failure, manifest.json is still written."""
        episode_id = video_episode["episode_id"]
        ep_dir = video_episode["ep_dir"]

        ch1 = MagicMock()
        ch1.chapter_id = "ch01"
        ch1.title = "Bitcoin Teknolojisi"
        ch1.visual = MagicMock()
        ch1.visual.type = "b_roll"
        ch1.visual.description = "Bitcoin network nodes"

        mock_doc = MagicMock()
        mock_doc.chapters = [ch1]
        mock_doc.schema_version = "1.0"
        mock_load_chapters.return_value = mock_doc

        mock_placeholder.return_value = {
            "chapter_id": "ch01",
            "chapter_title": "Bitcoin Teknolojisi",
            "visual_type": "b_roll",
            "asset_type": "photo",
            "file_path": "images/ch01_placeholder.png",
            "prompt": None,
            "generation_method": "template",
            "model": None,
            "size": "1920x1080",
            "mime_type": "image/png",
            "size_bytes": 0,
        }

        with patch(
            "btcedu.services.ffmpeg_service.normalize_video_clip",
            side_effect=RuntimeError("ffmpeg failed"),
        ):
            finalize_selections(db_session, episode_id, settings_for_video)

        manifest_path = ep_dir / "images" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["episode_id"] == episode_id
        assert len(manifest["images"]) == 1
        # The entry uses placeholder (asset_type: photo), not the video
        assert manifest["images"][0]["asset_type"] == "photo"
