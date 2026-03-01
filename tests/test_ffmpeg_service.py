"""Tests for Sprint 9: ffmpeg service layer."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from btcedu.services.ffmpeg_service import (
    OVERLAY_POSITIONS,
    ConcatResult,
    MediaInfo,
    OverlaySpec,
    SegmentResult,
    _build_drawtext_filter,
    _escape_drawtext,
    concatenate_segments,
    create_segment,
    find_font_path,
    get_ffmpeg_version,
    probe_media,
)


def test_get_ffmpeg_version_success():
    """Test ffmpeg version parsing."""
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout="ffmpeg version 6.1.1\nOther info..."
        )
        version = get_ffmpeg_version()
        assert "ffmpeg version 6.1.1" in version
        mock_run.assert_called_once()


def test_get_ffmpeg_version_not_found():
    """Test ffmpeg not found."""
    with patch("subprocess.run", side_effect=FileNotFoundError):
        version = get_ffmpeg_version()
        assert version == "unknown"


def test_find_font_path_returns_name_when_not_found():
    """Test font path fallback to name."""
    # Mock all paths to not exist
    with patch("pathlib.Path.exists", return_value=False):
        result = find_font_path("NonExistentFont")
        assert result == "NonExistentFont"


def test_escape_drawtext_plain_text():
    """Test escape with no special chars."""
    text = "Hello World"
    escaped = _escape_drawtext(text)
    assert escaped == "Hello World"


def test_escape_drawtext_colon_and_quotes():
    """Test escape of colons and quotes."""
    text = "Text: with 'quotes'"
    escaped = _escape_drawtext(text)
    assert "\\:" in escaped  # Colon escaped
    assert "'\\''" in escaped  # Quote escaped


def test_escape_drawtext_turkish_chars():
    """Test Turkish characters pass through."""
    text = "İşçiöğü"
    escaped = _escape_drawtext(text)
    # Turkish chars should be unmodified
    assert "İ" in escaped
    assert "ş" in escaped
    assert "ç" in escaped
    assert "ö" in escaped
    assert "ğ" in escaped
    assert "ü" in escaped


def test_build_drawtext_filter_lower_third():
    """Test drawtext filter for lower_third."""
    spec = OverlaySpec(
        text="Test Text",
        overlay_type="lower_third",
        fontsize=48,
        fontcolor="white",
        font="/tmp/font.ttf",
        position="bottom_center",
        start=2.0,
        end=7.0,
    )
    filter_str = _build_drawtext_filter(spec, "/tmp/font.ttf")

    assert "drawtext" in filter_str
    assert "fontfile=/tmp/font.ttf" in filter_str
    assert "text='Test Text'" in filter_str
    assert "fontsize=48" in filter_str
    assert "fontcolor=white" in filter_str
    assert OVERLAY_POSITIONS["bottom_center"] in filter_str
    assert "enable='between(t\\,2.0\\,7.0)'" in filter_str


def test_build_drawtext_filter_center():
    """Test drawtext filter for centered text."""
    spec = OverlaySpec(
        text="Center",
        overlay_type="title",
        fontsize=72,
        fontcolor="white",
        font="/tmp/font.ttf",
        position="center",
        start=0.0,
        end=5.0,
    )
    filter_str = _build_drawtext_filter(spec, "/tmp/font.ttf")

    assert OVERLAY_POSITIONS["center"] in filter_str
    assert "fontsize=72" in filter_str


def test_create_segment_dry_run(tmp_path):
    """Test segment creation in dry-run mode."""
    # Create dummy input files
    image = tmp_path / "image.png"
    audio = tmp_path / "audio.mp3"
    output = tmp_path / "segment.mp4"

    image.write_bytes(b"fake png")
    audio.write_bytes(b"fake mp3")

    result = create_segment(
        image_path=str(image),
        audio_path=str(audio),
        output_path=str(output),
        duration=60.0,
        overlays=[],
        dry_run=True,
    )

    assert isinstance(result, SegmentResult)
    assert result.returncode == 0
    assert result.stderr == "[dry-run]"
    assert output.exists()
    assert result.duration_seconds == 60.0


def test_create_segment_missing_image(tmp_path):
    """Test segment creation with missing image."""
    audio = tmp_path / "audio.mp3"
    output = tmp_path / "segment.mp4"
    audio.write_bytes(b"fake mp3")

    with pytest.raises(FileNotFoundError, match="Image not found"):
        create_segment(
            image_path=str(tmp_path / "missing.png"),
            audio_path=str(audio),
            output_path=str(output),
            duration=60.0,
            overlays=[],
        )


def test_create_segment_missing_audio(tmp_path):
    """Test segment creation with missing audio."""
    image = tmp_path / "image.png"
    output = tmp_path / "segment.mp4"
    image.write_bytes(b"fake png")

    with pytest.raises(FileNotFoundError, match="Audio not found"):
        create_segment(
            image_path=str(image),
            audio_path=str(tmp_path / "missing.mp3"),
            output_path=str(output),
            duration=60.0,
            overlays=[],
        )


def test_create_segment_with_overlays(tmp_path):
    """Test segment creation with text overlays."""
    image = tmp_path / "image.png"
    audio = tmp_path / "audio.mp3"
    output = tmp_path / "segment.mp4"

    image.write_bytes(b"fake png")
    audio.write_bytes(b"fake mp3")

    overlays = [
        OverlaySpec(
            text="Lower Third",
            overlay_type="lower_third",
            fontsize=48,
            fontcolor="white",
            font="/tmp/font.ttf",
            position="bottom_center",
            start=2.0,
            end=7.0,
        )
    ]

    # Mock ffmpeg execution
    with patch("btcedu.services.ffmpeg_service._run_ffmpeg") as mock_run:
        mock_run.return_value = (0, "")
        result = create_segment(
            image_path=str(image),
            audio_path=str(audio),
            output_path=str(output),
            duration=60.0,
            overlays=overlays,
        )

        # Check command contains drawtext
        cmd = result.ffmpeg_command
        filter_complex = None
        for i, arg in enumerate(cmd):
            if arg == "-filter_complex":
                filter_complex = cmd[i + 1]
                break

        assert filter_complex is not None
        assert "drawtext" in filter_complex
        assert "Lower Third" in filter_complex


def test_concatenate_segments_dry_run(tmp_path):
    """Test segment concatenation in dry-run mode."""
    seg1 = tmp_path / "seg1.mp4"
    seg2 = tmp_path / "seg2.mp4"
    output = tmp_path / "output.mp4"

    seg1.write_bytes(b"fake video 1")
    seg2.write_bytes(b"fake video 2")

    result = concatenate_segments(
        segment_paths=[str(seg1), str(seg2)],
        output_path=str(output),
        dry_run=True,
    )

    assert isinstance(result, ConcatResult)
    assert result.returncode == 0
    assert result.stderr == "[dry-run]"
    assert result.segment_count == 2
    assert output.exists()


def test_concatenate_segments_empty_list():
    """Test concatenation with no segments."""
    with pytest.raises(ValueError, match="No segments to concatenate"):
        concatenate_segments(segment_paths=[], output_path="/tmp/out.mp4")


def test_concatenate_segments_missing_segment(tmp_path):
    """Test concatenation with missing segment."""
    seg1 = tmp_path / "seg1.mp4"
    seg1.write_bytes(b"fake video")

    with pytest.raises(FileNotFoundError, match="Segment not found"):
        concatenate_segments(
            segment_paths=[str(seg1), str(tmp_path / "missing.mp4")],
            output_path=str(tmp_path / "output.mp4"),
        )


def test_probe_media_missing_file():
    """Test probing missing file."""
    with pytest.raises(FileNotFoundError, match="Media file not found"):
        probe_media("/nonexistent/file.mp4")


def test_probe_media_success(tmp_path):
    """Test successful media probing."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake video")

    ffprobe_output = {
        "format": {
            "duration": "123.45",
            "size": "1024000",
            "format_name": "mp4",
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1920,
                "height": 1080,
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
            },
        ],
    }

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(ffprobe_output)
        )

        info = probe_media(str(video))

        assert isinstance(info, MediaInfo)
        assert info.duration_seconds == 123.45
        assert info.width == 1920
        assert info.height == 1080
        assert info.codec_video == "h264"
        assert info.codec_audio == "aac"
        assert info.size_bytes == 1024000
        assert info.format_name == "mp4"


def test_probe_media_video_only(tmp_path):
    """Test probing video-only file."""
    video = tmp_path / "video.mp4"
    video.write_bytes(b"fake video")

    ffprobe_output = {
        "format": {
            "duration": "60.0",
            "size": "512000",
            "format_name": "mp4",
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "h264",
                "width": 1280,
                "height": 720,
            },
        ],
    }

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(
            returncode=0, stdout=json.dumps(ffprobe_output)
        )

        info = probe_media(str(video))

        assert info.codec_video == "h264"
        assert info.codec_audio is None
