"""Cutting a chapter into scenes, with the audio left strictly alone.

The promises being tested divide into three kinds. The arithmetic ones -- the
scene durations add up to the chapter, the rounding lands on the last scene --
are pure and cheap. The refusal ones -- a wrong look, a missing clip, a studio
that is not ready -- must fail closed and are also pure. Only the third kind,
"the finished chapter really does carry the original MP3 and nothing else",
can be settled by encoding a few frames, so those tests do exactly that with
synthetic assets.

No provider is contacted, nothing costs anything, and no real studio artwork is
required or invented.
"""

import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

from btcedu.config import Settings
from btcedu.core.scene_planner import (
    ROLE_ANCHOR,
    ROLE_REPORTER,
    TEMPLATE_ANCHOR,
    TEMPLATE_ANCHOR_RETURN,
    TEMPLATE_CLOSING,
    TEMPLATE_OPENING,
    TEMPLATE_REPORTER,
    TEMPLATE_WEATHER,
    VISUAL_MODE_FULLSCREEN,
    VISUAL_MODE_STUDIO,
    Scene,
)
from btcedu.core.scene_renderer import (
    SCENE_RENDERER_VERSION,
    SceneRenderContext,
    SceneRenderError,
    is_studio_scene,
    load_scene_context,
    render_scene_chapter,
    require_studio,
    resolve_avatar_clip,
    scene_durations,
    scene_hash_inputs,
    scene_manifest_block,
    template_background,
)
from btcedu.core.studio_manifest import load_studio_manifest, studio_content_hash

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")

WIDTH, HEIGHT, FPS = 320, 180, 25


@dataclass
class FakeChapter:
    chapter_id: str = "ch_01"
    order: int = 1


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"fixture command failed: {' '.join(cmd)}\n{result.stderr[-600:]}")


def _probe(path) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
         str(path)],
        capture_output=True, text=True, timeout=120, check=True,
    )
    return json.loads(result.stdout)


def _scene(
    scene_id: str,
    *,
    order: int,
    role: str = ROLE_ANCHOR,
    template: str = TEMPLATE_ANCHOR,
    mode: str = VISUAL_MODE_STUDIO,
    chapter_id: str = "ch_01",
    beat_index: int = 0,
    indices: list[int] | None = None,
    background: str | None = "images/topic.png",
    audio_file: str | None = None,
    duration: float = 1.0,
) -> Scene:
    return Scene(
        scene_id=scene_id,
        chapter_id=chapter_id,
        order=order,
        beat_index=beat_index,
        speaker_role=role,
        purpose="news",
        segment_indices=indices if indices is not None else [order - 1],
        text_hash=f"hash-{scene_id}",
        audio_file=audio_file,
        expected_duration_seconds=duration,
        visual_mode=mode,
        template_id=template,
        background_asset=background,
        background_asset_type="image",
        display_zone_id="main_wall" if mode == VISUAL_MODE_STUDIO else None,
        display_fit_mode="cover",
        focus_point=[0.5, 0.5],
        transition_in="cut",
        overlays={},
        needs_avatar=mode == VISUAL_MODE_STUDIO,
    )


