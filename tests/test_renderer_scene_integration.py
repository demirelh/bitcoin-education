"""The renderer, seen from the outside, with and without a scene plan.

Two things are being protected here. The first is the new behaviour: an episode
that has a plan is cut by scene, and the render manifest says so. The second is
the far more valuable one -- every episode that has *no* plan, which is every
D-ID episode, every Bitcoin-podcast episode and everything rendered before the
planner existed, must come out byte-for-byte identical in its fingerprint, so
nothing on this machine silently re-renders.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from btcedu.config import Settings
from btcedu.core.renderer import (
    _compute_render_content_hash,
    _current_render_content_hash,
    _load_chapters,
    _load_scene_context,
    _scene_hash_block,
    render_video,
)
from btcedu.db import Base
from btcedu.models.episode import Episode, EpisodeStatus

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")


@pytest.fixture
def db_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    # MediaAsset lives on its own declarative base and needs a second pass.
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
    session = sessionmaker(bind=engine)()
    yield session
    session.close()
    engine.dispose()


@pytest.fixture
def settings(tmp_path):
    return Settings(
        outputs_dir=str(tmp_path / "outputs"),
        render_resolution="320x180",
        render_fps=25,
        render_crf=35,
        render_preset="ultrafast",
        render_audio_bitrate="96k",
        render_timeout_segment=600,
        dry_run=True,
    )


def _episode(session, profile: str = "tagesschau_tr") -> Episode:
    episode = Episode(
        episode_id="ep001",
        title="Test",
        url="https://example.com",
        status=EpisodeStatus.TTS_DONE,
        pipeline_version=2,
        content_profile=profile,
    )
    session.add(episode)
    session.commit()
    return episode


def _write_inputs(outputs: Path) -> Path:
    base = outputs / "ep001"
    (base / "images").mkdir(parents=True)
    (base / "tts").mkdir(parents=True)
    (base / "images" / "ch01.png").write_bytes(b"pic")
    (base / "tts" / "ch01.mp3").write_bytes(b"snd")

    (base / "chapters.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "episode_id": "ep001",
                "title": "Test Episode",
                "total_chapters": 1,
                "estimated_duration_seconds": 30,
                "chapters": [
                    {
                        "chapter_id": "ch01",
                        "title": "Intro",
                        "order": 1,
                        "narration": {
                            "text": "Chapter one.",
                            "word_count": 2,
                            "estimated_duration_seconds": 30,
                        },
                        "visual": {
                            "type": "diagram",
                            "description": "A picture",
                            "image_prompt": "a picture",
                        },
                        "overlays": [],
                        "transitions": {"in": "fade", "out": "fade"},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (base / "images" / "manifest.json").write_text(
        json.dumps(
            {
                "episode_id": "ep001",
                "images": [
                    {
                        "chapter_id": "ch01",
                        "file_path": "images/ch01.png",
                        "generation_method": "flux",
                        "asset_type": "photo",
                        "content_hash": "img-1",
                        "metadata": {"beat_index": 0},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (base / "tts" / "manifest.json").write_text(
        json.dumps(
            {
                "episode_id": "ep001",
                "segments": [
                    {
                        "chapter_id": "ch01",
                        "file_path": "tts/ch01.mp3",
                        "duration_seconds": 30.0,
                        "text_hash": "t-1",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return base


def _write_plan(base: Path) -> None:
    (base / "scene_plan.json").write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "episode_id": "ep001",
                "content_hash": "plan-42",
                "presenter_look_id": "look-A",
                "scenes": [
                    {
                        "scene_id": "sc_001",
                        "chapter_id": "ch01",
                        "order": 1,
                        "beat_index": 0,
                        "speaker_role": "anchor_female",
                        "purpose": "news",
                        "segment_indices": [0],
                        "text_hash": "t",
                        "audio_file": None,
                        "expected_duration_seconds": 30.0,
                        "visual_mode": "studio_composite",
                        "template_id": "studio_anchor_medium",
                        "background_asset": "images/ch01.png",
                        "background_asset_type": "image",
                        "display_zone_id": "main_wall",
                        "display_fit_mode": "cover",
                        "focus_point": [0.5, 0.5],
                        "transition_in": "cut",
                        "overlays": {},
                        "needs_avatar": True,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


class TestBackwardCompatibility:
    """Everything that has no scene plan must be untouched."""

    def test_an_episode_without_a_plan_renders_as_before(self, db_session, settings, tmp_path):
        _episode(db_session, profile="bitcoin_podcast")
        _write_inputs(Path(settings.outputs_dir))

        result = render_video(db_session, "ep001", settings)

        assert result.segment_count == 1
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        # No scene keys at all: an old reader sees exactly the old document.
        assert "scenes" not in manifest
        assert "scene_plan_hash" not in manifest
        assert "segments" in manifest
        assert "timeline" in manifest

    def test_the_content_hash_of_a_planless_episode_is_unchanged(self, settings, tmp_path):
        base = _write_inputs(Path(settings.outputs_dir))
        chapters = _load_chapters(base / "chapters.json")
        images = json.loads((base / "images" / "manifest.json").read_text(encoding="utf-8"))
        tts = json.loads((base / "tts" / "manifest.json").read_text(encoding="utf-8"))

        without_argument = _compute_render_content_hash(chapters, images, tts, None)
        with_empty_scene_context = _compute_render_content_hash(
            chapters, images, tts, None, scene_context=None
        )

        assert without_argument == with_empty_scene_context

    def test_no_plan_means_no_scene_context(self, settings, tmp_path):
        base = _write_inputs(Path(settings.outputs_dir))

        assert _load_scene_context(base, settings, None, {}, {}) is None
        assert _scene_hash_block(base, settings, None, {}, {}) is None

    def test_a_bitcoin_podcast_episode_never_looks_for_a_studio(self, db_session, settings):
        from btcedu.core.scene_renderer import studio_directory

        episode = _episode(db_session, profile="bitcoin_podcast")

        assert studio_directory(settings, episode) == ""


class TestScenePlanIsRecognised:
    def test_a_disabled_anchor_ignores_a_studio_plan(self, db_session, settings, tmp_path):
        episode = _episode(db_session)
        base = _write_inputs(Path(settings.outputs_dir))
        _write_plan(base)
        images = json.loads((base / "images" / "manifest.json").read_text(encoding="utf-8"))
        tts = json.loads((base / "tts" / "manifest.json").read_text(encoding="utf-8"))

        assert settings.anchor_enabled is False
        assert _load_scene_context(base, settings, episode, images, tts) is None
        assert _scene_hash_block(base, settings, episode, images, tts) is None

    def test_the_plan_contributes_to_the_content_hash(self, db_session, settings, tmp_path):
        episode = _episode(db_session)
        base = _write_inputs(Path(settings.outputs_dir))
        images = json.loads((base / "images" / "manifest.json").read_text(encoding="utf-8"))
        tts = json.loads((base / "tts" / "manifest.json").read_text(encoding="utf-8"))
        settings.anchor_enabled = True

        before = _scene_hash_block(base, settings, episode, images, tts)
        _write_plan(base)
        after = _scene_hash_block(base, settings, episode, images, tts)

        assert before is None
        assert after is not None
        assert after["scene_plan_hash"] == "plan-42"

    def test_a_changed_plan_changes_the_render_hash(
        self, db_session, settings, tmp_path
    ):
        episode = _episode(db_session)
        base = _write_inputs(Path(settings.outputs_dir))
        _write_plan(base)
        chapters = _load_chapters(base / "chapters.json")
        images = json.loads((base / "images" / "manifest.json").read_text(encoding="utf-8"))
        tts = json.loads((base / "tts" / "manifest.json").read_text(encoding="utf-8"))
        settings.anchor_enabled = True

        first = _compute_render_content_hash(
            chapters, images, tts, None,
            scene_context=_scene_hash_block(base, settings, episode, images, tts),
        )
        plan = json.loads((base / "scene_plan.json").read_text(encoding="utf-8"))
        plan["content_hash"] = "plan-43"
        (base / "scene_plan.json").write_text(json.dumps(plan), encoding="utf-8")
        second = _compute_render_content_hash(
            chapters, images, tts, None,
            scene_context=_scene_hash_block(base, settings, episode, images, tts),
        )

        assert first != second

    def test_the_local_and_remote_hash_agree(self, db_session, settings, tmp_path):
        """Both sides must arrive at the same number, or the Pi loops for ever."""
        _episode(db_session)
        base = _write_inputs(Path(settings.outputs_dir))
        _write_plan(base)
        settings.anchor_enabled = True

        first = _current_render_content_hash(db_session, "ep001", settings)
        second = _current_render_content_hash(db_session, "ep001", settings)

        assert first == second
        assert first is not None

    def test_the_dry_run_keeps_the_chapter_path(self, db_session, settings, tmp_path):
        """A dry run makes no clip and no studio, so it must not try to."""
        _episode(db_session)
        base = _write_inputs(Path(settings.outputs_dir))
        _write_plan(base)
        settings.dry_run = True

        result = render_video(db_session, "ep001", settings)

        assert result.segment_count == 1
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))
        assert "scenes" not in manifest

    def test_a_render_never_costs_anything(self, db_session, settings, tmp_path):
        _episode(db_session)
        base = _write_inputs(Path(settings.outputs_dir))
        _write_plan(base)

        result = render_video(db_session, "ep001", settings)
        provenance = json.loads(result.provenance_path.read_text(encoding="utf-8"))

        assert provenance["cost_usd"] == 0.0


def _reporter_scene(scene_id: str, order: int, indices: list[int]) -> dict:
    return {
        "scene_id": scene_id,
        "chapter_id": "ch01",
        "order": order,
        "beat_index": order - 1,
        "speaker_role": "reporter_male",
        "purpose": "news",
        "segment_indices": indices,
        "text_hash": f"t-{scene_id}",
        "audio_file": None,
        "expected_duration_seconds": 1.0,
        "visual_mode": "fullscreen_media",
        "template_id": "reporter_fullscreen",
        "background_asset": "images/ch01.png",
        "background_asset_type": "image",
        "display_zone_id": None,
        "display_fit_mode": "cover",
        "focus_point": [0.5, 0.5],
        "transition_in": "cut",
        "overlays": {},
        "needs_avatar": False,
    }


@needs_ffmpeg
class TestSceneRenderEndToEnd:
    """A reporter-only chapter needs no studio and no avatar, so it can run
    through the real renderer with nothing but synthetic media."""

    def _prepare(self, db_session, settings) -> Path:
        _episode(db_session)
        base = _write_inputs(Path(settings.outputs_dir))
        subprocess.run(
            [FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=green:s=320x180",
             "-frames:v", "1", str(base / "images" / "ch01.png")],
            check=True, timeout=120,
        )
        subprocess.run(
            [FFMPEG, "-v", "error", "-y", "-f", "lavfi",
             "-i", "sine=frequency=440:duration=2.0", "-ar", "44100", "-ac", "1",
             str(base / "tts" / "ch01.mp3")],
            check=True, timeout=120,
        )
        tts = json.loads((base / "tts" / "manifest.json").read_text(encoding="utf-8"))
        tts["segments"][0]["duration_seconds"] = 2.0
        tts["segments"][0]["metadata"] = {
            "speaker_parts": [
                {"file": "p0.mp3", "duration_seconds": 1.2, "role": "reporter_male"},
                {"file": "p1.mp3", "duration_seconds": 0.8, "role": "reporter_male"},
            ]
        }
        (base / "tts" / "manifest.json").write_text(json.dumps(tts), encoding="utf-8")

        plan = {
            "schema_version": "1.0",
            "episode_id": "ep001",
            "content_hash": "plan-77",
            "presenter_look_id": "look-A",
            "scenes": [
                _reporter_scene("sc_001", 1, [0]),
                _reporter_scene("sc_002", 2, [1]),
            ],
        }
        (base / "scene_plan.json").write_text(json.dumps(plan), encoding="utf-8")
        settings.anchor_enabled = True
        settings.dry_run = False
        return base

    def test_the_chapter_is_cut_into_scenes(self, db_session, settings):
        self._prepare(db_session, settings)

        result = render_video(db_session, "ep001", settings)
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

        assert [s["scene_id"] for s in manifest["scenes"]] == ["sc_001", "sc_002"]
        assert manifest["scene_plan_hash"] == "plan-77"
        assert all(s["compositing_mode"] == "fullscreen_media" for s in manifest["scenes"])
        assert all(s["avatar_clip"] == "" for s in manifest["scenes"])

    def test_the_old_manifest_keys_still_describe_the_episode(self, db_session, settings):
        self._prepare(db_session, settings)

        result = render_video(db_session, "ep001", settings)
        manifest = json.loads(result.manifest_path.read_text(encoding="utf-8"))

        # One segment and one timeline entry per *chapter*: the scenes cut
        # inside it, and a YouTube chapter mark must not become a speaker mark.
        assert len(manifest["segments"]) == 1
        chapter_marks = [
            p for p in manifest["timeline"]
            if p.get("chapter_id") == "ch01" and p.get("kind") == "chapter"
        ]
        assert len(chapter_marks) == 1
        assert manifest["segments"][0]["chapter_id"] == "ch01"

    def test_the_scene_starts_are_recorded_against_the_episode(self, db_session, settings):
        self._prepare(db_session, settings)

        result = render_video(db_session, "ep001", settings)
        scenes = json.loads(result.manifest_path.read_text(encoding="utf-8"))["scenes"]

        assert scenes[0]["start_seconds_in_chapter"] == 0.0
        assert scenes[1]["start_seconds_in_chapter"] > 0.0
        assert scenes[0]["audio_source"] == "tts/ch01.mp3"

    def test_a_second_render_reuses_every_scene(self, db_session, settings):
        self._prepare(db_session, settings)
        render_video(db_session, "ep001", settings)

        result = render_video(db_session, "ep001", settings, force=True)
        scenes = json.loads(result.manifest_path.read_text(encoding="utf-8"))["scenes"]

        assert all(s["reused"] is True for s in scenes)
