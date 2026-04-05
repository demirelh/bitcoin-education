"""Tests for frame_editor core module — Gemini frame editing orchestration."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.config import Settings
from btcedu.core.frame_editor import FrameEditResult, _build_edit_prompt, edit_frames
from btcedu.models.episode import Episode, EpisodeStatus


@pytest.fixture
def gemini_settings():
    return Settings(
        database_url="sqlite:///:memory:",
        gemini_api_key="test-gemini-key",
        gemini_image_model="gemini-2.0-flash-exp",
        gemini_image_edit_enabled=True,
        outputs_dir="data/outputs",
        dry_run=False,
        max_episode_cost_usd=10.0,
    )


@pytest.fixture
def framed_episode(db_session, gemini_settings, tmp_path):
    """Episode at FRAMES_EXTRACTED with frames manifest and chapters."""
    eid = "test-frame-edit"
    ep = Episode(
        episode_id=eid,
        title="Test Frame Edit",
        url="https://example.com",
        status=EpisodeStatus.FRAMES_EXTRACTED,
        pipeline_version=2,
        content_profile="tagesschau_tr",
    )
    db_session.add(ep)
    db_session.commit()

    # Use tmp_path as outputs_dir
    gemini_settings.outputs_dir = str(tmp_path)
    ep_dir = tmp_path / eid
    frames_dir = ep_dir / "frames"
    styled_dir = frames_dir / "styled"
    styled_dir.mkdir(parents=True)

    # Create a fake styled frame
    frame_path = styled_dir / "frame_001.png"
    frame_path.write_bytes(b"fake-png-data")

    # Write frames manifest
    manifest = {
        "episode_id": eid,
        "schema_version": "1.0",
        "chapter_assignments": [
            {
                "chapter_id": "ch_01",
                "assigned_frame": str(frame_path),
                "timestamp_seconds": 5.0,
                "scene_score": 0.8,
            },
            {
                "chapter_id": "ch_02",
                "assigned_frame": str(frame_path),
                "timestamp_seconds": 30.0,
                "scene_score": 0.7,
            },
        ],
    }
    (frames_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    # Write chapters.json
    chapters = {
        "schema_version": "1.0",
        "episode_id": eid,
        "title": "Test Episode",
        "total_chapters": 2,
        "estimated_duration_seconds": 9,
        "chapters": [
            {
                "chapter_id": "ch_01",
                "title": "Die Wirtschaft",
                "order": 1,
                "narration": {
                    "text": "Almanya ekonomisi bu hafta pozitif sinyaller verdi.",
                    "word_count": 7,
                    "estimated_duration_seconds": 5,
                },
                "visual": {
                    "type": "b_roll",
                    "description": "Economic charts and graphs",
                    "image_prompt": "German economic charts",
                },
                "overlays": [],
                "transitions": {"in": "fade", "out": "fade"},
            },
            {
                "chapter_id": "ch_02",
                "title": "Das Wetter",
                "order": 2,
                "narration": {
                    "text": "Hava durumu: Almanya genelinde yagisli.",
                    "word_count": 6,
                    "estimated_duration_seconds": 4,
                },
                "visual": {
                    "type": "b_roll",
                    "description": "Weather map of Germany",
                    "image_prompt": "German weather map",
                },
                "overlays": [],
                "transitions": {"in": "fade", "out": "fade"},
            },
        ],
    }
    (ep_dir / "chapters.json").write_text(json.dumps(chapters), encoding="utf-8")

    return ep, gemini_settings


class TestBuildEditPrompt:
    def test_renders_template(self):
        prompt = _build_edit_prompt(
            chapter_title="Die Wirtschaft",
            narration_text="Almanya ekonomisi pozitif.",
            visual_description="Charts and graphs",
        )
        assert "Die Wirtschaft" in prompt
        assert "Almanya ekonomisi pozitif" in prompt
        assert "Turkish" in prompt

    def test_renders_without_visual_desc(self):
        prompt = _build_edit_prompt(
            chapter_title="Test",
            narration_text="Test narration",
        )
        assert "Test" in prompt


def _mock_gemini_post():
    """Return a mock requests.post that returns a Gemini image edit response."""
    import base64

    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {"text": "Edited."},
                        {
                            "inlineData": {
                                "mimeType": "image/png",
                                "data": base64.b64encode(b"edited-png").decode(),
                            }
                        },
                    ]
                }
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 500,
            "candidatesTokenCount": 300,
        },
    }
    return mock_resp


class TestEditFrames:
    @patch("btcedu.services.gemini_image_service.requests.post")
    def test_edit_frames_success(self, mock_post, framed_episode, db_session):
        ep, settings = framed_episode
        mock_post.return_value = _mock_gemini_post()

        result = edit_frames(db_session, ep.episode_id, settings, force=False)

        assert isinstance(result, FrameEditResult)
        assert result.chapters_edited == 2
        assert result.chapters_skipped == 0
        assert result.total_cost_usd > 0
        assert not result.skipped

        # Episode advanced
        db_session.refresh(ep)
        assert ep.status == EpisodeStatus.IMAGES_GENERATED

        # Manifest written
        manifest_path = Path(settings.outputs_dir) / ep.episode_id / "images" / "manifest.json"
        assert manifest_path.exists()
        manifest = json.loads(manifest_path.read_text())
        assert manifest["method"] == "gemini_frame_edit"
        assert len(manifest["entries"]) == 2

        # Edited files exist
        images_dir = Path(settings.outputs_dir) / ep.episode_id / "images"
        assert (images_dir / "ch_01_edited.png").exists()
        assert (images_dir / "ch_02_edited.png").exists()

    def test_edit_frames_dry_run(self, framed_episode, db_session):
        ep, settings = framed_episode
        settings.dry_run = True

        result = edit_frames(db_session, ep.episode_id, settings, force=False)

        assert result.chapters_skipped == 2
        assert result.chapters_edited == 0
        assert result.total_cost_usd == 0.0

        # Files should exist (copied from source)
        images_dir = Path(settings.outputs_dir) / ep.episode_id / "images"
        assert (images_dir / "ch_01_edited.png").exists()
        assert (images_dir / "ch_02_edited.png").exists()

    @patch("btcedu.services.gemini_image_service.requests.post")
    def test_edit_frames_gemini_failure_falls_back(
        self, mock_post, framed_episode, db_session
    ):
        ep, settings = framed_episode

        # Simulate API failure
        mock_post.return_value = MagicMock(
            ok=False, status_code=400, text="Bad Request"
        )

        result = edit_frames(db_session, ep.episode_id, settings, force=False)

        # Should fall back to copying original frames
        assert result.chapters_edited == 0
        assert result.chapters_skipped == 2

        # Episode still advances
        db_session.refresh(ep)
        assert ep.status == EpisodeStatus.IMAGES_GENERATED

    @patch("btcedu.services.gemini_image_service.requests.post")
    def test_edit_frames_idempotent(self, mock_post, framed_episode, db_session):
        ep, settings = framed_episode
        mock_post.return_value = _mock_gemini_post()

        # First run
        result1 = edit_frames(db_session, ep.episode_id, settings, force=False)
        assert not result1.skipped

        # Second run should be idempotent (skipped)
        result2 = edit_frames(db_session, ep.episode_id, settings, force=False)
        assert result2.skipped

    def test_edit_frames_no_manifest(self, db_session, gemini_settings, tmp_path):
        """Episode without frames manifest should skip gracefully."""
        eid = "no-frames-ep"
        ep = Episode(
            episode_id=eid,
            title="No Frames",
            url="https://example.com",
            status=EpisodeStatus.FRAMES_EXTRACTED,
            pipeline_version=2,
        )
        db_session.add(ep)
        db_session.commit()

        gemini_settings.outputs_dir = str(tmp_path)
        (tmp_path / eid).mkdir()

        result = edit_frames(db_session, eid, gemini_settings, force=False)
        assert result.skipped

    def test_edit_frames_wrong_status_raises(self, db_session, gemini_settings, tmp_path):
        ep = Episode(
            episode_id="wrong-status",
            title="Wrong Status",
            url="https://example.com",
            status=EpisodeStatus.NEW,
            pipeline_version=2,
        )
        db_session.add(ep)
        db_session.commit()

        gemini_settings.outputs_dir = str(tmp_path)

        with pytest.raises(ValueError, match="status"):
            edit_frames(db_session, "wrong-status", gemini_settings)

    def test_edit_frames_v1_raises(self, db_session, gemini_settings, tmp_path):
        ep = Episode(
            episode_id="v1-ep",
            title="V1 Episode",
            url="https://example.com",
            status=EpisodeStatus.FRAMES_EXTRACTED,
            pipeline_version=1,
        )
        db_session.add(ep)
        db_session.commit()

        gemini_settings.outputs_dir = str(tmp_path)

        with pytest.raises(ValueError, match="v1"):
            edit_frames(db_session, "v1-ep", gemini_settings)