def _studio_data(**overrides) -> dict:
    data = {
        "schema_version": 1,
        "studio_version": "1.0.0",
        "asset_version": "1.0.0",
        "name": "Synthetic Studio",
        "width": WIDTH,
        "height": HEIGHT,
        "fps": FPS,
        "alpha_mode": "alpha_webm",
        "background": {"path": "plate/bg.png", "kind": "image"},
        "intro_asset": {"path": "plate/wide.png", "kind": "image"},
        "loop_asset": {"path": "plate/loop.png", "kind": "image"},
        "display_zone": {
            "zone_id": "main_wall",
            "rect": {"x": 160, "y": 10, "width": 140, "height": 79},
            "fit_mode": "cover",
            "focus_point": [0.5, 0.5],
            "presenter_free": True,
        },
        "presenter": {"anchor_x": 70, "anchor_y": 180, "scale": 1.0},
        "fallback_display_media": {"path": "fallback/neutral.png", "kind": "image"},
        "logo_zone": {"x": 260, "y": 5, "width": 50, "height": 20},
        "safe_areas": {
            "lower_third": {"x": 20, "y": 130, "width": 200, "height": 22},
            "ticker": {"x": 0, "y": 160, "width": 320, "height": 20},
            "subtitle": {"x": 40, "y": 152, "width": 240, "height": 8},
        },
    }
    data.update(overrides)
    return data


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    """One synthetic episode plus one synthetic studio, built once."""
    if FFMPEG is None:
        pytest.skip("ffmpeg is not installed")

    root = tmp_path_factory.mktemp("scene_world")
    studio = root / "studio"
    (studio / "plate").mkdir(parents=True)
    (studio / "fallback").mkdir(parents=True)
    episode = root / "episode"
    (episode / "images").mkdir(parents=True)
    (episode / "anchor" / "clips").mkdir(parents=True)
    (episode / "tts").mkdir(parents=True)
    (episode / "video").mkdir(parents=True)

    for name, colour in (("bg", "navy"), ("wide", "teal"), ("loop", "purple")):
        _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
              "-i", f"color=c={colour}:s={WIDTH}x{HEIGHT}", "-frames:v", "1",
              str(studio / "plate" / f"{name}.png")])
    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=200x100",
          "-frames:v", "1", str(studio / "fallback" / "neutral.png")])

    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=green:s=400x200",
          "-frames:v", "1", str(episode / "images" / "topic.png")])
    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=maroon:s=400x200",
          "-frames:v", "1", str(episode / "images" / "topic_b.png")])
    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
          "-i", "testsrc=size=200x100:rate=25:duration=2", "-c:v", "libx264",
          "-pix_fmt", "yuv420p", str(episode / "video" / "clip.mp4")])

    # A transparent presenter clip. VP9 alpha cannot be *encoded* on this
    # machine, so the fixture uses QuickTime RLE, which carries a real alpha
    # channel and is what the probe is actually looking for.
    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
          "-i", "color=c=red:s=64x120,format=rgba,colorchannelmixer=aa=0.8",
          "-frames:v", "1", str(root / "presenter.png")])
    for name in ("sc_001", "sc_003", "sc_op", "sc_cl", "sc_wx"):
        _run([FFMPEG, "-v", "error", "-y", "-loop", "1", "-i", str(root / "presenter.png"),
              "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "2.0", "-r", str(FPS),
              "-c:v", "qtrle", "-pix_fmt", "argb", "-c:a", "aac", "-shortest",
              str(episode / "anchor" / "clips" / f"{name}.mov")])
    _run([FFMPEG, "-v", "error", "-y", "-loop", "1", "-i", str(root / "presenter.png"),
          "-t", "2.0", "-r", str(FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
          str(episode / "anchor" / "clips" / "opaque.mp4")])

    # The chapter's finished narration, and the two speaker parts inside it.
    # A different tone per part, so "the right part went to the right block"
    # is a measurable statement rather than a hopeful one.
    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
          "-i", "sine=frequency=300:duration=1.2", "-ar", "44100", "-ac", "1",
          str(episode / "tts" / "ch_01_p0.mp3")])
    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
          "-i", "sine=frequency=700:duration=0.8", "-ar", "44100", "-ac", "1",
          str(episode / "tts" / "ch_01_p1.mp3")])
    # The chapter MP3 is the two parts plus the pause that already sits between
    # them: 1.2 + 0.4 + 0.8 = 2.4s. Nothing may add that pause a second time.
    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
          "-i", "sine=frequency=300:duration=1.2", "-f", "lavfi",
          "-i", "anullsrc=r=44100:cl=mono:d=0.4", "-f", "lavfi",
          "-i", "sine=frequency=700:duration=0.8",
          "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]",
          "-map", "[out]", "-ar", "44100", "-ac", "1",
          str(episode / "tts" / "ch_01.mp3")])

    return {"root": root, "studio": studio, "episode": episode}


CHAPTER_DURATION = 2.4


def _write_studio(studio_dir: Path, **overrides):
    path = studio_dir / "manifest.json"
    path.write_text(json.dumps(_studio_data(**overrides)), encoding="utf-8")
    return load_studio_manifest(path)


def _parts() -> list[dict]:
    # The shape TTS actually records: a bare file name under tts/parts/ plus
    # the duration it was measured at.
    return [
        {"file": "ch_01_p0.mp3", "duration_seconds": 1.2, "role": ROLE_ANCHOR},
        {"file": "ch_01_p1.mp3", "duration_seconds": 0.8, "role": ROLE_REPORTER},
    ]


def _clip_entry(scene_id: str, *, look: str = "look-A", status: str = "completed") -> dict:
    return {
        "scene_id": scene_id,
        "chapter_id": "ch_01",
        "speaker_role": ROLE_ANCHOR,
        "avatar_look_id": look,
        "status": status,
        "video_path": f"anchor/clips/{scene_id}.mov",
        "content_hash": f"clip-hash-{scene_id}",
        "duration_seconds": 2.0,
    }


def _context(world, scenes, *, clips=None, studio=None, look="look-A"):
    manifest = studio if studio is not None else _write_studio(world["studio"])
    return SceneRenderContext(
        base_dir=world["episode"],
        plan={"content_hash": "plan-1", "presenter_look_id": look},
        scenes=scenes,
        plan_hash="plan-1",
        presenter_look_id=look,
        anchor_clips={c["scene_id"]: c for c in (clips or [])},
        anchor_look_id=look,
        anchor_schema_version="2.0",
        studio=manifest,
        studio_hash=studio_content_hash(manifest) if manifest else "",
        studio_problems=[],
        manifests={
            "image_manifest": {
                "images": [
                    {
                        "chapter_id": "ch_01",
                        "file_path": "images/topic.png",
                        "metadata": {"beat_index": 0},
                    }
                ]
            }
        },
    )


