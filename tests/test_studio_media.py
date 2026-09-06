"""Resolving the evening's topic medium, and deciding what a change invalidates.

Two separate promises are tested here. The first is that the studio monitor only
ever shows a file the pipeline already produced -- there is no second image or
video generation hiding behind the studio. The second is that the invalidation
hashes are wired so that a repainted studio wall or a swapped topic picture
never looks like a reason to buy an avatar clip again.
"""

import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from btcedu.core.avatar_jobs import compute_job_hash
from btcedu.core.studio_manifest import load_studio_manifest, studio_content_hash
from btcedu.core.studio_media import (
    DisplayMediaUnavailableError,
    classify_media,
    composite_content_hash,
    media_digest,
    resolve_scene_media,
)

RENDERER_VERSION = "2.0.0"


@dataclass
class FakeScene:
    """Only the fields the resolver looks at."""

    scene_id: str = "sc_001"
    chapter_id: str = "ch_01"
    beat_index: int = 0
    background_asset: str | None = None
    speaker_role: str = "anchor_female"


def _studio(tmp_path: Path, *, with_fallback: bool = True) -> "object":
    studio = tmp_path / "studio"
    (studio / "plate").mkdir(parents=True, exist_ok=True)
    (studio / "fallback").mkdir(parents=True, exist_ok=True)
    (studio / "plate" / "bg.png").write_bytes(b"plate")
    (studio / "fallback" / "neutral.png").write_bytes(b"neutral card")

    data = {
        "schema_version": 1,
        "studio_version": "1.0.0",
        "asset_version": "1.0.0",
        "width": 1920,
        "height": 1080,
        "fps": 25,
        "alpha_mode": "alpha_webm",
        "background": {"path": "plate/bg.png", "kind": "image"},
        "display_zone": {
            "zone_id": "main_wall",
            "rect": {"x": 960, "y": 140, "width": 820, "height": 461},
            "fit_mode": "cover",
            "focus_point": [0.5, 0.4],
            "presenter_free": True,
        },
        "presenter": {"anchor_x": 620, "anchor_y": 1080, "scale": 1.0},
        "logo_zone": {"x": 1660, "y": 60, "width": 200, "height": 80},
        "safe_areas": {
            "lower_third": {"x": 120, "y": 780, "width": 1200, "height": 160},
            "ticker": {"x": 0, "y": 960, "width": 1920, "height": 80},
            "subtitle": {"x": 240, "y": 860, "width": 1440, "height": 96},
        },
    }
    if with_fallback:
        data["fallback_display_media"] = {"path": "fallback/neutral.png", "kind": "image"}
    path = studio / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return load_studio_manifest(path)


def _episode(tmp_path: Path) -> Path:
    outputs = tmp_path / "episode"
    (outputs / "images").mkdir(parents=True, exist_ok=True)
    (outputs / "images" / "ch_01_b00.png").write_bytes(b"topic picture")
    (outputs / "images" / "ch_01_b01.png").write_bytes(b"second block picture")
    return outputs


class TestClassification:
    @pytest.mark.parametrize(
        "name,kind",
        [
            ("a.png", "image"),
            ("a.JPG", "image"),
            ("a.webp", "image"),
            ("a.mp4", "video"),
            ("a.webm", "video"),
            ("a.mov", "video"),
        ],
    )
    def test_known_extensions(self, name, kind):
        assert classify_media(name) == kind

    def test_an_unknown_extension_is_refused(self):
        with pytest.raises(DisplayMediaUnavailableError, match="Unsupported"):
            classify_media("a.psd")


