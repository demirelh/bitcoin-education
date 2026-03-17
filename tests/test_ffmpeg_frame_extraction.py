"""Tests for ffmpeg frame extraction, cropping, and style filter functions."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.services.ffmpeg_service import (
    _STYLE_FILTER_PRESETS,
    apply_style_filter,
    crop_frame,
    extract_keyframes,
)


class TestExtractKeyframes:
    @patch("btcedu.services.ffmpeg_service._run_ffmpeg")
    def test_scene_detection_parses_timestamps(self, mock_ffmpeg, tmp_path):
        """Parses pts_time values from ffmpeg showinfo output."""
        showinfo_output = (
            "[Parsed_showinfo_1 @ 0x123] n:0 pts:0 pts_time:2.500 ...\n"
            "[Parsed_showinfo_1 @ 0x123] n:1 pts:150 pts_time:5.000 ...\n"
            "[Parsed_showinfo_1 @ 0x123] n:2 pts:300 pts_time:10.500 ...\n"
        )
        # Pass 1: scene detection returns timestamps
        # Pass 2: each frame extraction succeeds
        call_count = {"n": 0}

        def side_effect(cmd, timeout):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # Scene detection pass
                return 0, showinfo_output
            else:
                # Frame extraction — create a fake file
                # Find output path from cmd
                output_path = cmd[-1]
                Path(output_path).parent.mkdir(parents=True, exist_ok=True)
                Path(output_path).write_bytes(b"\x89PNG")
                return 0, ""

        mock_ffmpeg.side_effect = side_effect

        with patch(
            "btcedu.services.ffmpeg_service._probe_image_dimensions", return_value=(1920, 1080)
        ):
            frames = extract_keyframes(
                video_path=str(tmp_path / "video.mp4"),
                output_dir=str(tmp_path / "frames"),
                scene_threshold=0.3,
            )

        assert len(frames) == 3
        assert frames[0].timestamp_seconds == 2.5
        assert frames[1].timestamp_seconds == 5.0
        assert frames[2].timestamp_seconds == 10.5

    @patch("btcedu.services.ffmpeg_service._run_ffmpeg")
    def test_min_interval_filtering(self, mock_ffmpeg, tmp_path):
        """Frames closer than min_interval are skipped."""
        showinfo_output = (
            "[info] pts_time:1.000\n"
            "[info] pts_time:1.500\n"  # too close to 1.0 (< 2.0 interval)
            "[info] pts_time:3.500\n"
        )
        call_count = {"n": 0}

        def side_effect(cmd, timeout):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return 0, showinfo_output
            output_path = cmd[-1]
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"\x89PNG")
            return 0, ""

        mock_ffmpeg.side_effect = side_effect

        with patch(
            "btcedu.services.ffmpeg_service._probe_image_dimensions", return_value=(1920, 1080)
        ):
            frames = extract_keyframes(
                str(tmp_path / "v.mp4"),
                str(tmp_path / "f"),
                min_interval_seconds=2.0,
            )

        assert len(frames) == 2
        assert frames[0].timestamp_seconds == 1.0
        assert frames[1].timestamp_seconds == 3.5

    @patch("btcedu.services.ffmpeg_service._run_ffmpeg")
    def test_max_frames_limit(self, mock_ffmpeg, tmp_path):
        """Respects max_frames cap."""
        timestamps = "\n".join(f"[info] pts_time:{i}.000" for i in range(50))
        call_count = {"n": 0}

        def side_effect(cmd, timeout):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return 0, timestamps
            output_path = cmd[-1]
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"\x89PNG")
            return 0, ""

        mock_ffmpeg.side_effect = side_effect

        with patch(
            "btcedu.services.ffmpeg_service._probe_image_dimensions", return_value=(1920, 1080)
        ):
            frames = extract_keyframes(
                str(tmp_path / "v.mp4"),
                str(tmp_path / "f"),
                max_frames=5,
                min_interval_seconds=0.5,
            )

        assert len(frames) <= 5

    def test_dry_run_returns_empty(self, tmp_path):
        """Dry run returns empty list without calling ffmpeg."""
        frames = extract_keyframes(
            str(tmp_path / "v.mp4"),
            str(tmp_path / "f"),
            dry_run=True,
        )
        assert frames == []

    @patch("btcedu.services.ffmpeg_service._run_ffmpeg")
    @patch("btcedu.services.ffmpeg_service.probe_media")
    def test_fallback_to_uniform_on_detection_failure(self, mock_probe, mock_ffmpeg, tmp_path):
        """Falls back to uniform extraction if scene detection fails."""
        from btcedu.services.ffmpeg_service import MediaInfo

        mock_probe.return_value = MediaInfo(
            duration_seconds=30.0,
            width=1920,
            height=1080,
            codec_video="h264",
            codec_audio="aac",
            size_bytes=1000,
            format_name="mp4",
        )

        call_count = {"n": 0}

        def side_effect(cmd, timeout):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return 1, "scene detection failed"  # Fail
            output_path = cmd[-1]
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            Path(output_path).write_bytes(b"\x89PNG")
            return 0, ""

        mock_ffmpeg.side_effect = side_effect

        with patch(
            "btcedu.services.ffmpeg_service._probe_image_dimensions", return_value=(1920, 1080)
        ):
            frames = extract_keyframes(
                str(tmp_path / "v.mp4"),
                str(tmp_path / "f"),
                max_frames=5,
            )

        # Should get some frames from uniform extraction
        assert len(frames) > 0


class TestCropFrame:
    @patch("btcedu.services.ffmpeg_service._run_ffmpeg")
    def test_crop_with_region(self, mock_ffmpeg, tmp_path):
        mock_ffmpeg.return_value = (0, "")
        inp = str(tmp_path / "input.png")
        out = str(tmp_path / "output.png")
        Path(inp).write_bytes(b"\x89PNG")

        crop_frame(inp, out, crop_region=(0, 0, 1920, 600))

        cmd = mock_ffmpeg.call_args[0][0]
        assert "crop=1920:600:0:0" in ",".join(cmd)
        assert "scale=1920:1080" in ",".join(cmd)

    @patch("btcedu.services.ffmpeg_service._run_ffmpeg")
    def test_crop_without_region_scales_only(self, mock_ffmpeg, tmp_path):
        mock_ffmpeg.return_value = (0, "")
        inp = str(tmp_path / "input.png")
        out = str(tmp_path / "output.png")
        Path(inp).write_bytes(b"\x89PNG")

        crop_frame(inp, out, crop_region=None)

        cmd = mock_ffmpeg.call_args[0][0]
        vf_idx = cmd.index("-filter:v") + 1
        assert "crop" not in cmd[vf_idx]
        assert "scale=1920:1080" in cmd[vf_idx]

    def test_dry_run_creates_empty_file(self, tmp_path):
        out = str(tmp_path / "dry.png")
        crop_frame(str(tmp_path / "in.png"), out, dry_run=True)
        assert Path(out).exists()

    @patch("btcedu.services.ffmpeg_service._run_ffmpeg")
    def test_failure_raises_runtime_error(self, mock_ffmpeg, tmp_path):
        mock_ffmpeg.return_value = (1, "crop error")
        with pytest.raises(RuntimeError, match="crop_frame failed"):
            crop_frame(str(tmp_path / "in.png"), str(tmp_path / "out.png"))


class TestApplyStyleFilter:
    @patch("btcedu.services.ffmpeg_service._run_ffmpeg")
    def test_news_recolor_preset(self, mock_ffmpeg, tmp_path):
        mock_ffmpeg.return_value = (0, "")
        inp = str(tmp_path / "in.png")
        out = str(tmp_path / "out.png")
        Path(inp).write_bytes(b"\x89PNG")

        apply_style_filter(inp, out, filter_preset="news_recolor")

        cmd = mock_ffmpeg.call_args[0][0]
        vf_idx = cmd.index("-filter:v") + 1
        assert "hue=" in cmd[vf_idx]
        assert "curves=vintage" in cmd[vf_idx]

    def test_invalid_preset_raises(self, tmp_path):
        with pytest.raises(ValueError, match="Unknown filter preset"):
            apply_style_filter(
                str(tmp_path / "in.png"),
                str(tmp_path / "out.png"),
                filter_preset="nonexistent",
            )

    def test_dry_run_copies_file(self, tmp_path):
        inp = tmp_path / "in.png"
        inp.write_bytes(b"\x89PNG_CONTENT")
        out = str(tmp_path / "out.png")

        apply_style_filter(str(inp), out, dry_run=True)
        assert Path(out).read_bytes() == b"\x89PNG_CONTENT"

    def test_all_presets_are_valid_strings(self):
        for name, vf in _STYLE_FILTER_PRESETS.items():
            assert isinstance(name, str)
            assert isinstance(vf, str)
            assert len(vf) > 0


class TestDownloadVideo:
    """Tests for the new download_video function."""

    @patch("btcedu.services.download_service.subprocess")
    def test_download_video_happy_path(self, mock_subprocess, tmp_path):
        from btcedu.services.download_service import download_video

        out_dir = str(tmp_path / "output")
        video_file = tmp_path / "output" / "video.mp4"

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_subprocess.run.return_value = mock_result

        # Create the expected output file
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        video_file.write_bytes(b"\x00" * 100)

        result = download_video("https://example.com/video", out_dir)
        assert result == str(video_file)

    @patch("btcedu.services.download_service.subprocess")
    def test_download_video_failure_raises(self, mock_subprocess, tmp_path):
        from btcedu.services.download_service import download_video

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stderr = "download error"
        mock_subprocess.run.return_value = mock_result

        with pytest.raises(RuntimeError, match="yt-dlp failed"):
            download_video("https://example.com/video", str(tmp_path / "out"))

    @patch("btcedu.services.download_service.subprocess")
    def test_download_video_format_string(self, mock_subprocess, tmp_path):
        from btcedu.services.download_service import download_video

        out_dir = str(tmp_path / "output")
        Path(out_dir).mkdir(parents=True, exist_ok=True)
        (tmp_path / "output" / "video.mp4").write_bytes(b"\x00")

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_subprocess.run.return_value = mock_result

        download_video("https://example.com/video", out_dir, max_height=480)

        cmd = mock_subprocess.run.call_args[0][0]
        # Verify the format string includes the height limit
        fmt_idx = cmd.index("--format") + 1
        assert "480" in cmd[fmt_idx]