def _settings(tmp_path) -> Settings:
    return Settings(
        outputs_dir=str(tmp_path / "outputs"),
        render_resolution=f"{WIDTH}x{HEIGHT}",
        render_fps=FPS,
        render_crf=35,
        render_preset="ultrafast",
        render_audio_bitrate="96k",
        render_timeout_segment=600,
        dry_run=False,
    )


class TestSceneDurations:
    """The arithmetic that keeps picture and sound the same length."""

    def test_the_shares_add_up_to_the_chapter_exactly(self):
        scenes = [_scene("a", order=1, indices=[0]), _scene("b", order=2, indices=[1])]

        durations = scene_durations(scenes, _parts(), CHAPTER_DURATION)

        assert sum(durations) == pytest.approx(CHAPTER_DURATION, abs=1e-9)

    def test_the_measured_parts_set_the_weights(self):
        scenes = [_scene("a", order=1, indices=[0]), _scene("b", order=2, indices=[1])]

        first, second = scene_durations(scenes, _parts(), CHAPTER_DURATION)

        # 1.2 : 0.8 of a 2.4s chapter, so the pause is shared in proportion
        # rather than inserted again.
        assert first == pytest.approx(1.44, abs=0.01)
        assert second == pytest.approx(0.96, abs=0.01)

    def test_the_rounding_difference_lands_on_the_last_scene(self):
        scenes = [_scene(f"s{i}", order=i + 1, indices=[]) for i in range(3)]
        parts = [{"duration_seconds": 1.0} for _ in range(3)]

        durations = scene_durations(scenes, parts, 10.0)

        assert sum(durations) == pytest.approx(10.0, abs=1e-9)
        assert durations[0] == durations[1]

    def test_it_is_deterministic(self):
        scenes = [_scene("a", order=1, indices=[0]), _scene("b", order=2, indices=[1])]

        assert scene_durations(scenes, _parts(), CHAPTER_DURATION) == scene_durations(
            scenes, _parts(), CHAPTER_DURATION
        )

    def test_planned_durations_carry_an_episode_without_measured_parts(self):
        scenes = [
            _scene("a", order=1, indices=[], duration=3.0),
            _scene("b", order=2, indices=[], duration=1.0),
        ]

        first, second = scene_durations(scenes, [], 8.0)

        assert first == pytest.approx(6.0, abs=0.01)
        assert second == pytest.approx(2.0, abs=0.01)

    def test_no_scenes_means_no_durations(self):
        assert scene_durations([], _parts(), 5.0) == []


class TestSceneClassification:
    def test_anchor_templates_are_studio_scenes(self):
        for template in (
            TEMPLATE_OPENING,
            TEMPLATE_ANCHOR,
            TEMPLATE_ANCHOR_RETURN,
            TEMPLATE_WEATHER,
            TEMPLATE_CLOSING,
        ):
            scene = _scene("s", order=1, template=template)
            assert is_studio_scene(scene) is True

    def test_a_reporter_scene_is_never_a_studio_scene(self):
        scene = _scene(
            "s",
            order=1,
            role=ROLE_REPORTER,
            template=TEMPLATE_REPORTER,
            mode=VISUAL_MODE_FULLSCREEN,
        )

        assert is_studio_scene(scene) is False

    def test_opening_and_closing_stand_in_front_of_their_own_plates(self, world):
        studio = _write_studio(world["studio"])

        opening = template_background(studio, TEMPLATE_OPENING)
        closing = template_background(studio, TEMPLATE_CLOSING)
        medium = template_background(studio, TEMPLATE_ANCHOR)

        assert opening is not None and opening.endswith("wide.png")
        assert closing is not None and closing.endswith("loop.png")
        # The ordinary medium shot uses the manifest's own background.
        assert medium is None

    def test_the_weather_handover_stays_in_the_studio(self, world):
        studio = _write_studio(world["studio"])

        assert template_background(studio, TEMPLATE_WEATHER).endswith("loop.png")