class TestResolution:
    def test_the_scene_medium_is_used_when_it_exists(self, tmp_path):
        outputs = _episode(tmp_path)
        studio = _studio(tmp_path)
        scene = FakeScene(background_asset="images/ch_01_b00.png")

        resolved = resolve_scene_media(scene, outputs, studio)

        assert resolved.path == (outputs / "images" / "ch_01_b00.png").resolve()
        assert resolved.kind == "image"
        assert resolved.is_fallback is False

    def test_the_chapter_manifest_is_consulted_when_the_scene_names_nothing(self, tmp_path):
        outputs = _episode(tmp_path)
        studio = _studio(tmp_path)
        manifests = {
            "image_manifest": {
                "images": [
                    {
                        "chapter_id": "ch_01",
                        "file_path": "images/ch_01_b01.png",
                        "metadata": {"beat_index": 0},
                    }
                ]
            }
        }

        resolved = resolve_scene_media(FakeScene(), outputs, studio, manifests=manifests)

        assert resolved.relative_path == "images/ch_01_b01.png"
        assert resolved.is_fallback is False

    def test_a_failed_image_entry_is_never_shown(self, tmp_path):
        outputs = _episode(tmp_path)
        studio = _studio(tmp_path)
        manifests = {
            "image_manifest": {
                "images": [
                    {
                        "chapter_id": "ch_01",
                        "file_path": "images/ch_01_b00.png",
                        "generation_method": "failed",
                    }
                ]
            }
        }

        resolved = resolve_scene_media(FakeScene(), outputs, studio, manifests=manifests)

        assert resolved.is_fallback is True

    def test_a_video_manifest_wins_over_a_still(self, tmp_path):
        outputs = _episode(tmp_path)
        (outputs / "video").mkdir()
        (outputs / "video" / "ch_01.mp4").write_bytes(b"clip")
        studio = _studio(tmp_path)
        manifests = {
            "video_manifest": {"videos": [{"chapter_id": "ch_01", "file_path": "video/ch_01.mp4"}]},
            "image_manifest": {
                "images": [{"chapter_id": "ch_01", "file_path": "images/ch_01_b00.png"}]
            },
        }

        resolved = resolve_scene_media(FakeScene(), outputs, studio, manifests=manifests)

        assert resolved.kind == "video"
        assert resolved.relative_path == "video/ch_01.mp4"

    def test_a_missing_file_falls_through_to_the_neutral_card(self, tmp_path):
        outputs = _episode(tmp_path)
        studio = _studio(tmp_path)
        scene = FakeScene(background_asset="images/never_generated.png")

        resolved = resolve_scene_media(scene, outputs, studio)

        assert resolved.is_fallback is True
        assert resolved.path.name == "neutral.png"

    def test_without_a_declared_fallback_the_resolver_fails_closed(self, tmp_path):
        outputs = _episode(tmp_path)
        studio = _studio(tmp_path, with_fallback=False)

        with pytest.raises(DisplayMediaUnavailableError, match="no defined picture"):
            resolve_scene_media(FakeScene(), outputs, studio)

    def test_a_declared_but_missing_fallback_fails_closed(self, tmp_path):
        outputs = _episode(tmp_path)
        studio = _studio(tmp_path)
        studio.asset_path(studio.fallback_display_media).unlink()

        with pytest.raises(DisplayMediaUnavailableError, match="missing"):
            resolve_scene_media(FakeScene(), outputs, studio)

    @pytest.mark.parametrize(
        "hostile", ["/etc/passwd", "../../../etc/hosts.png", "images/../../secret.png"]
    )
    def test_media_outside_the_episode_is_refused(self, tmp_path, hostile):
        outputs = _episode(tmp_path)
        studio = _studio(tmp_path)

        # An unsafe path is never opened; the scene falls back instead.
        resolved = resolve_scene_media(FakeScene(background_asset=hostile), outputs, studio)

        assert resolved.is_fallback is True


class TestTwoPresentationsOfOneAsset:
    def test_the_same_file_serves_the_monitor_and_the_full_frame(self, tmp_path):
        outputs = _episode(tmp_path)
        studio = _studio(tmp_path)
        anchor = FakeScene(scene_id="sc_a", background_asset="images/ch_01_b00.png")
        reporter = FakeScene(
            scene_id="sc_b", background_asset="images/ch_01_b00.png", speaker_role="reporter_male"
        )

        in_monitor = resolve_scene_media(anchor, outputs, studio).for_monitor(studio)
        full_frame = resolve_scene_media(reporter, outputs, studio).for_fullscreen()

        assert in_monitor.path == full_frame.path
        assert in_monitor.focus_point == studio.display_zone.focus_point
        assert full_frame.focus_point == (0.5, 0.5)

    def test_the_monitor_inherits_the_zone_framing(self, tmp_path):
        outputs = _episode(tmp_path)
        studio = _studio(tmp_path)
        resolved = resolve_scene_media(
            FakeScene(background_asset="images/ch_01_b00.png"), outputs, studio
        )

        assert resolved.for_monitor(studio).fit_mode == studio.display_zone.fit_mode
        assert resolved.for_monitor(studio, "contain").fit_mode == "contain"


