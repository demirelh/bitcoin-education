"""Dry-run tests for H-1 ffmpeg smoke-test helpers.

These tests verify command construction only — no ffmpeg binary required.
Real execution is validated manually via `btcedu smoke-test-video` on the
Raspberry Pi.
"""

from pathlib import Path

import pytest

from btcedu.services.ffmpeg_service import generate_silent_audio, generate_test_video


class TestGenerateTestVideoCommand:
    def test_uses_testsrc2_filter(self, tmp_path):
        """ffmpeg command must use the testsrc2 lavfi source."""
        out = str(tmp_path / "test.mp4")
        result = generate_test_video(out, duration=2.0, resolution="1920x1080", dry_run=True)
        cmd = result.ffmpeg_command
        cmd_str = " ".join(cmd)
        assert "testsrc2" in cmd_str

    def test_uses_libx264_yuv420p(self, tmp_path):
        """ffmpeg command must encode to libx264 with yuv420p pixel format."""
        out = str(tmp_path / "test.mp4")
        result = generate_test_video(out, dry_run=True)
        cmd = result.ffmpeg_command
        assert "libx264" in cmd
        assert "yuv420p" in cmd

    def test_dry_run_touches_output_file(self, tmp_path):
        """Dry-run mode creates an empty output file (no ffmpeg needed)."""
        out = str(tmp_path / "test.mp4")
        result = generate_test_video(out, dry_run=True)
        assert Path(result.segment_path).exists()
        assert result.returncode == 0
        assert result.size_bytes == 0


class TestGenerateSilentAudioCommand:
    def test_uses_anullsrc_filter(self, tmp_path):
        """ffmpeg command must use the anullsrc lavfi source."""
        out = str(tmp_path / "silent.m4a")
        result = generate_silent_audio(out, duration=2.0, dry_run=True)
        cmd_str = " ".join(result.ffmpeg_command)
        assert "anullsrc" in cmd_str

    def test_uses_aac_codec(self, tmp_path):
        """ffmpeg command must encode to AAC audio."""
        out = str(tmp_path / "silent.m4a")
        result = generate_silent_audio(out, dry_run=True)
        assert "aac" in result.ffmpeg_command

    def test_dry_run_touches_output_file(self, tmp_path):
        """Dry-run mode creates an empty output file."""
        out = str(tmp_path / "silent.m4a")
        result = generate_silent_audio(out, dry_run=True)
        assert Path(result.segment_path).exists()
        assert result.returncode == 0


class TestSmokeTestIntegrationDryRun:
    def test_full_sequence_dry_run_no_exception(self, tmp_path):
        """Full smoke-test sequence completes without exceptions in dry_run=True mode.

        Covers the same steps as `btcedu smoke-test-video` but with all
        ffmpeg calls dry-run'd so no binary is required.
        """
        from btcedu.services.ffmpeg_service import (
            create_video_segment,
            normalize_video_clip,
        )

        raw_video = str(tmp_path / "raw_test.mp4")
        norm_video = str(tmp_path / "normalized.mp4")
        silent_audio = str(tmp_path / "silent.m4a")
        segment_out = str(tmp_path / "segment.mp4")

        # Step 1: generate test video (dry_run)
        r1 = generate_test_video(raw_video, duration=2.0, dry_run=True)
        assert r1.returncode == 0

        # Step 2: normalize (dry_run — input must exist since normalize checks it)
        Path(raw_video).touch()  # already created by step 1 dry_run
        r2 = normalize_video_clip(
            input_path=raw_video,
            output_path=norm_video,
            resolution="1920x1080",
            dry_run=True,
        )
        assert r2.returncode == 0

        # Step 3: generate silent audio (dry_run)
        r3 = generate_silent_audio(silent_audio, duration=2.0, dry_run=True)
        assert r3.returncode == 0

        # Step 4: create video segment (dry_run — inputs must exist)
        Path(norm_video).touch()   # created by normalize dry_run
        Path(silent_audio).touch() # created by generate_silent_audio dry_run
        r4 = create_video_segment(
            video_path=norm_video,
            audio_path=silent_audio,
            output_path=segment_out,
            duration=2.0,
            overlays=[],
            dry_run=True,
        )
        assert r4.returncode == 0

        # Key flags verified (dry_run builds but doesn't execute)
        cmd_str = " ".join(r4.ffmpeg_command)
        assert "-stream_loop" in cmd_str
        assert "1:a" in r4.ffmpeg_command