class TestAvatarClipResolution:
    """Rendering is never a reason to buy anything."""

    def test_a_finished_clip_resolves(self, world):
        ctx = _context(world, [_scene("sc_001", order=1)], clips=[_clip_entry("sc_001")])

        path, look = resolve_avatar_clip(ctx, ctx.scenes[0])

        assert path.name == "sc_001.mov"
        assert look == "look-A"

    def test_a_missing_manifest_entry_fails_closed(self, world):
        ctx = _context(world, [_scene("sc_001", order=1)], clips=[])

        with pytest.raises(SceneRenderError, match="never orders one"):
            resolve_avatar_clip(ctx, ctx.scenes[0])

    @pytest.mark.parametrize("status", ["failed", "reserved", "reconcile_required", ""])
    def test_an_unfinished_job_is_not_a_clip(self, world, status):
        ctx = _context(
            world,
            [_scene("sc_001", order=1)],
            clips=[_clip_entry("sc_001", status=status)],
        )

        with pytest.raises(SceneRenderError, match="not a finished clip"):
            resolve_avatar_clip(ctx, ctx.scenes[0])

    def test_a_foreign_look_id_fails_closed(self, world):
        ctx = _context(
            world,
            [_scene("sc_001", order=1)],
            clips=[_clip_entry("sc_001", look="look-B")],
            look="look-A",
        )

        with pytest.raises(SceneRenderError, match="One outfit per episode"):
            resolve_avatar_clip(ctx, ctx.scenes[0])

    def test_a_missing_clip_file_fails_closed(self, world):
        entry = _clip_entry("sc_001")
        entry["video_path"] = "anchor/clips/never_downloaded.mov"
        ctx = _context(world, [_scene("sc_001", order=1)], clips=[entry])

        with pytest.raises(SceneRenderError, match="is missing"):
            resolve_avatar_clip(ctx, ctx.scenes[0])

    def test_a_clip_path_leaving_the_episode_is_refused(self, world):
        entry = _clip_entry("sc_001")
        entry["video_path"] = "../../../etc/hosts"
        ctx = _context(world, [_scene("sc_001", order=1)], clips=[entry])

        with pytest.raises(SceneRenderError, match="escapes|missing"):
            resolve_avatar_clip(ctx, ctx.scenes[0])

    def test_all_anchor_scenes_share_one_look(self, world):
        ctx = _context(
            world,
            [_scene("sc_001", order=1), _scene("sc_003", order=3)],
            clips=[_clip_entry("sc_001"), _clip_entry("sc_003")],
        )

        looks = {resolve_avatar_clip(ctx, s)[1] for s in ctx.scenes}

        assert looks == {"look-A"}


class TestStudioReadiness:
    def test_a_missing_studio_fails_closed(self, world):
        ctx = _context(world, [_scene("sc_001", order=1)])
        ctx.studio = None
        ctx.studio_problems = ["Studio manifest not found"]

        with pytest.raises(SceneRenderError, match="no usable studio manifest"):
            require_studio(ctx)

    def test_an_unready_studio_fails_closed(self, world):
        ctx = _context(world, [_scene("sc_001", order=1)])
        ctx.studio_problems = ["plate/bg.png is missing"]

        with pytest.raises(SceneRenderError, match="not ready"):
            require_studio(ctx)

    def test_a_ready_studio_is_returned(self, world):
        ctx = _context(world, [_scene("sc_001", order=1)])

        assert require_studio(ctx).studio_version == "1.0.0"


class TestHashInputs:
    """Which change invalidates which render."""

    def _hash(self, world, **kwargs):
        ctx = _context(world, **kwargs)
        return json.dumps(scene_hash_inputs(ctx), sort_keys=True, default=str)

    def test_no_plan_contributes_nothing(self):
        assert scene_hash_inputs(None) is None

    def test_the_same_inputs_hash_the_same(self, world):
        scenes = [_scene("sc_001", order=1)]
        assert self._hash(world, scenes=scenes, clips=[_clip_entry("sc_001")]) == self._hash(
            world, scenes=scenes, clips=[_clip_entry("sc_001")]
        )

    def test_a_studio_change_changes_the_render_inputs(self, world):
        scenes = [_scene("sc_001", order=1)]
        before = self._hash(world, scenes=scenes, clips=[_clip_entry("sc_001")])
        repainted = _write_studio(world["studio"], studio_version="2.0.0")

        after = self._hash(
            world, scenes=scenes, clips=[_clip_entry("sc_001")], studio=repainted
        )
        _write_studio(world["studio"])

        assert before != after

    def test_a_new_avatar_clip_changes_the_render_inputs(self, world):
        scenes = [_scene("sc_001", order=1)]
        before = self._hash(world, scenes=scenes, clips=[_clip_entry("sc_001")])
        changed = _clip_entry("sc_001")
        changed["content_hash"] = "clip-hash-different"

        assert before != self._hash(world, scenes=scenes, clips=[changed])

    def test_a_reporter_media_change_changes_only_the_render(self, world):
        reporter = _scene(
            "sc_002",
            order=2,
            role=ROLE_REPORTER,
            template=TEMPLATE_REPORTER,
            mode=VISUAL_MODE_FULLSCREEN,
        )
        before = self._hash(world, scenes=[_scene("sc_001", order=1), reporter])
        moved = _scene(
            "sc_002",
            order=2,
            role=ROLE_REPORTER,
            template=TEMPLATE_REPORTER,
            mode=VISUAL_MODE_FULLSCREEN,
            background="images/topic_b.png",
        )

        after = self._hash(world, scenes=[_scene("sc_001", order=1), moved])

        assert before != after
        # And it names no avatar job at all, so nothing can be re-bought.
        assert "avatar" not in moved.template_id

    def test_the_renderer_version_is_part_of_the_fingerprint(self, world):
        payload = scene_hash_inputs(_context(world, [_scene("sc_001", order=1)]))

        assert payload["renderer_version"] == SCENE_RENDERER_VERSION


