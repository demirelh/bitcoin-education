"""The studio manifest is an input file, so it is treated like one.

A studio that is merely plausible produces a bulletin that is subtly wrong every
evening: a monitor over the presenter's face, a ticker behind the desk. These
tests pin down the rules that make "plausible" not good enough.
"""

import json
from pathlib import Path

import pytest

from btcedu.core.studio_manifest import (
    ALPHA_MODE_OPAQUE,
    Rect,
    StudioManifestError,
    StudioNotReadyError,
    load_studio_manifest,
    parse_studio_manifest,
    require_studio_ready,
    resolve_studio_asset,
    studio_content_hash,
    studio_readiness_problems,
)

EXAMPLE_MANIFEST = Path("assets/almanya24/studio/manifest.example.json")


def _valid_manifest() -> dict:
    return {
        "schema_version": 1,
        "studio_version": "1.2.0",
        "asset_version": "1.2.0",
        "name": "Test Studio",
        "width": 1920,
        "height": 1080,
        "fps": 25,
        "alpha_mode": "alpha_webm",
        "background": {"path": "plate/bg.png", "kind": "image"},
        "display_zone": {
            "zone_id": "main_wall",
            "rect": {"x": 960, "y": 140, "width": 820, "height": 461},
            "fit_mode": "cover",
            "focus_point": [0.5, 0.45],
            "presenter_free": True,
        },
        "presenter": {"anchor_x": 620, "anchor_y": 1080, "scale": 1.0},
        "fallback_display_media": {"path": "fallback/neutral.png", "kind": "image"},
        "logo_zone": {"x": 1660, "y": 60, "width": 200, "height": 80},
        "safe_areas": {
            "lower_third": {"x": 120, "y": 780, "width": 1200, "height": 160},
            "ticker": {"x": 0, "y": 960, "width": 1920, "height": 80},
            "subtitle": {"x": 240, "y": 860, "width": 1440, "height": 96},
        },
    }


def _write_studio(tmp_path: Path, data: dict, *, create_assets: bool = True) -> Path:
    studio = tmp_path / "studio"
    studio.mkdir(parents=True, exist_ok=True)
    if create_assets:
        for relative in (
            "plate/bg.png",
            "fallback/neutral.png",
            "layers/desk.png",
            "layers/shadow.png",
            "plate/loop.mp4",
            "plate/intro.mp4",
            "layers/mask.png",
        ):
            target = studio / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"synthetic asset bytes")
    path = studio / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class TestValidManifests:
    def test_a_complete_manifest_parses(self, tmp_path):
        manifest = load_studio_manifest(_write_studio(tmp_path, _valid_manifest()))

        assert manifest.width == 1920
        assert manifest.fps == 25
        assert manifest.display_zone.zone_id == "main_wall"
        assert manifest.presenter.anchor_y == 1080
        assert manifest.safe_areas.ticker.height == 80

    def test_optional_layers_are_optional(self, tmp_path):
        manifest = load_studio_manifest(_write_studio(tmp_path, _valid_manifest()))

        assert manifest.shadow_layer is None
        assert manifest.foreground_layer is None
        assert manifest.intro_asset is None
        assert manifest.loop_asset is None

    def test_optional_layers_are_parsed_when_present(self, tmp_path):
        data = _valid_manifest()
        data["shadow_layer"] = "layers/shadow.png"
        data["foreground_layer"] = {"path": "layers/desk.png", "kind": "image"}
        data["loop_asset"] = {"path": "plate/loop.mp4", "kind": "video"}
        data["intro_asset"] = "plate/intro.mp4"

        manifest = load_studio_manifest(_write_studio(tmp_path, data))

        assert manifest.shadow_layer.kind == "image"
        assert manifest.loop_asset.kind == "video"
        assert manifest.intro_asset.path == "plate/intro.mp4"

    def test_four_corners_are_accepted_for_a_tilted_wall(self, tmp_path):
        data = _valid_manifest()
        data["display_zone"]["corners"] = [[960, 140], [1780, 152], [1780, 601], [960, 589]]

        manifest = load_studio_manifest(_write_studio(tmp_path, data))

        assert len(manifest.display_zone.corners) == 4
        assert manifest.display_zone.corners[1] == (1780, 152)


