"""WP-8C: the guard that asks what a render actually opened.

``render_inputs`` records what the pipeline believes it will use. These tests
cover the other half: reading every ffmpeg command back and refusing a file
that nobody measured.
"""

import threading

import pytest

from btcedu.core import render_guard
from btcedu.core.render_guard import (
    UnknownRenderInputError,
    arm,
    arm_now,
    command_file_inputs,
    disarm_now,
    disarmed,
    inspect,
    is_armed,
    system_inputs_seen,
)


@pytest.fixture(autouse=True)
def _never_leak_an_armed_guard():
    """A leaked arming would fail unrelated tests in confusing ways."""
    yield
    disarm_now()


def _file(directory, name, body=b"x"):
    path = directory / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


class TestReadingACommandBack:
    def test_an_input_file_is_found(self, tmp_path):
        picture = _file(tmp_path, "chapter01.png")
        assert command_file_inputs(["ffmpeg", "-i", str(picture), "out.mp4"]) == [str(picture)]

    def test_a_synthetic_source_is_not_a_file(self, tmp_path):
        cmd = ["ffmpeg", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo", "out.wav"]
        assert command_file_inputs(cmd) == []

    def test_a_missing_path_is_ffmpegs_error_not_ours(self, tmp_path):
        cmd = ["ffmpeg", "-i", str(tmp_path / "gone.png"), "out.mp4"]
        assert command_file_inputs(cmd) == []

    def test_a_concat_list_pulls_in_what_it_names(self, tmp_path):
        first = _file(tmp_path, "seg1.mp4")
        second = _file(tmp_path, "seg2.mp4")
        listing = tmp_path / "concat.txt"
        listing.write_text(f"file '{first}'\nfile 'seg2.mp4'\n", encoding="utf-8")

        found = command_file_inputs(["ffmpeg", "-f", "concat", "-i", str(listing), "out.mp4"])

        assert str(listing) in found
        assert str(first) in found
        assert str(tmp_path / "seg2.mp4") in found
        assert str(second) == str(tmp_path / "seg2.mp4")

    def test_a_filter_may_name_its_own_typeface(self, tmp_path):
        font = _file(tmp_path, "Inter.ttf")
        cmd = ["ffmpeg", "-vf", f"drawtext=fontfile={font}:text='hi':x=10", "out.mp4"]
        assert command_file_inputs(cmd) == [str(font)]

    def test_an_escaped_colon_stays_part_of_the_path(self, tmp_path):
        font = _file(tmp_path, "od:d.ttf")
        escaped = str(font).replace(":", "\\:")
        cmd = ["ffmpeg", "-vf", f"drawtext=fontfile={escaped}:text=hi", "out.mp4"]
        assert command_file_inputs(cmd) == [str(font)]

    def test_a_filter_may_name_its_own_source(self, tmp_path):
        clip = _file(tmp_path, "overlay.mov")
        cmd = ["ffmpeg", "-filter_complex", f"movie={clip}[ov]", "out.mp4"]
        assert command_file_inputs(cmd) == [str(clip)]

    def test_an_output_path_is_not_an_input(self, tmp_path):
        source = _file(tmp_path, "in.mp4")
        target = _file(tmp_path, "out.mp4")
        assert command_file_inputs(["ffmpeg", "-i", str(source), str(target)]) == [str(source)]


class TestWhatTheGuardAllows:
    def test_nothing_is_checked_while_disarmed(self, tmp_path):
        stranger = _file(tmp_path, "elsewhere/photo.png")
        assert not is_armed()
        inspect(["ffmpeg", "-i", str(stranger), "out.mp4"])

    def test_a_measured_input_passes(self, tmp_path):
        picture = _file(tmp_path, "images/chapter01.png")
        with arm(inventory=[picture], work_roots=[tmp_path / "render"]):
            inspect(["ffmpeg", "-i", str(picture), "out.mp4"])

    def test_an_intermediate_of_this_render_passes(self, tmp_path):
        segment = _file(tmp_path, "render/segments/seg01.mp4")
        with arm(inventory=[], work_roots=[tmp_path / "render"]):
            inspect(["ffmpeg", "-i", str(segment), "out.mp4"])

    def test_a_declared_system_input_passes_and_is_recorded(self, tmp_path):
        font = _file(tmp_path, "sys/fonts/DejaVu.ttf")
        with arm(
            inventory=[],
            work_roots=[tmp_path / "render"],
            system_roots=[tmp_path / "sys"],
        ):
            inspect(["ffmpeg", "-vf", f"drawtext=fontfile={font}", "out.mp4"])
            assert system_inputs_seen() == [str(font)]

    def test_a_path_variant_of_a_measured_input_passes(self, tmp_path):
        picture = _file(tmp_path, "images/chapter01.png")
        detour = tmp_path / "images" / ".." / "images" / "chapter01.png"
        with arm(inventory=[picture], work_roots=[tmp_path / "render"]):
            inspect(["ffmpeg", "-i", str(detour), "out.mp4"])


class TestWhatTheGuardRefuses:
    def test_an_unmeasured_file_stops_the_render(self, tmp_path):
        stranger = _file(tmp_path, "elsewhere/photo.png")
        with arm(inventory=[], work_roots=[tmp_path / "render"], episode_id="ep001"):
            with pytest.raises(UnknownRenderInputError) as excinfo:
                inspect(["ffmpeg", "-i", str(stranger), "out.mp4"])
        assert "ep001" in str(excinfo.value)
        assert "photo.png" in str(excinfo.value)

    def test_a_swapped_picture_from_outside_the_episode_stops_the_render(self, tmp_path):
        measured = _file(tmp_path, "images/chapter01.png")
        smuggled = _file(tmp_path, "tmp/chapter01.png")
        with arm(inventory=[measured], work_roots=[tmp_path / "render"]):
            with pytest.raises(UnknownRenderInputError):
                inspect(["ffmpeg", "-i", str(smuggled), "out.mp4"])

    def test_a_concat_list_cannot_smuggle_a_segment_in(self, tmp_path):
        listing = tmp_path / "render" / "concat.txt"
        listing.parent.mkdir(parents=True, exist_ok=True)
        stranger = _file(tmp_path, "elsewhere/seg.mp4")
        listing.write_text(f"file '{stranger}'\n", encoding="utf-8")
        with arm(inventory=[], work_roots=[tmp_path / "render"]):
            with pytest.raises(UnknownRenderInputError):
                inspect(["ffmpeg", "-f", "concat", "-i", str(listing), "out.mp4"])

    def test_an_undeclared_font_stops_the_render(self, tmp_path):
        font = _file(tmp_path, "home/operator/Fancy.ttf")
        with arm(
            inventory=[],
            work_roots=[tmp_path / "render"],
            system_roots=[tmp_path / "sys"],
        ):
            with pytest.raises(UnknownRenderInputError):
                inspect(["ffmpeg", "-vf", f"drawtext=fontfile={font}", "out.mp4"])


class TestArmingAndDisarming:
    def test_the_context_manager_restores_the_previous_state(self, tmp_path):
        assert not is_armed()
        with arm(inventory=[], work_roots=[tmp_path]):
            assert is_armed()
        assert not is_armed()

    def test_arm_now_needs_an_explicit_disarm(self, tmp_path):
        arm_now(inventory=[], work_roots=[tmp_path])
        assert is_armed()
        disarm_now()
        assert not is_armed()

    def test_a_probe_can_step_outside_the_guard(self, tmp_path):
        stranger = _file(tmp_path, "elsewhere/photo.png")
        with arm(inventory=[], work_roots=[tmp_path / "render"]):
            with disarmed():
                inspect(["ffprobe", "-i", str(stranger)])
            with pytest.raises(UnknownRenderInputError):
                inspect(["ffmpeg", "-i", str(stranger), "out.mp4"])

    def test_a_nested_arming_restores_the_outer_inventory(self, tmp_path):
        outer = _file(tmp_path, "images/outer.png")
        inner = _file(tmp_path, "images/inner.png")
        with arm(inventory=[outer], work_roots=[tmp_path / "render"]):
            with arm(inventory=[inner], work_roots=[tmp_path / "render"]):
                with pytest.raises(UnknownRenderInputError):
                    inspect(["ffmpeg", "-i", str(outer), "out.mp4"])
            inspect(["ffmpeg", "-i", str(outer), "out.mp4"])

    def test_another_thread_is_not_guarded(self, tmp_path):
        """The job manager renders in threads; arming must not leak between them."""
        stranger = _file(tmp_path, "elsewhere/photo.png")
        seen: list[bool] = []

        def _elsewhere():
            seen.append(is_armed())
            inspect(["ffmpeg", "-i", str(stranger), "out.mp4"])

        with arm(inventory=[], work_roots=[tmp_path / "render"]):
            worker = threading.Thread(target=_elsewhere)
            worker.start()
            worker.join()

        assert seen == [False]


class TestArmingFromARecordedBlock:
    """``_arm_render_guard`` turns the manifest's root/relative pairs back into
    the absolute paths of this machine — the block deliberately stores no
    absolute path so it means the same thing on the Pi and on a runner."""

    def _settings(self, tmp_path):
        return type("S", (), {"outputs_dir": str(tmp_path / "outputs")})()

    def test_a_legacy_block_leaves_the_guard_disarmed(self, tmp_path):
        from btcedu.core.renderer import _arm_render_guard

        _arm_render_guard(None, tmp_path, "ep001", self._settings(tmp_path), None)
        assert not is_armed()

    def test_a_recorded_entry_becomes_an_allowed_path(self, tmp_path):
        from btcedu.core.render_inputs import ROOT_EPISODE, ROOT_SYSTEM
        from btcedu.core.renderer import _arm_render_guard

        settings = self._settings(tmp_path)
        episode_dir = tmp_path / "outputs" / "ep001"
        picture = _file(episode_dir, "images/chapter01.png")
        font = _file(tmp_path, "sysfont/DejaVu.ttf")
        block = {
            "entries": [
                {"root": ROOT_EPISODE, "path": "images/chapter01.png"},
                {"root": ROOT_SYSTEM, "path": str(font)},
            ]
        }

        _arm_render_guard(block, episode_dir, "ep001", settings, None)
        try:
            inspect(["ffmpeg", "-i", str(picture), "out.mp4"])
            inspect(["ffmpeg", "-i", str(font), "out.mp4"])
            with pytest.raises(UnknownRenderInputError):
                inspect(["ffmpeg", "-i", str(_file(tmp_path, "other/x.png")), "out.mp4"])
        finally:
            disarm_now()

    def test_this_renders_own_scratch_space_is_allowed(self, tmp_path):
        from btcedu.core.render_inputs import ROOT_EPISODE
        from btcedu.core.renderer import _arm_render_guard

        settings = self._settings(tmp_path)
        episode_dir = tmp_path / "outputs" / "ep001"
        _file(episode_dir, "images/chapter01.png")
        levelled = _file(episode_dir, "render/inputs/intro.mp3")
        block = {"entries": [{"root": ROOT_EPISODE, "path": "images/chapter01.png"}]}

        _arm_render_guard(block, episode_dir, "ep001", settings, None)
        try:
            inspect(["ffmpeg", "-i", str(levelled), "out.mp4"])
        finally:
            disarm_now()


class TestTheHookInFfmpegService:
    def test_every_ffmpeg_call_passes_through_the_guard(self, tmp_path, monkeypatch):
        """The guard is only worth anything if _run_ffmpeg really consults it."""
        from btcedu.services import ffmpeg_service

        stranger = _file(tmp_path, "elsewhere/photo.png")
        monkeypatch.setattr(
            ffmpeg_service.subprocess,
            "run",
            lambda *a, **k: pytest.fail("ffmpeg must not start with an unknown input"),
        )
        with arm(inventory=[], work_roots=[tmp_path / "render"]):
            with pytest.raises(UnknownRenderInputError):
                ffmpeg_service._run_ffmpeg(
                    ["ffmpeg", "-i", str(stranger), str(tmp_path / "out.mp4")], 30
                )

    def test_the_module_declares_its_system_roots(self):
        assert "/usr/share/fonts" in render_guard.DEFAULT_SYSTEM_ROOTS
