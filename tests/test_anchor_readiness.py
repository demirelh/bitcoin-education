"""What `btcedu anchor-readiness` must refuse, and what it must let through.

Every fixture here is synthetic. The real profile is checked once, at the end,
to confirm the one thing the command exists to say today: ALMANYA24 is not
ready, because asset phase 1 has not happened.
"""

import json
from datetime import date
from pathlib import Path

import pytest

from btcedu.config import Settings
from btcedu.core.anchor_readiness import (
    EXIT_BLOCKED,
    EXIT_READY,
    EXIT_USAGE,
    EXIT_WARNINGS,
    REPORT_SCHEMA_VERSION,
    STATUS_BLOCKED,
    STATUS_PASS,
    STATUS_WARNING,
    evaluate_readiness,
    format_report,
    online_notice,
    resolve_studio_mode,
)

LOOK_ID = "look-real-0001"


def _settings(tmp_path: Path, **overrides) -> Settings:
    base = {
        "outputs_dir": str(tmp_path / "outputs"),
        "render_resolution": "1920x1080",
        "render_fps": 25,
        "anchor_enabled": True,
        "max_episode_cost_usd": 15.0,
        "heygen_api_key": "",
        "heygen_avatar_id": "",
    }
    base.update(overrides)
    return Settings(**base)


def _studio_manifest(studio_dir: Path, **overrides) -> Path:
    studio_dir.mkdir(parents=True, exist_ok=True)
    for name in ("background.png", "fallback.png", "intro.mp4", "loop.mp4", "mask.png"):
        (studio_dir / name).write_bytes(b"\x00" * 64)
    data = {
        "schema_version": 1,
        "studio_version": "1.0.0",
        "asset_version": "1.0.0",
        "name": "ALMANYA24 Studio",
        "width": 1920,
        "height": 1080,
        "fps": 25,
        "alpha_mode": "alpha_webm",
        "background": {"path": "background.png", "kind": "image"},
        "fallback_display_media": {"path": "fallback.png", "kind": "image"},
        "display_zone": {
            "zone_id": "monitor",
            "rect": {"x": 900, "y": 200, "width": 800, "height": 450},
            "fit_mode": "cover",
            "focus_point": [0.5, 0.5],
            "presenter_free": True,
        },
        "presenter": {"anchor_x": 600, "anchor_y": 1080, "scale": 1.0},
        "logo_zone": {"x": 40, "y": 40, "width": 200, "height": 80},
        "safe_areas": {
            "lower_third": {"x": 100, "y": 820, "width": 1000, "height": 140},
            "ticker": {"x": 0, "y": 980, "width": 1920, "height": 100},
            "subtitle": {"x": 200, "y": 900, "width": 1520, "height": 80},
        },
    }
    data.update(overrides)
    path = studio_dir / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _rights_record(path: Path, **overrides) -> Path:
    data = {
        "schema_version": 1,
        "record_version": 2,
        "updated_at": "2026-01-31T12:00:00Z",
        "presenter_rights_id": "presenter-01",
        "consent_confirmed": True,
        "voice_likeness_confirmed": True,
        "synthetic_video_confirmed": True,
        "permitted_channels": ["almanya24-youtube"],
        "permitted_territories": ["DE"],
        "valid_from": "2026-01-01",
        "valid_until": "2030-01-01",
        "revoked": False,
        "contract_reference": "CONTRACT-1",
        "operator_approval": {
            "approved": True,
            "approved_by_ref": "ops-hd",
            "approved_on": "2026-01-01",
        },
        "ai_disclosure_text": "Sunucu yapay zeka ile olusturulmustur.",
    }
    data.update(overrides)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def _anchor_block(studio_dir: Path, rights_file: Path, **overrides) -> dict:
    block = {
        "provider": "heygen",
        "engine": "avatar_iii",
        "avatar_type": "digital_twin",
        "output_format": "webm",
        "studio_mode": "composite",
        "resolution": "1080p",
        "aspect_ratio": "16:9",
        "cost_per_second_usd": 0.0167,
        "cost_source": "HeyGen price list",
        "cost_checked_on": "2026-09-05",
        "max_cost_usd": 7.0,
        "max_concurrent_jobs": 4,
        "rotation_strategy": "least_recently_used",
        "looks": [
            {"name": "look_01", "avatar_look_id": LOOK_ID, "active": True},
            {"name": "look_02", "avatar_look_id": "look-real-0002", "active": True},
        ],
        "studio": {
            "asset_dir": str(studio_dir),
            "manifest": "manifest.json",
            "required_version": 1,
        },
        "rights": {
            "consent_documented": True,
            "record_file": str(rights_file),
            "channel": "almanya24-youtube",
            "territory": "DE",
            "ai_disclosure_required": True,
        },
    }
    block.update(overrides)
    return block