class TestRejectedManifests:
    @pytest.mark.parametrize("version", [0, 2, 99])
    def test_unknown_schema_version_is_refused(self, tmp_path, version):
        data = _valid_manifest()
        data["schema_version"] = version
        with pytest.raises(StudioManifestError, match="schema_version"):
            load_studio_manifest(_write_studio(tmp_path, data))

    def test_missing_required_field_is_refused(self, tmp_path):
        data = _valid_manifest()
        del data["presenter"]
        with pytest.raises(StudioManifestError, match="presenter"):
            load_studio_manifest(_write_studio(tmp_path, data))

    def test_display_zone_outside_the_frame_is_refused(self, tmp_path):
        data = _valid_manifest()
        data["display_zone"]["rect"] = {"x": 1800, "y": 100, "width": 400, "height": 200}
        with pytest.raises(StudioManifestError, match="inside the frame"):
            load_studio_manifest(_write_studio(tmp_path, data))

    @pytest.mark.parametrize("area", ["lower_third", "ticker", "subtitle"])
    def test_monitor_over_a_text_safe_area_is_refused(self, tmp_path, area):
        data = _valid_manifest()
        data["display_zone"]["rect"] = dict(data["safe_areas"][area])
        with pytest.raises(StudioManifestError, match="safe area"):
            load_studio_manifest(_write_studio(tmp_path, data))

    def test_bad_fit_mode_is_refused(self, tmp_path):
        data = _valid_manifest()
        data["display_zone"]["fit_mode"] = "stretch"
        with pytest.raises(StudioManifestError, match="fit_mode"):
            load_studio_manifest(_write_studio(tmp_path, data))

    def test_focus_point_outside_the_unit_square_is_refused(self, tmp_path):
        data = _valid_manifest()
        data["display_zone"]["focus_point"] = [1.4, 0.5]
        with pytest.raises(StudioManifestError, match="focus_point"):
            load_studio_manifest(_write_studio(tmp_path, data))

    def test_three_corners_are_refused(self, tmp_path):
        data = _valid_manifest()
        data["display_zone"]["corners"] = [[0, 0], [10, 0], [10, 10]]
        with pytest.raises(StudioManifestError, match="four points"):
            load_studio_manifest(_write_studio(tmp_path, data))

    def test_absurd_presenter_scale_is_refused(self, tmp_path):
        data = _valid_manifest()
        data["presenter"]["scale"] = 12.0
        with pytest.raises(StudioManifestError, match="scale"):
            load_studio_manifest(_write_studio(tmp_path, data))

    def test_unknown_alpha_mode_is_refused(self, tmp_path):
        data = _valid_manifest()
        data["alpha_mode"] = "magic"
        with pytest.raises(StudioManifestError, match="alpha_mode"):
            load_studio_manifest(_write_studio(tmp_path, data))

    def test_broken_json_is_refused_with_a_useful_message(self, tmp_path):
        studio = tmp_path / "studio"
        studio.mkdir()
        (studio / "manifest.json").write_text("{not json", encoding="utf-8")
        with pytest.raises(StudioManifestError, match="not valid JSON"):
            load_studio_manifest(studio / "manifest.json")

    def test_missing_manifest_is_refused(self, tmp_path):
        with pytest.raises(StudioManifestError, match="not found"):
            load_studio_manifest(tmp_path / "nope" / "manifest.json")

    def test_unknown_asset_extension_is_refused(self, tmp_path):
        data = _valid_manifest()
        data["background"] = {"path": "plate/bg.tiff"}
        with pytest.raises(StudioManifestError, match="image or a video"):
            load_studio_manifest(_write_studio(tmp_path, data))


class TestOpaqueFallbackSafety:
    def test_opaque_mode_needs_a_presenter_free_zone_or_a_mask(self, tmp_path):
        data = _valid_manifest()
        data["alpha_mode"] = ALPHA_MODE_OPAQUE
        data["display_zone"]["presenter_free"] = False
        with pytest.raises(StudioManifestError, match="presenter_free|occlusion_mask"):
            load_studio_manifest(_write_studio(tmp_path, data))

    def test_opaque_mode_accepts_a_presenter_free_zone(self, tmp_path):
        data = _valid_manifest()
        data["alpha_mode"] = ALPHA_MODE_OPAQUE
        data["display_zone"]["presenter_free"] = True

        manifest = load_studio_manifest(_write_studio(tmp_path, data))

        assert manifest.alpha_mode == ALPHA_MODE_OPAQUE

    def test_opaque_mode_accepts_an_occlusion_mask(self, tmp_path):
        data = _valid_manifest()
        data["alpha_mode"] = ALPHA_MODE_OPAQUE
        data["display_zone"]["presenter_free"] = False
        data["occlusion_mask"] = {"path": "layers/mask.png", "kind": "image"}

        manifest = load_studio_manifest(_write_studio(tmp_path, data))

        assert manifest.occlusion_mask.path == "layers/mask.png"

    def test_an_occlusion_mask_must_be_an_image(self, tmp_path):
        data = _valid_manifest()
        data["occlusion_mask"] = {"path": "plate/loop.mp4", "kind": "video"}
        with pytest.raises(StudioManifestError, match="occlusion_mask.kind"):
            load_studio_manifest(_write_studio(tmp_path, data))


