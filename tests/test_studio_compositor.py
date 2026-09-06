"""Compositing tests: half graph inspection, half real ffmpeg.

The pure tests read the filter graph, because most compositing mistakes are
visible in the graph long before they are visible on screen. The integration
tests actually encode a few frames with synthetic assets, because the promises
that matter most -- the narration is the audio, the duration is the narration's,
the frame is the studio's -- can only be checked on a real file.

Nothing here contacts a provider or costs anything.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from btcedu.core.studio_manifest import (
    ALPHA_MODE_OPAQUE,
    load_studio_manifest,
)
from btcedu.services.studio_compositor import (
    DisplayMedia,
    StudioCompositeError,
    StudioCompositeRequest,
    _pix_fmt_has_alpha,
    build_composite_command,
    composite_studio_scene,
    probe_duration,
    probe_has_alpha,
)

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(FFMPEG is None, reason="ffmpeg is not installed")

WIDTH, HEIGHT, FPS = 320, 180, 25
NARRATION_SECONDS = 1.6


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


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"fixture command failed: {' '.join(cmd)}\n{result.stderr[-600:]}")


@pytest.fixture(scope="module")
def media(tmp_path_factory):
    """Synthetic assets, built once. Small on purpose: a Pi encodes these."""
    if FFMPEG is None:
        pytest.skip("ffmpeg is not installed")

    root = tmp_path_factory.mktemp("studio_fixtures")
    studio = root / "studio"
    (studio / "plate").mkdir(parents=True)
    (studio / "layers").mkdir(parents=True)
    (studio / "fallback").mkdir(parents=True)
    episode = root / "episode"
    episode.mkdir()

    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", f"color=c=navy:s={WIDTH}x{HEIGHT}",
          "-frames:v", "1", str(studio / "plate" / "bg.png")])
    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
          "-i", f"color=c=black@0.0:s={WIDTH}x{HEIGHT}", "-frames:v", "1",
          "-vf", "format=rgba", str(studio / "layers" / "desk.png")])
    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
          "-i", f"color=c=black@0.0:s={WIDTH}x{HEIGHT}", "-frames:v", "1",
          "-vf", "format=rgba", str(studio / "layers" / "shadow.png")])
    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=gray:s=200x100",
          "-frames:v", "1", str(studio / "fallback" / "neutral.png")])

    # A wide still and a tall still, so cover/contain differ visibly.
    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=green:s=400x100",
          "-frames:v", "1", str(episode / "topic_wide.png")])
    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi", "-i", "color=c=orange:s=100x400",
          "-frames:v", "1", str(episode / "topic_tall.png")])
    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
          "-i", "testsrc=size=200x100:rate=25:duration=1", "-c:v", "libx264",
          "-pix_fmt", "yuv420p", str(episode / "topic_clip.mp4")])

    # The presenter. A transparent clip and an opaque one: the difference is
    # the whole point of the alpha probe.
    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
          "-i", "color=c=red:s=64x120,format=rgba,colorchannelmixer=aa=0.75",
          "-frames:v", "1", str(root / "avatar_frame.png")])
    _run([FFMPEG, "-v", "error", "-y", "-loop", "1", "-i", str(root / "avatar_frame.png"),
          "-t", "1.0", "-r", str(FPS), "-c:v", "qtrle", "-pix_fmt", "argb",
          str(root / "avatar_alpha.mov")])
    _run([FFMPEG, "-v", "error", "-y", "-loop", "1", "-i", str(root / "avatar_frame.png"),
          "-t", "1.0", "-r", str(FPS), "-c:v", "libx264", "-pix_fmt", "yuv420p",
          str(root / "avatar_opaque.mp4")])
    # A presenter clip that carries its own (silent) audio track, so "the
    # avatar's audio is discarded" can be measured rather than asserted.
    _run([FFMPEG, "-v", "error", "-y", "-loop", "1", "-i", str(root / "avatar_frame.png"),
          "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "1.0", "-r", str(FPS),
          "-c:v", "qtrle", "-pix_fmt", "argb", "-c:a", "aac", "-shortest",
          str(root / "avatar_alpha_with_audio.mov")])

    _run([FFMPEG, "-v", "error", "-y", "-f", "lavfi",
          "-i", f"sine=frequency=440:duration={NARRATION_SECONDS}",
          "-ar", "44100", "-ac", "1", str(episode / "narration.mp3")])

    return {
        "root": root,
        "studio": studio,
        "episode": episode,
        "avatar_alpha": str(root / "avatar_alpha.mov"),
        "avatar_alpha_with_audio": str(root / "avatar_alpha_with_audio.mov"),
        "avatar_opaque": str(root / "avatar_opaque.mp4"),
        "narration": str(episode / "narration.mp3"),
    }


def _manifest(studio_dir: Path, **overrides):
    path = studio_dir / "manifest.json"
    path.write_text(json.dumps(_studio_data(**overrides)), encoding="utf-8")
    return load_studio_manifest(path)


def _probe(path: str) -> dict:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format",
         str(path)],
        capture_output=True, text=True, timeout=120, check=True,
    )
    return json.loads(result.stdout)


def _mean_volume(path: str) -> float:
    result = subprocess.run(
        [FFMPEG, "-v", "info", "-i", str(path), "-af", "volumedetect", "-f", "null", "-"],
        capture_output=True, text=True, timeout=300, check=False,
    )
    for line in result.stderr.splitlines():
        if "mean_volume:" in line:
            return float(line.split("mean_volume:")[1].strip().split()[0])
    raise AssertionError(f"volumedetect produced no mean_volume for {path}")


class TestAlphaDetection:
    @pytest.mark.parametrize(
        "pix_fmt,expected",
        [
            ("yuva420p", True),
            ("yuva444p10le", True),
            ("rgba", True),
            ("bgra", True),
            ("argb", True),
            ("ya8", True),
            ("yuv420p", False),
            ("yuv444p", False),
            ("gray", False),
            ("", False),
        ],
    )
    def test_pixel_formats_are_classified(self, pix_fmt, expected):
        assert _pix_fmt_has_alpha(pix_fmt) is expected

    @needs_ffmpeg
    def test_a_transparent_clip_is_recognised(self, media):
        assert probe_has_alpha(media["avatar_alpha"]) is True

    @needs_ffmpeg
    def test_an_opaque_clip_is_not_mistaken_for_a_transparent_one(self, media):
        assert probe_has_alpha(media["avatar_opaque"]) is False

    @needs_ffmpeg
    def test_alpha_mode_refuses_an_opaque_clip(self, media):
        manifest = _manifest(media["studio"])
        request = StudioCompositeRequest(
            manifest=manifest,
            audio_path=media["narration"],
            output_path=str(media["root"] / "must_not_exist.mp4"),
            scene_id="sc_opaque",
            avatar_clip=media["avatar_opaque"],
        )

        with pytest.raises(StudioCompositeError, match="no alpha channel"):
            composite_studio_scene(request)
        assert not (media["root"] / "must_not_exist.mp4").exists()

    def test_a_webm_flagged_alpha_mode_counts_as_transparent(self, monkeypatch):
        # Real HeyGen WebM keeps yuv420p and carries alpha out of band.
        monkeypatch.setattr(
            "btcedu.services.studio_compositor._ffprobe_streams",
            lambda path: [
                {"codec_type": "video", "pix_fmt": "yuv420p", "tags": {"alpha_mode": "1"}}
            ],
        )
        assert probe_has_alpha("whatever.webm") is True

    def test_a_file_without_a_video_stream_is_an_error(self, monkeypatch):
        monkeypatch.setattr(
            "btcedu.services.studio_compositor._ffprobe_streams",
            lambda path: [{"codec_type": "audio"}],
        )
        with pytest.raises(StudioCompositeError, match="No video stream"):
            probe_has_alpha("audio_only.m4a")


class TestFilterGraph:
    def _request(self, tmp_path, **kwargs):
        studio = tmp_path / "studio"
        (studio / "plate").mkdir(parents=True)
        (studio / "layers").mkdir(parents=True)
        (studio / "fallback").mkdir(parents=True)
        overrides = kwargs.pop("manifest_overrides", {})
        path = studio / "manifest.json"
        path.write_text(json.dumps(_studio_data(**overrides)), encoding="utf-8")
        manifest = load_studio_manifest(path)
        defaults = {
            "manifest": manifest,
            "audio_path": str(tmp_path / "narration.mp3"),
            "output_path": str(tmp_path / "out.mp4"),
            "scene_id": "sc_001",
        }
        defaults.update(kwargs)
        return StudioCompositeRequest(**defaults)

    def _graph(self, cmd: list[str]) -> str:
        return cmd[cmd.index("-filter_complex") + 1]

    def test_only_the_narration_is_mapped_as_audio(self, tmp_path):
        request = self._request(
            tmp_path,
            avatar_clip=str(tmp_path / "avatar.mov"),
            display_media=DisplayMedia(path=str(tmp_path / "topic.png")),
        )
        cmd, _ = build_composite_command(request, 5.0, perspective_available=False)

        audio_maps = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-map"]
        assert audio_maps[0] == "[vout]"
        assert len(audio_maps) == 2
        # The last input is the narration; nothing else may be mapped as audio.
        assert audio_maps[1].endswith(":a:0")
        narration_index = int(audio_maps[1].split(":")[0])
        inputs = [cmd[i + 1] for i, arg in enumerate(cmd) if arg == "-i"]
        assert inputs[narration_index] == request.audio_path

    def test_output_is_yuv420p_at_the_manifest_frame_rate(self, tmp_path):
        request = self._request(tmp_path)
        cmd, _ = build_composite_command(request, 5.0, perspective_available=False)

        assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
        assert cmd[cmd.index("-r") + 1] == str(FPS)
        assert cmd[cmd.index("-c:v") + 1] == "libx264"

    def test_duration_is_pinned_to_the_narration(self, tmp_path):
        request = self._request(tmp_path)
        cmd, _ = build_composite_command(request, 7.25, perspective_available=False)

        assert cmd[cmd.index("-t") + 1] == "7.25"
        assert "-shortest" not in cmd

    def test_the_presenter_is_padded_not_cut(self, tmp_path):
        request = self._request(tmp_path, avatar_clip=str(tmp_path / "avatar.mov"))
        cmd, _ = build_composite_command(request, 5.0, perspective_available=False)

        assert "tpad=stop_mode=clone" in self._graph(cmd)

    def test_cover_crops_around_the_focus_point(self, tmp_path):
        request = self._request(
            tmp_path,
            display_media=DisplayMedia(
                path=str(tmp_path / "topic.png"), fit_mode="cover", focus_point=(0.25, 0.75)
            ),
        )
        cmd, _ = build_composite_command(request, 5.0, perspective_available=False)
        graph = self._graph(cmd)

        assert "force_original_aspect_ratio=increase" in graph
        assert "crop=140:79:(iw-ow)*0.25:(ih-oh)*0.75" in graph

    def test_contain_pads_instead_of_cropping(self, tmp_path):
        request = self._request(
            tmp_path,
            display_media=DisplayMedia(path=str(tmp_path / "topic.png"), fit_mode="contain"),
        )
        graph = self._graph(
            build_composite_command(request, 5.0, perspective_available=False)[0]
        )

        assert "force_original_aspect_ratio=decrease" in graph
        assert "pad=140:79" in graph
        assert "crop=" not in graph

    def test_an_unknown_fit_mode_is_refused(self, tmp_path):
        request = self._request(
            tmp_path,
            display_media=DisplayMedia(path=str(tmp_path / "topic.png"), fit_mode="stretch"),
        )
        with pytest.raises(StudioCompositeError, match="fit mode"):
            build_composite_command(request, 5.0, perspective_available=False)

    def test_perspective_is_used_when_corners_exist_and_ffmpeg_supports_it(self, tmp_path):
        overrides = _studio_data()
        overrides["display_zone"]["corners"] = [[160, 10], [300, 14], [300, 89], [160, 85]]
        request = self._request(
            tmp_path,
            manifest_overrides={"display_zone": overrides["display_zone"]},
            display_media=DisplayMedia(path=str(tmp_path / "topic.png")),
        )

        cmd, used = build_composite_command(request, 5.0, perspective_available=True)

        assert used is True
        assert "perspective=160:10:300:14:300:89:160:85:sense=destination" in self._graph(cmd)

    def test_perspective_is_skipped_when_ffmpeg_lacks_the_filter(self, tmp_path):
        overrides = _studio_data()
        overrides["display_zone"]["corners"] = [[160, 10], [300, 14], [300, 89], [160, 85]]
        request = self._request(
            tmp_path,
            manifest_overrides={"display_zone": overrides["display_zone"]},
            display_media=DisplayMedia(path=str(tmp_path / "topic.png")),
        )

        cmd, used = build_composite_command(request, 5.0, perspective_available=False)

        assert used is False
        assert "perspective=" not in self._graph(cmd)

    def test_optional_layers_appear_only_when_declared(self, tmp_path):
        plain = self._request(tmp_path, avatar_clip=str(tmp_path / "a.mov"))
        plain_graph = self._graph(
            build_composite_command(plain, 5.0, perspective_available=False)[0]
        )
        assert "[shadow]" not in plain_graph
        assert "[fg]" not in plain_graph

        with_layers = self._request(
            tmp_path / "layered",
            manifest_overrides={
                "shadow_layer": "layers/shadow.png",
                "foreground_layer": "layers/desk.png",
            },
            avatar_clip=str(tmp_path / "a.mov"),
        )
        layered_graph = self._graph(
            build_composite_command(with_layers, 5.0, perspective_available=False)[0]
        )
        assert "[shadow]" in layered_graph
        assert "[fg]" in layered_graph
        # The desk is drawn over the presenter, the shadow under her.
        assert layered_graph.index("[withshadow]") < layered_graph.index("[withavatar]")
        assert layered_graph.index("[withavatar]") < layered_graph.index("[withfg]")

    def test_no_shell_is_ever_involved(self, tmp_path):
        request = self._request(
            tmp_path,
            avatar_clip=str(tmp_path / "a b';rm -rf x.mov"),
            display_media=DisplayMedia(path=str(tmp_path / "t$(id).png")),
        )
        cmd, _ = build_composite_command(request, 5.0, perspective_available=False)

        # Hostile characters survive as literal argv entries and never reach a
        # filter string, which is the only place they could be interpreted.
        assert str(tmp_path / "a b';rm -rf x.mov") in cmd
        assert "rm -rf" not in self._graph(cmd)
        assert "$(id)" not in self._graph(cmd)


class TestOpaqueFallbackPath:
    def test_the_opaque_path_shares_the_architecture(self, tmp_path):
        studio = tmp_path / "studio"
        (studio / "plate").mkdir(parents=True)
        (studio / "fallback").mkdir(parents=True)
        data = _studio_data(alpha_mode=ALPHA_MODE_OPAQUE)
        data["display_zone"]["presenter_free"] = True
        (studio / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        manifest = load_studio_manifest(studio / "manifest.json")

        request = StudioCompositeRequest(
            manifest=manifest,
            audio_path=str(tmp_path / "n.mp3"),
            output_path=str(tmp_path / "out.mp4"),
            avatar_clip=str(tmp_path / "avatar.mp4"),
            display_media=DisplayMedia(path=str(tmp_path / "topic.png")),
        )
        cmd, _ = build_composite_command(request, 4.0, perspective_available=False)

        # Same graph shape, same encoder settings: only the alpha promise differs.
        assert cmd[cmd.index("-pix_fmt") + 1] == "yuv420p"
        assert "[withavatar]" in cmd[cmd.index("-filter_complex") + 1]

    @needs_ffmpeg
    def test_an_opaque_clip_needs_no_alpha_in_opaque_mode(self, media, tmp_path):
        data = _studio_data(alpha_mode=ALPHA_MODE_OPAQUE)
        data["display_zone"]["presenter_free"] = True
        manifest_path = media["studio"] / "manifest_opaque.json"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        manifest = load_studio_manifest(manifest_path)

        result = composite_studio_scene(
            StudioCompositeRequest(
                manifest=manifest,
                audio_path=media["narration"],
                output_path=str(tmp_path / "opaque.mp4"),
                scene_id="sc_opaque_ok",
                avatar_clip=media["avatar_opaque"],
                display_media=DisplayMedia(path=str(media["episode"] / "topic_wide.png")),
            )
        )

        assert Path(result.output_path).exists()
        assert result.alpha_mode == ALPHA_MODE_OPAQUE

    @needs_ffmpeg
    def test_a_monitor_over_an_unknown_presenter_position_is_refused(self, media, tmp_path):
        data = _studio_data(alpha_mode=ALPHA_MODE_OPAQUE)
        data["display_zone"]["presenter_free"] = True
        manifest_path = media["studio"] / "manifest_opaque2.json"
        manifest_path.write_text(json.dumps(data), encoding="utf-8")
        manifest = load_studio_manifest(manifest_path)
        # A manifest cannot express this, so it is forced here: the runtime
        # check must hold even if a manifest is built in code.
        object.__setattr__(manifest.display_zone, "presenter_free", False)

        with pytest.raises(StudioCompositeError, match="presenter provably is not"):
            composite_studio_scene(
                StudioCompositeRequest(
                    manifest=manifest,
                    audio_path=media["narration"],
                    output_path=str(tmp_path / "never.mp4"),
                    avatar_clip=media["avatar_opaque"],
                    display_media=DisplayMedia(path=str(media["episode"] / "topic_wide.png")),
                )
            )


class TestDryRun:
    def test_dry_run_builds_a_command_and_touches_nothing(self, tmp_path):
        studio = tmp_path / "studio"
        (studio / "plate").mkdir(parents=True)
        (studio / "fallback").mkdir(parents=True)
        (studio / "manifest.json").write_text(json.dumps(_studio_data()), encoding="utf-8")
        manifest = load_studio_manifest(studio / "manifest.json")
        audio = tmp_path / "narration.mp3"
        audio.write_bytes(b"\xff\xfb\x90" + b"\x00" * 64)
        output = tmp_path / "out.mp4"

        result = composite_studio_scene(
            StudioCompositeRequest(
                manifest=manifest,
                audio_path=str(audio),
                output_path=str(output),
                scene_id="sc_dry",
                duration_seconds=4.0,
            ),
            dry_run=True,
        )

        assert result.dry_run is True
        assert result.ffmpeg_command[0] == "ffmpeg"
        assert not output.exists()

    def test_dry_run_does_not_require_real_studio_assets(self, tmp_path):
        studio = tmp_path / "studio"
        studio.mkdir()
        (studio / "manifest.json").write_text(json.dumps(_studio_data()), encoding="utf-8")
        manifest = load_studio_manifest(studio / "manifest.json")
        audio = tmp_path / "narration.mp3"
        audio.write_bytes(b"\xff\xfb\x90")

        result = composite_studio_scene(
            StudioCompositeRequest(
                manifest=manifest,
                audio_path=str(audio),
                output_path=str(tmp_path / "out.mp4"),
                duration_seconds=3.0,
            ),
            dry_run=True,
        )

        assert result.duration_seconds == 3.0


@needs_ffmpeg
class TestRealCompositing:
    def _compose(self, media, tmp_path, name, **kwargs):
        manifest = kwargs.pop("manifest", None) or _manifest(media["studio"])
        return composite_studio_scene(
            StudioCompositeRequest(
                manifest=manifest,
                audio_path=media["narration"],
                output_path=str(tmp_path / name),
                scene_id=name,
                **kwargs,
            )
        )

    def test_a_still_in_the_monitor_produces_a_correct_file(self, media, tmp_path):
        result = self._compose(
            media,
            tmp_path,
            "still.mp4",
            avatar_clip=media["avatar_alpha"],
            display_media=DisplayMedia(path=str(media["episode"] / "topic_wide.png")),
        )

        probe = _probe(result.output_path)
        video = next(s for s in probe["streams"] if s["codec_type"] == "video")
        assert (video["width"], video["height"]) == (WIDTH, HEIGHT)
        assert video["pix_fmt"] == "yuv420p"
        assert video["r_frame_rate"] == f"{FPS}/1"
        assert result.size_bytes > 0

    def test_a_video_in_the_monitor_works_too(self, media, tmp_path):
        result = self._compose(
            media,
            tmp_path,
            "clip.mp4",
            avatar_clip=media["avatar_alpha"],
            display_media=DisplayMedia(
                path=str(media["episode"] / "topic_clip.mp4"), kind="video"
            ),
        )

        assert Path(result.output_path).exists()
        assert abs(result.duration_seconds - NARRATION_SECONDS) < 0.35

    def test_the_duration_follows_the_narration_not_the_clip(self, media, tmp_path):
        # The avatar clip is 1.0s, the narration 1.6s. The presenter must be
        # held, not the sentence cut.
        assert abs(probe_duration(media["avatar_alpha"]) - 1.0) < 0.2

        result = self._compose(media, tmp_path, "held.mp4", avatar_clip=media["avatar_alpha"])

        assert result.duration_seconds > 1.4

    def test_the_original_narration_is_the_final_audio(self, media, tmp_path):
        result = self._compose(
            media,
            tmp_path,
            "audio.mp4",
            avatar_clip=media["avatar_alpha_with_audio"],
        )

        probe = _probe(result.output_path)
        audio_streams = [s for s in probe["streams"] if s["codec_type"] == "audio"]
        assert len(audio_streams) == 1
        # The avatar's track is silence, the narration is a sine. A silent
        # result would mean the wrong track survived.
        assert _mean_volume(result.output_path) > -50.0

    def test_cover_and_contain_produce_different_pictures(self, media, tmp_path):
        cover = self._compose(
            media,
            tmp_path,
            "cover.mp4",
            display_media=DisplayMedia(
                path=str(media["episode"] / "topic_tall.png"), fit_mode="cover"
            ),
        )
        contain = self._compose(
            media,
            tmp_path,
            "contain.mp4",
            display_media=DisplayMedia(
                path=str(media["episode"] / "topic_tall.png"), fit_mode="contain"
            ),
        )

        assert Path(cover.output_path).read_bytes() != Path(contain.output_path).read_bytes()

    def test_the_focus_point_changes_the_crop(self, media, tmp_path):
        left = self._compose(
            media,
            tmp_path,
            "focus_left.mp4",
            display_media=DisplayMedia(
                path=str(media["episode"] / "topic_clip.mp4"),
                kind="video",
                fit_mode="cover",
                focus_point=(0.0, 0.0),
            ),
        )
        right = self._compose(
            media,
            tmp_path,
            "focus_right.mp4",
            display_media=DisplayMedia(
                path=str(media["episode"] / "topic_clip.mp4"),
                kind="video",
                fit_mode="cover",
                focus_point=(1.0, 1.0),
            ),
        )

        assert Path(left.output_path).read_bytes() != Path(right.output_path).read_bytes()

    def test_the_desk_layer_is_composited_when_declared(self, media, tmp_path):
        plain = self._compose(media, tmp_path, "nodesk.mp4", avatar_clip=media["avatar_alpha"])
        with_desk = self._compose(
            media,
            tmp_path,
            "desk.mp4",
            manifest=_manifest(
                media["studio"],
                foreground_layer="layers/desk.png",
                shadow_layer="layers/shadow.png",
            ),
            avatar_clip=media["avatar_alpha"],
        )

        assert Path(plain.output_path).exists()
        assert Path(with_desk.output_path).exists()
        assert with_desk.size_bytes > 0

    def test_the_output_is_written_atomically(self, media, tmp_path):
        result = self._compose(media, tmp_path, "atomic.mp4", avatar_clip=media["avatar_alpha"])

        assert Path(result.output_path).exists()
        assert list(tmp_path.glob(".*.part.mp4")) == []

    def test_a_failed_render_leaves_no_output_behind(self, media, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "btcedu.services.studio_compositor._run", lambda cmd, timeout: (1, "boom")
        )
        with pytest.raises(StudioCompositeError, match="compositing failed"):
            self._compose(media, tmp_path, "broken.mp4", avatar_clip=media["avatar_alpha"])

        assert not (tmp_path / "broken.mp4").exists()
        assert list(tmp_path.glob(".*.part.mp4")) == []

    def test_a_truncated_result_is_rejected(self, media, tmp_path, monkeypatch):
        real_duration = probe_duration

        def half(path):
            value = real_duration(path)
            return value / 2 if str(path).endswith(".part.mp4") else value

        monkeypatch.setattr("btcedu.services.studio_compositor.probe_duration", half)
        with pytest.raises(StudioCompositeError, match="end of the sentence"):
            self._compose(media, tmp_path, "short.mp4", avatar_clip=media["avatar_alpha"])

        assert not (tmp_path / "short.mp4").exists()

    def test_a_missing_input_is_refused_before_ffmpeg_runs(self, media, tmp_path):
        with pytest.raises(StudioCompositeError, match="Avatar clip not found"):
            self._compose(media, tmp_path, "missing.mp4", avatar_clip=str(tmp_path / "nope.mov"))

        with pytest.raises(StudioCompositeError, match="Narration audio not found"):
            composite_studio_scene(
                StudioCompositeRequest(
                    manifest=_manifest(media["studio"]),
                    audio_path=str(tmp_path / "nothing.mp3"),
                    output_path=str(tmp_path / "x.mp4"),
                )
            )