class TestSceneManifestBlock:
    def test_the_block_carries_the_studio_and_plan_identity(self, world):
        ctx = _context(world, [_scene("sc_001", order=1)])

        block = scene_manifest_block(ctx, [])

        assert block["scene_plan_hash"] == "plan-1"
        assert block["presenter_look_id"] == "look-A"
        assert block["studio_version"] == "1.0.0"
        assert block["scene_renderer_version"] == SCENE_RENDERER_VERSION
        assert block["scenes"] == []


@needs_ffmpeg
class TestRenderedChapters:
    """What the file on disk actually contains."""

    def _render(self, world, tmp_path, scenes, clips, *, studio=None):
        ctx = _context(world, scenes, clips=clips, studio=studio)
        out = tmp_path / "segments" / "ch_01.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)
        result, entries = render_scene_chapter(
            ctx=ctx,
            chapter=FakeChapter(),
            scenes=scenes,
            parts=_parts(),
            audio_path=world["episode"] / "tts" / "ch_01.mp3",
            output_path=out,
            duration=CHAPTER_DURATION,
            overlays=[],
            fade_in_duration=0.0,
            fade_out_duration=0.0,
            settings=_settings(tmp_path),
            font="DejaVuSans-Bold",
            enhancement_kwargs=lambda kind, index: {},
            episode_offset_seconds=12.5,
        )
        return out, result, entries

    def _anchor_reporter_anchor(self):
        return [
            _scene("sc_001", order=1, indices=[0]),
            _scene(
                "sc_002",
                order=2,
                role=ROLE_REPORTER,
                template=TEMPLATE_REPORTER,
                mode=VISUAL_MODE_FULLSCREEN,
                indices=[1],
            ),
            _scene("sc_003", order=3, indices=[]),
        ]

    def test_anchor_reporter_anchor_renders_in_plan_order(self, world, tmp_path):
        scenes = self._anchor_reporter_anchor()
        _, _, entries = self._render(
            world, tmp_path, scenes, [_clip_entry("sc_001"), _clip_entry("sc_003")]
        )

        assert [e.scene_id for e in entries] == ["sc_001", "sc_002", "sc_003"]
        assert [e.speaker_role for e in entries] == [ROLE_ANCHOR, ROLE_REPORTER, ROLE_ANCHOR]

    def test_the_reporter_scene_uses_no_avatar_and_no_studio(self, world, tmp_path):
        scenes = self._anchor_reporter_anchor()
        _, _, entries = self._render(
            world, tmp_path, scenes, [_clip_entry("sc_001"), _clip_entry("sc_003")]
        )
        reporter = next(e for e in entries if e.speaker_role == ROLE_REPORTER)

        assert reporter.avatar_clip == ""
        assert reporter.avatar_look_id == ""
        assert reporter.studio_content_hash == ""
        assert reporter.compositing_mode == "fullscreen_media"

    def test_the_anchor_scenes_are_composited_into_the_studio(self, world, tmp_path):
        scenes = self._anchor_reporter_anchor()
        _, _, entries = self._render(
            world, tmp_path, scenes, [_clip_entry("sc_001"), _clip_entry("sc_003")]
        )
        anchors = [e for e in entries if e.speaker_role == ROLE_ANCHOR]

        assert all(e.compositing_mode == "alpha_webm" for e in anchors)
        assert all(e.avatar_clip.startswith("anchor/clips/") for e in anchors)
        assert all(e.studio_version == "1.0.0" for e in anchors)

    def test_the_monitor_and_the_reporter_show_the_same_asset(self, world, tmp_path):
        scenes = self._anchor_reporter_anchor()
        _, _, entries = self._render(
            world, tmp_path, scenes, [_clip_entry("sc_001"), _clip_entry("sc_003")]
        )

        assert {e.display_media for e in entries} == {"images/topic.png"}
        assert len({e.display_media_hash for e in entries}) == 1

    def test_the_chapter_mp3_is_the_only_audio_track(self, world, tmp_path):
        scenes = self._anchor_reporter_anchor()
        out, _, entries = self._render(
            world, tmp_path, scenes, [_clip_entry("sc_001"), _clip_entry("sc_003")]
        )
        streams = _probe(out)["streams"]

        assert len([s for s in streams if s["codec_type"] == "audio"]) == 1
        assert all(e.audio_source == "tts/ch_01.mp3" for e in entries)

    def test_the_final_duration_is_the_narrations(self, world, tmp_path):
        scenes = self._anchor_reporter_anchor()
        out, _, _ = self._render(
            world, tmp_path, scenes, [_clip_entry("sc_001"), _clip_entry("sc_003")]
        )

        duration = float(_probe(out)["format"]["duration"])

        # Within a frame or two of the narration: the picture carries a little
        # tail headroom so the last word cannot be clipped, never less.
        assert duration >= CHAPTER_DURATION - 0.05
        assert duration <= CHAPTER_DURATION + 0.4

    def test_no_heygen_audio_survives(self, world, tmp_path):
        """The avatar clips carry their own track; the output must not."""
        scenes = self._anchor_reporter_anchor()
        out, _, _ = self._render(
            world, tmp_path, scenes, [_clip_entry("sc_001"), _clip_entry("sc_003")]
        )

        source = _probe(world["episode"] / "anchor" / "clips" / "sc_001.mov")["streams"]
        assert any(s["codec_type"] == "audio" for s in source)
        # One track out, and it is as long as the chapter narration -- there is
        # simply no room for a second voice.
        audio = [s for s in _probe(out)["streams"] if s["codec_type"] == "audio"]
        assert len(audio) == 1

    def test_the_scene_starts_are_recorded_in_chapter_and_episode_time(self, world, tmp_path):
        scenes = self._anchor_reporter_anchor()
        _, _, entries = self._render(
            world, tmp_path, scenes, [_clip_entry("sc_001"), _clip_entry("sc_003")]
        )

        assert entries[0].start_seconds_in_chapter == 0.0
        assert entries[0].start_seconds_in_episode == 12.5
        assert entries[1].start_seconds_in_chapter == pytest.approx(
            entries[0].duration_seconds, abs=0.01
        )
        assert entries[1].start_seconds_in_episode == pytest.approx(
            12.5 + entries[0].duration_seconds, abs=0.01
        )

    def test_the_right_speaker_part_belongs_to_the_right_scene(self, world, tmp_path):
        scenes = self._anchor_reporter_anchor()
        _, _, entries = self._render(
            world, tmp_path, scenes, [_clip_entry("sc_001"), _clip_entry("sc_003")]
        )

        assert entries[0].audio_parts == ["tts/parts/ch_01_p0.mp3"]
        assert entries[1].audio_parts == ["tts/parts/ch_01_p1.mp3"]
        # The third scene spans no part of its own and claims none.
        assert entries[2].audio_parts == []

    def test_a_chapter_of_only_reporter_scenes_renders(self, world, tmp_path):
        scenes = [
            _scene(
                f"sc_r{i}",
                order=i + 1,
                role=ROLE_REPORTER,
                template=TEMPLATE_REPORTER,
                mode=VISUAL_MODE_FULLSCREEN,
                indices=[i],
            )
            for i in range(2)
        ]

        out, _, entries = self._render(world, tmp_path, scenes, [])

        assert out.exists()
        assert all(e.avatar_clip == "" for e in entries)

    def test_a_chapter_of_only_anchor_scenes_renders(self, world, tmp_path):
        scenes = [_scene("sc_001", order=1, indices=[0]), _scene("sc_003", order=2, indices=[1])]

        out, _, entries = self._render(
            world, tmp_path, scenes, [_clip_entry("sc_001"), _clip_entry("sc_003")]
        )

        assert out.exists()
        assert all(e.avatar_clip for e in entries)

    def test_opening_and_closing_use_their_studio_templates(self, world, tmp_path):
        scenes = [
            _scene("sc_op", order=1, template=TEMPLATE_OPENING, indices=[0]),
            _scene("sc_cl", order=2, template=TEMPLATE_CLOSING, indices=[1]),
        ]

        _, _, entries = self._render(
            world, tmp_path, scenes, [_clip_entry("sc_op"), _clip_entry("sc_cl")]
        )

        assert [e.template_id for e in entries] == [TEMPLATE_OPENING, TEMPLATE_CLOSING]
        assert all(e.compositing_mode == "alpha_webm" for e in entries)

    def test_the_weather_handover_is_a_studio_scene(self, world, tmp_path):
        scenes = [
            _scene("sc_wx", order=1, template=TEMPLATE_WEATHER, indices=[0]),
            _scene(
                "sc_002",
                order=2,
                role=ROLE_REPORTER,
                template=TEMPLATE_REPORTER,
                mode=VISUAL_MODE_FULLSCREEN,
                indices=[1],
            ),
        ]

        _, _, entries = self._render(world, tmp_path, scenes, [_clip_entry("sc_wx")])

        assert entries[0].compositing_mode == "alpha_webm"
        assert entries[1].compositing_mode == "fullscreen_media"

    def test_an_unchanged_scene_is_reused(self, world, tmp_path):
        scenes = [_scene("sc_001", order=1, indices=[0]), _scene("sc_003", order=2, indices=[1])]
        clips = [_clip_entry("sc_001"), _clip_entry("sc_003")]

        _, _, first = self._render(world, tmp_path, scenes, clips)
        _, _, second = self._render(world, tmp_path, scenes, clips)

        assert [e.reused for e in first] == [False, False]
        assert [e.reused for e in second] == [True, True]

    def test_a_changed_scene_is_rendered_again_on_its_own(self, world, tmp_path):
        scenes = [_scene("sc_001", order=1, indices=[0]), _scene("sc_003", order=2, indices=[1])]
        clips = [_clip_entry("sc_001"), _clip_entry("sc_003")]
        self._render(world, tmp_path, scenes, clips)

        changed = list(clips)
        changed[1] = _clip_entry("sc_003")
        changed[1]["content_hash"] = "clip-hash-new"

        _, _, entries = self._render(world, tmp_path, scenes, changed)

        assert entries[0].reused is True
        assert entries[1].reused is False

    def test_a_corrupted_shot_is_rendered_again(self, world, tmp_path):
        scenes = [_scene("sc_001", order=1, indices=[0]), _scene("sc_003", order=2, indices=[1])]
        clips = [_clip_entry("sc_001"), _clip_entry("sc_003")]
        out, _, _ = self._render(world, tmp_path, scenes, clips)
        broken = out.parent / "scenes" / "ch_01_sc_003.mp4"
        broken.write_bytes(b"not a video")

        _, _, entries = self._render(world, tmp_path, scenes, clips)

        assert entries[0].reused is True
        assert entries[1].reused is False

    def test_the_lower_third_is_drawn_once(self, world, tmp_path):
        """A speaker change is a cut, not a reason to repeat the caption."""
        from btcedu.services.ffmpeg_service import OverlaySpec

        recorded: list[int] = []
        real_segment = None

        scenes = self._anchor_reporter_anchor()
        ctx = _context(world, scenes, clips=[_clip_entry("sc_001"), _clip_entry("sc_003")])
        out = tmp_path / "segments" / "ch_01.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)

        import btcedu.services.ffmpeg_service as ffmpeg_service

        real_segment = ffmpeg_service.create_video_segment

        def spy(*args, **kwargs):
            recorded.append(len(kwargs.get("overlays") or []))
            return real_segment(*args, **kwargs)

        ffmpeg_service.create_video_segment = spy
        try:
            render_scene_chapter(
                ctx=ctx,
                chapter=FakeChapter(),
                scenes=scenes,
                parts=_parts(),
                audio_path=world["episode"] / "tts" / "ch_01.mp3",
                output_path=out,
                duration=CHAPTER_DURATION,
                overlays=[
                    OverlaySpec(
                        text="Headline",
                        overlay_type="lower_third",
                        fontsize=18,
                        fontcolor="white",
                        font="DejaVuSans-Bold",
                        position="lower_third_headline",
                        start=0.0,
                        end=3.0,
                    )
                ],
                fade_in_duration=0.0,
                fade_out_duration=0.0,
                settings=_settings(tmp_path),
                font="DejaVuSans-Bold",
                enhancement_kwargs=lambda kind, index: {},
            )
        finally:
            ffmpeg_service.create_video_segment = real_segment

        # Exactly one shot was given the caption, and it was the first.
        assert recorded.count(1) == 1
        assert recorded[0] == 1

    def test_the_opaque_fallback_renders_from_an_mp4(self, world, tmp_path):
        studio = _write_studio(world["studio"], alpha_mode="opaque_mp4")
        entry = _clip_entry("sc_001")
        entry["video_path"] = "anchor/clips/opaque.mp4"
        scenes = [_scene("sc_001", order=1, indices=[0])]

        out, _, entries = self._render(world, tmp_path, scenes, [entry], studio=studio)
        _write_studio(world["studio"])

        assert out.exists()
        assert entries[0].compositing_mode == "opaque_mp4"

    def test_an_opaque_clip_in_alpha_mode_is_refused(self, world, tmp_path):
        entry = _clip_entry("sc_001")
        entry["video_path"] = "anchor/clips/opaque.mp4"
        scenes = [_scene("sc_001", order=1, indices=[0])]

        with pytest.raises(SceneRenderError, match="composited|alpha"):
            self._render(world, tmp_path, scenes, [entry])

    def test_a_missing_scene_asset_is_rescued_by_the_chapter_manifest(self, world, tmp_path):
        """A stale path in the plan must not cost the story its picture."""
        scenes = [_scene("sc_001", order=1, indices=[0], background="images/gone.png")]

        _, _, entries = self._render(world, tmp_path, scenes, [_clip_entry("sc_001")])

        assert entries[0].used_fallback_media is False
        assert entries[0].display_media == "images/topic.png"

    def test_without_any_topic_medium_the_neutral_studio_card_is_used(self, world, tmp_path):
        scenes = [_scene("sc_001", order=1, indices=[0], background="images/gone.png")]
        ctx = _context(world, scenes, clips=[_clip_entry("sc_001")])
        ctx.manifests = {}
        out = tmp_path / "segments" / "ch_01.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)

        _, entries = render_scene_chapter(
            ctx=ctx,
            chapter=FakeChapter(),
            scenes=scenes,
            parts=_parts(),
            audio_path=world["episode"] / "tts" / "ch_01.mp3",
            output_path=out,
            duration=CHAPTER_DURATION,
            overlays=[],
            fade_in_duration=0.0,
            fade_out_duration=0.0,
            settings=_settings(tmp_path),
            font="DejaVuSans-Bold",
            enhancement_kwargs=lambda kind, index: {},
        )

        assert entries[0].used_fallback_media is True
        assert entries[0].display_media.endswith("neutral.png")

    def test_without_a_fallback_card_the_scene_fails_closed(self, world, tmp_path):
        studio = _write_studio(world["studio"], fallback_display_media=None)
        scenes = [_scene("sc_001", order=1, indices=[0], background="images/gone.png")]
        ctx = _context(world, scenes, clips=[_clip_entry("sc_001")], studio=studio)
        ctx.manifests = {}
        out = tmp_path / "segments" / "ch_01.mp4"
        out.parent.mkdir(parents=True, exist_ok=True)

        with pytest.raises(SceneRenderError, match="no topic medium"):
            render_scene_chapter(
                ctx=ctx,
                chapter=FakeChapter(),
                scenes=scenes,
                parts=_parts(),
                audio_path=world["episode"] / "tts" / "ch_01.mp3",
                output_path=out,
                duration=CHAPTER_DURATION,
                overlays=[],
                fade_in_duration=0.0,
                fade_out_duration=0.0,
                settings=_settings(tmp_path),
                font="DejaVuSans-Bold",
                enhancement_kwargs=lambda kind, index: {},
            )
        _write_studio(world["studio"])


