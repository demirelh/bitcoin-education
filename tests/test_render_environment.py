"""WP-8C: the system inputs a render cannot bind by hash.

The point of these tests is the boundary the module draws: the environment is
recorded as evidence, and it must *not* become part of the artefact's identity,
because that would make every remote render permanently stale.
"""

import json
from unittest.mock import patch

from btcedu.core import render_environment as env


class TestDescribingTheMachine:
    def test_the_expected_facts_are_present(self):
        described = env.describe("NotoSans-Bold")
        for key in (
            "origin",
            "renderer_version",
            "ffmpeg_version",
            "ffprobe_version",
            "filters",
            "encoders",
            "decoders",
            "font",
            "platform",
        ):
            assert key in described, key
        assert described["origin"] == "local"
        assert set(described["platform"]) == {"system", "machine", "python"}

    def test_nothing_operational_leaks_into_the_artefact(self):
        """A manifest is shipped to a runner and shown in a dashboard."""
        described = json.dumps(env.describe("NotoSans-Bold")).lower()
        import getpass
        import socket

        assert socket.gethostname().lower() not in described
        assert f"/home/{getpass.getuser().lower()}" not in described

    def test_a_font_that_resolves_to_a_file_is_marked_as_such(self):
        with patch(
            "btcedu.services.ffmpeg_service.find_font_path", return_value="/usr/share/fonts/x.ttf"
        ):
            env._probe.cache_clear()
            described = env.describe("X")
        assert described["font"]["resolution"] == "file"
        assert described["font"]["resolved"] == "/usr/share/fonts/x.ttf"
        env._probe.cache_clear()

    def test_a_font_left_to_fontconfig_is_marked_as_such(self):
        """A bare name is resolved by whichever machine draws the frame."""
        with patch("btcedu.services.ffmpeg_service.find_font_path", return_value="NotoSans-Bold"):
            env._probe.cache_clear()
            described = env.describe("NotoSans-Bold")
        assert described["font"]["resolution"] == "fontconfig"
        env._probe.cache_clear()

    def test_a_missing_binary_is_reported_not_raised(self):
        with patch("btcedu.core.render_environment.subprocess.run", side_effect=FileNotFoundError):
            env._probe.cache_clear()
            described = env.describe("X")
        assert described["ffmpeg_version"] == "unknown"
        assert described["ffprobe_version"] == "unknown"
        assert described["filters"] == []
        env._probe.cache_clear()

    def test_only_the_names_we_care_about_are_listed(self):
        described = env.describe("")
        assert set(described["filters"]) <= set(env.RELEVANT_FILTERS)
        assert set(described["encoders"]) <= set(env.RELEVANT_ENCODERS)


class TestSpellingOutTheDifferences:
    def _base(self):
        return {
            "renderer_version": "0.1.0",
            "ffmpeg_version": "ffmpeg version 6.1.1",
            "ffprobe_version": "ffprobe version 6.1.1",
            "filters": ["drawtext"],
            "encoders": ["libx264"],
            "decoders": ["h264"],
            "font": {"resolved": "/usr/share/fonts/a.ttf"},
            "platform": {"system": "Linux", "machine": "aarch64", "python": "3.12.0"},
        }

    def test_two_identical_machines_differ_in_nothing(self):
        assert env.differences(self._base(), self._base()) == []

    def test_a_different_ffmpeg_is_named(self):
        other = self._base()
        other["ffmpeg_version"] = "ffmpeg version 7.0"
        assert any("ffmpeg_version" in line for line in env.differences(self._base(), other))

    def test_a_different_font_file_is_named(self):
        other = self._base()
        other["font"] = {"resolved": "/usr/share/fonts/b.ttf"}
        assert any(line.startswith("font:") for line in env.differences(self._base(), other))

    def test_a_different_architecture_is_named(self):
        other = self._base()
        other["platform"] = {"system": "Linux", "machine": "x86_64", "python": "3.12.0"}
        assert any(line.startswith("platform:") for line in env.differences(self._base(), other))

    def test_a_missing_side_is_not_a_difference(self):
        assert env.differences(None, self._base()) == []


class TestARemoteResultStaysRecognisableAsRemote:
    def test_the_runners_description_is_relabelled_and_kept(self, tmp_path):
        from btcedu.core.remote_render import _record_remote_origin

        manifest_path = tmp_path / "render_manifest.json"
        provenance_path = tmp_path / "provenance.json"
        runner = {
            "origin": "local",
            "ffmpeg_version": "ffmpeg version 7.0-runner",
            "font": {"name": "NotoSans-Bold", "resolved": "/usr/share/fonts/runner.ttf"},
            "platform": {"system": "Linux", "machine": "x86_64", "python": "3.12.0"},
        }
        manifest = {"episode_id": "ep001", "render_environment": runner}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        provenance_path.write_text(json.dumps({"stage": "render"}), encoding="utf-8")

        _record_remote_origin(manifest, manifest_path, provenance_path)

        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert stored["render_origin"] == "remote"
        assert stored["render_environment"]["origin"] == "remote"
        assert stored["render_environment"]["ffmpeg_version"] == "ffmpeg version 7.0-runner"
        assert stored["local_render_environment"]["origin"] == "local"
        # A rebased remote result must not read as if this machine drew it.
        assert stored["render_environment_differences"]

        provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
        assert provenance["render_origin"] == "remote"
        assert provenance["render_environment"]["origin"] == "remote"

    def test_a_result_without_a_description_is_still_marked_remote(self, tmp_path):
        from btcedu.core.remote_render import _record_remote_origin

        manifest_path = tmp_path / "render_manifest.json"
        manifest = {"episode_id": "ep001"}
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        _record_remote_origin(manifest, manifest_path, tmp_path / "absent.json")

        stored = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert stored["render_origin"] == "remote"
        assert stored["render_environment"] == {"origin": "remote"}


class TestTheEnvironmentIsNotTheIdentity:
    def test_the_content_hash_ignores_the_machine(self, tmp_path):
        """If it did not, a remote render could never be current."""
        import inspect as _inspect

        from btcedu.core.renderer import _compute_render_content_hash

        source = _inspect.getsource(_compute_render_content_hash)
        assert "render_environment" not in source
        assert "ffmpeg_version" not in source
        assert "platform" not in source