class TestInvalidationRules:
    """Which change invalidates what -- the rules, expressed as hashes."""

    def _hashes(self, tmp_path, **overrides):
        studio = overrides.pop("studio", None) or _studio(tmp_path)
        base = {
            "studio_hash": studio_content_hash(studio),
            "scene_id": "sc_001",
            "scene_plan_hash": "plan-1",
            "display_media_hash": "media-1",
            "avatar_clip_hash": "clip-1",
            "avatar_look_id": "look-1",
            "audio_hash": "audio-1",
            "compositing_params": {"overlays": {"ticker": True}},
            "renderer_version": RENDERER_VERSION,
        }
        base.update(overrides)
        return composite_content_hash(**base)

    def _avatar_hash(self, **overrides):
        base = {
            "scene_id": "sc_001",
            "text_hash": "text-1",
            "audio_hash": "audio-1",
            "avatar_look_id": "look-1",
            "provider": "heygen",
            "engine": "avatar_iii",
            "output_format": "webm",
            "resolution": "1080p",
            "aspect_ratio": "16:9",
        }
        base.update(overrides)
        return compute_job_hash(**base)

    def test_the_same_inputs_hash_the_same(self, tmp_path):
        assert self._hashes(tmp_path) == self._hashes(tmp_path)

    def test_a_studio_change_invalidates_compositing_but_not_the_avatar(self, tmp_path):
        before = self._hashes(tmp_path)
        studio_dir = tmp_path / "studio"
        data = json.loads((studio_dir / "manifest.json").read_text(encoding="utf-8"))
        data["studio_version"] = "2.0.0"
        data["presenter"]["scale"] = 0.92
        (studio_dir / "manifest.json").write_text(json.dumps(data), encoding="utf-8")
        repainted = load_studio_manifest(studio_dir / "manifest.json")

        after = self._hashes(tmp_path, studio=repainted)

        assert before != after
        # The avatar job hash has no studio input at all, which is exactly why
        # a new studio cannot re-buy a clip.
        assert self._avatar_hash() == self._avatar_hash()

    def test_a_topic_media_change_invalidates_the_scene_but_not_the_avatar(self, tmp_path):
        before = self._hashes(tmp_path)
        after = self._hashes(tmp_path, display_media_hash="media-2")

        assert before != after
        assert self._avatar_hash() == self._avatar_hash()

    def test_a_new_avatar_clip_invalidates_the_scene(self, tmp_path):
        assert self._hashes(tmp_path) != self._hashes(tmp_path, avatar_clip_hash="clip-2")

    def test_an_overlay_only_change_invalidates_only_the_render(self, tmp_path):
        before = self._hashes(tmp_path)
        after = self._hashes(tmp_path, compositing_params={"overlays": {"ticker": False}})

        assert before != after
        # Neither the avatar job nor anything upstream of it moved.
        assert self._avatar_hash() == self._avatar_hash()

    def test_new_narration_audio_invalidates_both_the_scene_and_the_avatar(self, tmp_path):
        assert self._hashes(tmp_path) != self._hashes(tmp_path, audio_hash="audio-2")
        assert self._avatar_hash() != self._avatar_hash(audio_hash="audio-2")

    def test_a_renderer_upgrade_invalidates_compositing(self, tmp_path):
        assert self._hashes(tmp_path) != self._hashes(tmp_path, renderer_version="2.1.0")

    def test_a_changed_scene_plan_invalidates_compositing(self, tmp_path):
        assert self._hashes(tmp_path) != self._hashes(tmp_path, scene_plan_hash="plan-2")

    def test_a_reassigned_look_invalidates_both(self, tmp_path):
        assert self._hashes(tmp_path) != self._hashes(tmp_path, avatar_look_id="look-2")
        assert self._avatar_hash() != self._avatar_hash(avatar_look_id="look-2")


class TestMediaDigest:
    def test_identical_bytes_digest_identically(self, tmp_path):
        first = tmp_path / "a.png"
        second = tmp_path / "b.png"
        first.write_bytes(b"same picture")
        second.write_bytes(b"same picture")

        assert media_digest(first) == media_digest(second)

    def test_different_bytes_digest_differently(self, tmp_path):
        first = tmp_path / "a.png"
        second = tmp_path / "b.png"
        first.write_bytes(b"one picture")
        second.write_bytes(b"another picture")

        assert media_digest(first) != media_digest(second)

    def test_a_missing_file_has_an_empty_digest(self, tmp_path):
        assert media_digest(tmp_path / "gone.png") == ""