class TestContextLoading:
    """The branch every older episode takes."""

    def test_no_plan_means_no_context(self, tmp_path):
        episode = tmp_path / "ep"
        episode.mkdir()

        assert load_scene_context(episode, Settings(), None) is None

    def test_an_unreadable_plan_falls_back_to_chapters(self, tmp_path):
        episode = tmp_path / "ep"
        episode.mkdir()
        (episode / "scene_plan.json").write_text("{ not json", encoding="utf-8")

        assert load_scene_context(episode, Settings(), None) is None

    def test_an_empty_plan_means_no_context(self, tmp_path):
        episode = tmp_path / "ep"
        episode.mkdir()
        (episode / "scene_plan.json").write_text(json.dumps({"scenes": []}), encoding="utf-8")

        assert load_scene_context(episode, Settings(), None) is None

    def test_a_plan_without_a_studio_still_loads(self, tmp_path):
        episode = tmp_path / "ep"
        episode.mkdir()
        scene = _scene("sc_001", order=1)
        (episode / "scene_plan.json").write_text(
            json.dumps(
                {
                    "content_hash": "plan-9",
                    "presenter_look_id": "look-A",
                    "scenes": [scene.__dict__],
                }
            ),
            encoding="utf-8",
        )

        ctx = load_scene_context(episode, Settings(), None)

        assert ctx is not None
        assert ctx.plan_hash == "plan-9"
        # No profile, so no studio is even looked for.
        assert ctx.studio is None

    def test_scenes_are_returned_per_chapter_in_order(self, tmp_path):
        episode = tmp_path / "ep"
        episode.mkdir()
        scenes = [
            _scene("sc_003", order=3, chapter_id="ch_02"),
            _scene("sc_002", order=2, chapter_id="ch_01"),
            _scene("sc_001", order=1, chapter_id="ch_01"),
        ]
        (episode / "scene_plan.json").write_text(
            json.dumps({"scenes": [s.__dict__ for s in scenes]}), encoding="utf-8"
        )

        ctx = load_scene_context(episode, Settings(), None)

        assert [s.scene_id for s in ctx.scenes_for("ch_01")] == ["sc_001", "sc_002"]
        assert [s.scene_id for s in ctx.scenes_for("ch_02")] == ["sc_003"]