class TestPathSafety:
    @pytest.mark.parametrize(
        "hostile",
        [
            "/etc/passwd",
            "../../.env",
            "plate/../../secrets.png",
            "plate/$(id).png",
            "plate/a;rm -rf b.png",
            "plate/a'b.png",
            "",
        ],
    )
    def test_paths_that_leave_the_studio_are_refused(self, tmp_path, hostile):
        with pytest.raises(StudioManifestError):
            resolve_studio_asset(tmp_path, hostile)

    def test_a_hostile_path_in_a_manifest_is_refused_at_load_time(self, tmp_path):
        data = _valid_manifest()
        data["background"] = {"path": "../../../etc/hosts.png", "kind": "image"}
        with pytest.raises(StudioManifestError, match="escapes the studio|Unsafe asset path"):
            load_studio_manifest(_write_studio(tmp_path, data))

    def test_a_symlink_out_of_the_studio_is_refused(self, tmp_path):
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"secret")
        studio = tmp_path / "studio"
        studio.mkdir()
        (studio / "link.png").symlink_to(outside)

        with pytest.raises(StudioManifestError, match="escapes the studio"):
            resolve_studio_asset(studio, "link.png")

    def test_a_normal_relative_path_resolves_inside_the_studio(self, tmp_path):
        studio = tmp_path / "studio"
        studio.mkdir()
        resolved = resolve_studio_asset(studio, "plate/bg.png")
        assert resolved == (studio / "plate" / "bg.png").resolve()


class TestReadiness:
    def test_missing_asset_files_block_the_studio(self, tmp_path):
        manifest = load_studio_manifest(
            _write_studio(tmp_path, _valid_manifest(), create_assets=False)
        )

        problems = studio_readiness_problems(manifest)

        assert any("plate/bg.png" in problem for problem in problems)
        with pytest.raises(StudioNotReadyError, match="not ready"):
            require_studio_ready(manifest)

    def test_a_complete_studio_is_ready(self, tmp_path):
        manifest = load_studio_manifest(_write_studio(tmp_path, _valid_manifest()))

        assert studio_readiness_problems(manifest) == []
        require_studio_ready(manifest)

    def test_a_studio_without_a_fallback_graphic_is_not_ready(self, tmp_path):
        data = _valid_manifest()
        data.pop("fallback_display_media")
        manifest = load_studio_manifest(_write_studio(tmp_path, data))

        problems = studio_readiness_problems(manifest)

        assert any("fallback_display_media" in problem for problem in problems)

    def test_an_empty_asset_file_is_not_ready(self, tmp_path):
        path = _write_studio(tmp_path, _valid_manifest())
        (path.parent / "plate" / "bg.png").write_bytes(b"")
        manifest = load_studio_manifest(path)

        assert any("empty" in problem for problem in studio_readiness_problems(manifest))

    def test_a_wrong_digest_is_not_ready(self, tmp_path):
        data = _valid_manifest()
        data["background"] = {"path": "plate/bg.png", "kind": "image", "sha256": "0" * 64}
        manifest = load_studio_manifest(_write_studio(tmp_path, data))

        assert any("sha256" in problem for problem in studio_readiness_problems(manifest))

    def test_placeholder_flags_block_the_studio(self, tmp_path):
        data = _valid_manifest()
        data["placeholder"] = True
        data["background"] = {"path": "plate/bg.png", "kind": "image", "placeholder": True}
        manifest = load_studio_manifest(_write_studio(tmp_path, data))

        problems = studio_readiness_problems(manifest)

        assert any("placeholder=true" in problem for problem in problems)
        assert any("marked as a placeholder" in problem for problem in problems)