class _FakeProfile:
    def __init__(self, anchor: dict):
        self.stage_config = {"anchor": anchor}


class _FakeRegistry:
    def __init__(self, profile):
        self._profile = profile

    def get(self, name):
        del name
        return self._profile


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A complete, ready deployment that individual tests then break."""
    studio_dir = tmp_path / "studio"
    _studio_manifest(studio_dir)
    rights_file = _rights_record(tmp_path / "rights" / "anchor-rights.json")
    state = {"anchor": _anchor_block(studio_dir, rights_file)}

    def fake_registry(settings):
        del settings
        return _FakeRegistry(_FakeProfile(state["anchor"]))

    monkeypatch.setattr("btcedu.profiles.get_registry", fake_registry)
    # The real profile YAML is read separately for publish settings; point the
    # readiness check at a synthetic one so a profile edit cannot silently
    # change what these tests assert.
    monkeypatch.setattr(
        "btcedu.core.anchor_readiness._profile_dict",
        lambda profile, settings: {
            "auto_publish": False,
            "youtube": {
                "publish_target": "test",
                "targets": {"test": {"default_privacy": "private"}},
            },
        },
    )
    return {
        "tmp_path": tmp_path,
        "studio_dir": studio_dir,
        "rights_file": rights_file,
        "state": state,
        "settings": _settings(tmp_path),
    }


def _run(world, **kwargs):
    kwargs.setdefault("today", date(2026, 6, 1))
    return evaluate_readiness("almanya24_test", world["settings"], **kwargs)


def _status(report, check_id: str) -> str:
    for result in report.results:
        if result.check_id == check_id:
            return result.status
    raise AssertionError(f"No check {check_id!r} in report: {[r.check_id for r in report.results]}")


class TestAFullyReadyConfiguration:
    def test_nothing_blocks(self, world):
        report = _run(world)

        assert [r.check_id for r in report.blocked] == []

    def test_the_exit_code_reflects_readiness(self, world):
        report = _run(world)

        assert report.exit_code in (EXIT_READY, EXIT_WARNINGS)
        assert report.exit_code != EXIT_BLOCKED

    def test_every_area_is_covered(self, world):
        report = _run(world)

        areas = {result.area for result in report.results}
        assert {"configuration", "studio", "pipeline", "rights"} <= areas


class TestConfigurationFaults:
    def test_a_placeholder_look_blocks(self, world):
        world["state"]["anchor"]["looks"] = [
            {"name": "look_01", "avatar_look_id": "REPLACE_WITH_HEYGEN_LOOK_ID", "active": False},
        ]

        report = _run(world)

        assert _status(report, "config.no_placeholder_looks") == STATUS_BLOCKED
        assert _status(report, "config.active_look") == STATUS_BLOCKED

    def test_a_duplicate_look_id_blocks(self, world):
        world["state"]["anchor"]["looks"] = [
            {"name": "look_01", "avatar_look_id": LOOK_ID, "active": False},
            {"name": "look_02", "avatar_look_id": LOOK_ID, "active": True},
        ]

        report = _run(world)

        assert _status(report, "config.look_ids_unique") == STATUS_BLOCKED

    def test_a_duplicate_look_name_blocks(self, world):
        world["state"]["anchor"]["looks"] = [
            {"name": "look_01", "avatar_look_id": LOOK_ID, "active": True},
            {"name": "look_01", "avatar_look_id": "look-real-0002", "active": False},
        ]

        # parse_looks rejects duplicate names outright, so the whole anchor
        # block fails to parse. That is stricter than a per-check warning and
        # it is the behaviour the readiness report has to surface.
        report = _run(world)

        assert _status(report, "config.parses") == STATUS_BLOCKED

    def test_no_active_look_blocks(self, world):
        for look in world["state"]["anchor"]["looks"]:
            look["active"] = False

        report = _run(world)

        assert _status(report, "config.active_look") == STATUS_BLOCKED

    def test_the_wrong_engine_blocks(self, world):
        world["state"]["anchor"]["engine"] = "avatar_iv"

        report = _run(world)

        assert _status(report, "config.engine") == STATUS_BLOCKED

    def test_a_non_positive_cost_rate_blocks(self, world):
        world["state"]["anchor"]["cost_per_second_usd"] = 0.0

        report = _run(world)

        # resolve_anchor_config refuses a zero rate outright; the readiness
        # report must therefore block rather than silently skip the section.
        assert _status(report, "config.parses") == STATUS_BLOCKED

    def test_an_implausible_cost_rate_warns(self, world):
        world["state"]["anchor"]["cost_per_second_usd"] = 1.67

        report = _run(world)

        assert _status(report, "config.cost_rate_plausible") == STATUS_WARNING

    def test_the_anchor_budget_is_expected_to_be_seven_dollars(self, world):
        report = _run(world)
        assert _status(report, "config.stage_budget_expected") == STATUS_PASS

        world["state"]["anchor"]["max_cost_usd"] = 3.0
        assert _status(_run(world), "config.stage_budget_expected") == STATUS_WARNING

    def test_a_zero_stage_budget_blocks(self, world):
        world["state"]["anchor"]["max_cost_usd"] = 0.0

        report = _run(world)

        assert _status(report, "config.stage_budget_positive") == STATUS_BLOCKED

    def test_an_episode_budget_below_the_stage_budget_blocks(self, world):
        world["settings"] = _settings(world["tmp_path"], max_episode_cost_usd=3.0)

        report = _run(world)

        assert _status(report, "config.episode_budget_covers_stage") == STATUS_BLOCKED

    def test_a_disabled_anchor_path_is_only_a_warning(self, world):
        world["settings"] = _settings(world["tmp_path"], anchor_enabled=False)

        report = _run(world)

        assert _status(report, "config.anchor_enabled") == STATUS_WARNING

    def test_a_global_avatar_id_without_an_active_look_blocks(self, world):
        for look in world["state"]["anchor"]["looks"]:
            look["active"] = False
        world["settings"] = _settings(world["tmp_path"], heygen_avatar_id="global-avatar")

        report = _run(world)

        assert _status(report, "config.no_global_avatar_fallback") == STATUS_BLOCKED

    def test_a_missing_profile_blocks_and_stops_early(self, world, monkeypatch):
        monkeypatch.setattr("btcedu.profiles.get_registry", lambda s: _FakeRegistry(None))

        report = _run(world)

        assert _status(report, "config.profile_exists") == STATUS_BLOCKED


class TestStudioChecks:
    def test_a_missing_manifest_blocks(self, world):
        (world["studio_dir"] / "manifest.json").unlink()

        report = _run(world)

        assert _status(report, "studio.manifest_present") == STATUS_BLOCKED

    def test_an_invalid_manifest_blocks(self, world):
        (world["studio_dir"] / "manifest.json").write_text("{}", encoding="utf-8")

        report = _run(world)

        assert _status(report, "studio.manifest_valid") == STATUS_BLOCKED

    def test_missing_assets_block(self, world):
        (world["studio_dir"] / "background.png").unlink()

        report = _run(world)

        assert _status(report, "studio.assets_present") == STATUS_BLOCKED

    def test_a_mismatched_frame_blocks(self, world):
        # Internally consistent at 1280x720, so the manifest itself is valid;
        # what is wrong is that the renderer produces a 1920x1080 frame.
        _studio_manifest(
            world["studio_dir"],
            width=1280,
            height=720,
            display_zone={
                "zone_id": "monitor",
                "rect": {"x": 600, "y": 130, "width": 530, "height": 300},
                "fit_mode": "cover",
                "focus_point": [0.5, 0.5],
                "presenter_free": True,
            },
            presenter={"anchor_x": 400, "anchor_y": 720, "scale": 1.0},
            logo_zone={"x": 30, "y": 30, "width": 130, "height": 55},
            safe_areas={
                "lower_third": {"x": 70, "y": 545, "width": 660, "height": 95},
                "ticker": {"x": 0, "y": 650, "width": 1280, "height": 70},
                "subtitle": {"x": 130, "y": 600, "width": 1010, "height": 55},
            },
        )

        report = _run(world)

        assert _status(report, "studio.frame_matches_renderer") == STATUS_BLOCKED

    def test_a_mismatched_frame_rate_blocks(self, world):
        _studio_manifest(world["studio_dir"], fps=30)  # renderer runs at 25

        report = _run(world)

        assert _status(report, "studio.fps_matches_renderer") == STATUS_BLOCKED

    def test_a_missing_fallback_graphic_blocks(self, world):
        data = json.loads((world["studio_dir"] / "manifest.json").read_text(encoding="utf-8"))
        data.pop("fallback_display_media")
        (world["studio_dir"] / "manifest.json").write_text(json.dumps(data), encoding="utf-8")

        report = _run(world)

        assert _status(report, "studio.fallback_graphic") == STATUS_BLOCKED

    def test_an_unknown_studio_mode_blocks(self, world):
        report = _run(world, studio_mode="hologram")

        assert _status(report, "studio.mode_valid") == STATUS_BLOCKED

    def test_the_opaque_mode_needs_a_safe_zone_or_a_mask(self, world):
        data = json.loads((world["studio_dir"] / "manifest.json").read_text(encoding="utf-8"))
        data["alpha_mode"] = "opaque_mp4"
        data["display_zone"]["presenter_free"] = False
        (world["studio_dir"] / "manifest.json").write_text(json.dumps(data), encoding="utf-8")

        report = _run(world, studio_mode="opaque_mp4")

        # The manifest parser refuses this outright, which is stricter than a
        # readiness warning: without a safe zone or a mask the monitor could be
        # drawn over the presenter's face, so the studio never loads at all.
        assert _status(report, "studio.manifest_valid") == STATUS_BLOCKED

    def test_the_opaque_mode_accepts_an_occlusion_mask(self, world):
        data = json.loads((world["studio_dir"] / "manifest.json").read_text(encoding="utf-8"))
        data["alpha_mode"] = "opaque_mp4"
        data["display_zone"]["presenter_free"] = False
        data["occlusion_mask"] = {"path": "mask.png", "kind": "image"}
        (world["studio_dir"] / "manifest.json").write_text(json.dumps(data), encoding="utf-8")

        report = _run(world, studio_mode="opaque_mp4")

        assert _status(report, "studio.opaque_safe_zone") == STATUS_PASS

    def test_a_missing_ffmpeg_blocks(self, world, monkeypatch):
        monkeypatch.setattr("shutil.which", lambda name: None)

        report = _run(world)

        assert _status(report, "ffmpeg.present") == STATUS_BLOCKED

    def test_alpha_decoding_is_checked_only_in_the_alpha_mode(self, world, monkeypatch):
        monkeypatch.setattr("btcedu.core.anchor_readiness._ffmpeg_decoders", lambda: {"h264"})

        blocked = _run(world, studio_mode="alpha_webm")
        assert _status(blocked, "ffmpeg.alpha_decode") == STATUS_BLOCKED

        opaque = _run(world, studio_mode="opaque_mp4")
        assert not any(r.check_id == "ffmpeg.alpha_decode" for r in opaque.results)

    def test_a_missing_perspective_filter_only_warns(self, world, monkeypatch):
        monkeypatch.setattr(
            "btcedu.services.studio_compositor.has_perspective_filter", lambda: False
        )

        report = _run(world)

        assert _status(report, "ffmpeg.perspective") == STATUS_WARNING


class TestRightsGate:
    def test_a_missing_record_blocks(self, world):
        world["state"]["anchor"]["rights"]["record_file"] = ""

        report = _run(world)

        assert _status(report, "rights.record_configured") == STATUS_BLOCKED

    def test_an_absent_record_file_blocks(self, world):
        world["rights_file"].unlink()

        report = _run(world)

        assert _status(report, "rights.record_valid") == STATUS_BLOCKED

    def test_withdrawn_consent_blocks(self, world):
        _rights_record(world["rights_file"], revoked=True, revoked_on="2026-05-01")

        report = _run(world)

        assert _status(report, "rights.permits_generation") == STATUS_BLOCKED

    def test_a_revoked_profile_flag_blocks_before_the_record_is_read(self, world):
        world["state"]["anchor"]["rights"]["revoked"] = True

        report = _run(world)

        assert _status(report, "rights.profile_not_revoked") == STATUS_BLOCKED

    def test_missing_consent_blocks(self, world):
        _rights_record(world["rights_file"], consent_confirmed=False)

        report = _run(world)

        assert _status(report, "rights.permits_generation") == STATUS_BLOCKED

    def test_an_expired_release_blocks(self, world):
        _rights_record(world["rights_file"], valid_until="2026-02-01")

        report = _run(world)

        assert _status(report, "rights.permits_generation") == STATUS_BLOCKED

    def test_a_release_that_has_not_started_blocks(self, world):
        _rights_record(world["rights_file"], valid_from="2027-01-01", valid_until="2030-01-01")

        report = _run(world)

        assert _status(report, "rights.permits_generation") == STATUS_BLOCKED

    def test_the_wrong_channel_blocks(self, world):
        _rights_record(world["rights_file"], permitted_channels=["some-other-channel"])

        report = _run(world)

        assert _status(report, "rights.permits_generation") == STATUS_BLOCKED

    def test_the_wrong_territory_blocks(self, world):
        _rights_record(world["rights_file"], permitted_territories=["US"])

        report = _run(world)

        assert _status(report, "rights.permits_generation") == STATUS_BLOCKED

    def test_worldwide_covers_every_territory(self, world):
        _rights_record(world["rights_file"], permitted_territories=["worldwide"])

        report = _run(world)

        assert _status(report, "rights.permits_generation") == STATUS_PASS

    def test_a_missing_operator_approval_blocks(self, world):
        _rights_record(world["rights_file"], operator_approval={"approved": False})

        report = _run(world)

        assert _status(report, "rights.permits_generation") == STATUS_BLOCKED

    def test_a_missing_ai_disclosure_blocks(self, world):
        _rights_record(world["rights_file"], ai_disclosure_text="")

        report = _run(world)

        assert _status(report, "rights.ai_disclosure") == STATUS_BLOCKED
        assert _status(report, "rights.permits_generation") == STATUS_BLOCKED


class TestOutputAndExitCodes:
    def test_the_json_report_is_versioned(self, world):
        payload = _run(world).to_dict()

        assert payload["schema_version"] == REPORT_SCHEMA_VERSION
        for check in payload["checks"]:
            assert set(check) == {"check_id", "area", "severity", "status", "detail", "remedy"}

    def test_the_exit_code_is_two_when_blocked(self, world):
        world["state"]["anchor"]["engine"] = "avatar_iv"

        assert _run(world).exit_code == EXIT_BLOCKED

    def test_the_exit_code_is_one_for_warnings_only(self, world):
        world["settings"] = _settings(world["tmp_path"], anchor_enabled=False)
        report = _run(world)

        assert not report.blocked
        assert report.exit_code == EXIT_WARNINGS

    def test_the_exit_code_is_zero_when_everything_passes(self, world):
        report = _run(world)
        report.results = [r for r in report.results if r.status == STATUS_PASS]

        assert report.exit_code == EXIT_READY

    def test_no_secret_appears_in_the_text_report(self, world):
        world["settings"] = _settings(
            world["tmp_path"],
            heygen_api_key="hg-super-secret-key",
            anthropic_api_key="sk-ant-secret",
        )

        text = format_report(_run(world))

        assert "hg-super-secret-key" not in text
        assert "sk-ant-secret" not in text

    def test_no_secret_appears_in_the_json_report(self, world):
        world["settings"] = _settings(world["tmp_path"], heygen_api_key="hg-super-secret-key")

        payload = json.dumps(_run(world).to_dict())

        assert "hg-super-secret-key" not in payload

    def test_the_text_report_groups_by_area(self, world):
        text = format_report(_run(world))

        for area in ("[CONFIGURATION]", "[STUDIO]", "[PIPELINE]", "[RIGHTS]"):
            assert area in text

    def test_the_studio_mode_follows_the_profile_when_unspecified(self, world):
        from btcedu.core.anchor_config import resolve_anchor_config

        config = resolve_anchor_config("almanya24_test", world["settings"])

        assert resolve_studio_mode(config, None) == "alpha_webm"
        assert resolve_studio_mode(config, "opaque_mp4") == "opaque_mp4"

    def test_the_online_notice_promises_no_purchase(self, world):
        notice = online_notice()

        assert "read-only" in notice
        assert "no video is generated" in notice


class TestOfflineMode:
    def test_the_default_run_touches_no_network(self, world, monkeypatch):
        def explode(*args, **kwargs):
            raise AssertionError("the offline readiness run made a network request")

        monkeypatch.setattr("requests.Session.get", explode)
        monkeypatch.setattr("requests.Session.post", explode)
        monkeypatch.setattr("requests.get", explode)
        monkeypatch.setattr("requests.post", explode)

        report = _run(world)

        assert report.online is False
        assert not any(r.area == "provider" for r in report.results)


class TestTheRealProfileToday:
    """Phase 1 has not happened, so the honest answer is 'blocked'."""

    def test_the_shipped_profile_is_blocked_with_actionable_reasons(self, tmp_path):
        settings = _settings(tmp_path, anchor_enabled=False)

        report = evaluate_readiness("tagesschau_tr", settings, studio_mode="alpha_webm")

        assert report.exit_code == EXIT_BLOCKED
        blocked = {r.check_id for r in report.blocked}
        assert "config.no_placeholder_looks" in blocked
        assert "studio.manifest_present" in blocked
        for result in report.blocked:
            assert result.remedy, f"{result.check_id} blocks without telling anyone what to do"

    def test_other_profiles_are_not_broken_by_the_checker(self, tmp_path):
        settings = _settings(tmp_path)

        report = evaluate_readiness("bitcoin_podcast", settings)

        # bitcoin_podcast uses D-ID, so it is legitimately "not HeyGen". The
        # point is that asking the question neither crashes nor mutates it.
        assert report.profile == "bitcoin_podcast"
        assert any(r.check_id == "config.provider" for r in report.results)


class TestTheCommandLine:
    """The exit codes an operator or a CI job will actually branch on."""

    def _invoke(self, tmp_path, args, **settings_kwargs):
        from click.testing import CliRunner

        from btcedu import cli as cli_module

        settings = _settings(tmp_path, **settings_kwargs)

        class _Factory:
            def __call__(self):
                raise RuntimeError("readiness must not need the production database")

        return CliRunner().invoke(
            cli_module.cli,
            args,
            obj={"settings": settings, "session_factory": _Factory()},
        )

    def test_the_shipped_profile_exits_two(self, tmp_path):
        result = self._invoke(
            tmp_path, ["anchor-readiness", "--profile", "tagesschau_tr"], anchor_enabled=False
        )

        assert result.exit_code == EXIT_BLOCKED
        assert "BLOCKED" in result.output

    def test_an_unknown_profile_is_reported_not_merely_rejected(self, tmp_path):
        result = self._invoke(tmp_path, ["anchor-readiness", "--profile", "no_such_profile"])

        # A misspelled profile is a blocking finding with a remedy rather than
        # a bare usage error: the operator gets the same report shape either way.
        assert result.exit_code == EXIT_BLOCKED
        assert "config.profile_exists" in result.output

    def test_an_evaluation_that_cannot_run_exits_three(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "btcedu.core.anchor_readiness.evaluate_readiness",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("registry unreadable")),
        )

        result = self._invoke(tmp_path, ["anchor-readiness", "--profile", "tagesschau_tr"])

        assert result.exit_code == EXIT_USAGE

    def test_an_invalid_studio_mode_is_refused_by_click(self, tmp_path):
        result = self._invoke(
            tmp_path,
            ["anchor-readiness", "--profile", "tagesschau_tr", "--studio-mode", "hologram"],
        )

        assert result.exit_code != 0
        assert "hologram" in result.output

    def test_the_json_report_is_versioned_and_parses(self, tmp_path):
        result = self._invoke(
            tmp_path,
            ["anchor-readiness", "--profile", "tagesschau_tr", "--json"],
            anchor_enabled=False,
        )

        payload = json.loads(result.output)
        assert payload["schema_version"] >= 1
        assert payload["exit_code"] == EXIT_BLOCKED
        assert all(
            {"check_id", "severity", "status", "remedy"} <= set(c) for c in payload["checks"]
        )

    def test_readiness_needs_no_database(self, tmp_path):
        # The factory in _invoke raises; a broken or absent database must not
        # stop an operator from asking what is missing.
        result = self._invoke(
            tmp_path, ["anchor-readiness", "--profile", "tagesschau_tr"], anchor_enabled=False
        )

        assert result.exit_code == EXIT_BLOCKED

    def test_the_online_promise_is_printed_before_any_request(self, tmp_path, monkeypatch):
        calls = []

        class _Refuser:
            def __init__(self, *args, **kwargs):
                calls.append(args)
                raise AssertionError("no request may be made in this test")

        monkeypatch.setattr("btcedu.services.heygen_readonly.HeyGenReadOnlyClient", _Refuser)
        result = self._invoke(
            tmp_path,
            ["anchor-readiness", "--profile", "tagesschau_tr", "--online"],
            anchor_enabled=False,
        )

        assert "no video is generated" in result.output.lower()