class TestShippedExample:
    def test_the_repository_ships_an_example_and_no_production_manifest(self):
        assert EXAMPLE_MANIFEST.exists()
        # A manifest.json here would tell the pipeline the studio exists. Asset
        # phase 1 has not happened, so it must not exist.
        assert not (EXAMPLE_MANIFEST.parent / "manifest.json").exists()

    def test_the_example_is_a_valid_manifest(self):
        manifest = load_studio_manifest(EXAMPLE_MANIFEST)

        assert manifest.width == 1920
        assert manifest.height == 1080
        assert manifest.fps == 25
        assert manifest.display_zone.zone_id == "main_wall"

    def test_the_example_can_never_pass_readiness(self):
        manifest = load_studio_manifest(EXAMPLE_MANIFEST)

        problems = studio_readiness_problems(manifest)

        assert problems
        assert any("placeholder" in problem for problem in problems)
        with pytest.raises(StudioNotReadyError):
            require_studio_ready(manifest)


class TestOtherProfilesAreUntouched:
    """The studio belongs to ALMANYA24 alone.

    Nothing here may become a precondition for the Bitcoin podcast or for the
    D-ID path, which is why the profile that has no studio must resolve its
    anchor configuration without ever looking for a manifest.
    """

    def test_the_bitcoin_podcast_profile_declares_no_studio(self):
        from btcedu.config import Settings
        from btcedu.core.anchor_config import load_profile_anchor_config, parse_studio

        raw = load_profile_anchor_config("bitcoin_podcast", Settings())

        studio = parse_studio(raw.get("studio"))
        assert studio.asset_dir == ""
        assert studio.manifest == "manifest.json"

    def test_almanya24_points_at_the_shipped_studio_directory(self):
        from btcedu.config import Settings
        from btcedu.core.anchor_config import load_profile_anchor_config, parse_studio

        raw = load_profile_anchor_config("tagesschau_tr", Settings())

        studio = parse_studio(raw.get("studio"))
        assert studio.asset_dir == "assets/almanya24/studio"
        # Phase 1 has not happened, so the configured manifest is absent and the
        # studio fails closed rather than silently compositing into nothing.
        assert not (EXAMPLE_MANIFEST.parent / studio.manifest).exists()

    def test_loading_a_manifest_never_shells_out(self, monkeypatch):
        import subprocess

        def explode(*args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("the studio manifest must not shell out")

        monkeypatch.setattr(subprocess, "run", explode)
        manifest = load_studio_manifest(EXAMPLE_MANIFEST)

        assert manifest.studio_version.endswith("placeholder")


class TestContentHash:
    def test_identical_manifests_hash_identically(self, tmp_path):
        first = load_studio_manifest(_write_studio(tmp_path / "a", _valid_manifest()))
        second = load_studio_manifest(_write_studio(tmp_path / "b", _valid_manifest()))

        assert studio_content_hash(first) == studio_content_hash(second)

    @pytest.mark.parametrize(
        "mutate",
        [
            lambda d: d.update(studio_version="9.9.9"),
            lambda d: d.update(asset_version="9.9.9"),
            lambda d: d.update(fps=30),
            lambda d: d["presenter"].update(scale=0.9),
            lambda d: d["display_zone"]["rect"].update(x=900),
            lambda d: d.update(background={"path": "plate/bg.png", "sha256": "a" * 64}),
        ],
    )
    def test_every_visible_change_changes_the_hash(self, tmp_path, mutate):
        base = load_studio_manifest(_write_studio(tmp_path / "base", _valid_manifest()))
        changed_data = _valid_manifest()
        mutate(changed_data)
        changed = load_studio_manifest(_write_studio(tmp_path / "changed", changed_data))

        assert studio_content_hash(base) != studio_content_hash(changed)

    def test_the_studio_name_is_not_part_of_the_hash(self, tmp_path):
        base = load_studio_manifest(_write_studio(tmp_path / "base", _valid_manifest()))
        renamed_data = _valid_manifest()
        renamed_data["name"] = "Renamed, same studio"
        renamed = load_studio_manifest(_write_studio(tmp_path / "renamed", renamed_data))

        assert studio_content_hash(base) == studio_content_hash(renamed)


class TestRect:
    def test_touching_rectangles_do_not_overlap(self):
        assert not Rect(0, 0, 10, 10).overlaps(Rect(10, 0, 10, 10))

    def test_intersecting_rectangles_overlap(self):
        assert Rect(0, 0, 10, 10).overlaps(Rect(9, 9, 10, 10))


def test_parse_accepts_a_dict_without_touching_disk(tmp_path):
    manifest = parse_studio_manifest(_valid_manifest(), tmp_path)
    assert manifest.base_dir == tmp_path
