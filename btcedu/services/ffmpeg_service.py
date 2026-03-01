"""ffmpeg service: Video composition via ffmpeg CLI."""

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class OverlaySpec:
    """Text overlay specification for ffmpeg drawtext filter."""

    text: str
    overlay_type: str  # lower_third, title, quote, statistic
    fontsize: int
    fontcolor: str
    font: str
    position: str  # bottom_center, center, top_center
    start: float  # seconds
    end: float  # seconds
    box: bool = True
    boxcolor: str = "black@0.6"
    boxborderw: int = 10


@dataclass
class SegmentResult:
    """Result of creating a single video segment."""

    segment_path: str
    duration_seconds: float
    size_bytes: int
    ffmpeg_command: list[str]
    returncode: int
    stderr: str


@dataclass
class ConcatResult:
    """Result of concatenating video segments."""

    output_path: str
    duration_seconds: float
    size_bytes: int
    segment_count: int
    ffmpeg_command: list[str]
    returncode: int
    stderr: str


@dataclass
class MediaInfo:
    """Media file metadata from ffprobe."""

    duration_seconds: float
    width: int | None
    height: int | None
    codec_video: str | None
    codec_audio: str | None
    size_bytes: int
    format_name: str


# Position mappings for overlay text
OVERLAY_POSITIONS = {
    "bottom_center": "x=(w-text_w)/2:y=h-th-60",
    "center": "x=(w-text_w)/2:y=(h-text_h)/2",
    "top_center": "x=(w-text_w)/2:y=60",
}


def get_ffmpeg_version() -> str:
    """Get ffmpeg version string."""
    try:
        result = subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # Parse first line like "ffmpeg version 6.1.1"
            first_line = result.stdout.split("\n")[0]
            return first_line.strip()
        return "unknown"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return "unknown"


def find_font_path(font_name: str) -> str:
    """Find font file path or return fontconfig name.

    Args:
        font_name: Font name like "NotoSans-Bold"

    Returns:
        Absolute path to font file or original name if not found
    """
    # Search common Linux font paths
    search_paths = [
        Path("/usr/share/fonts/truetype/noto"),
        Path("/usr/share/fonts/truetype/dejavu"),
        Path("/usr/share/fonts/TTF"),
    ]

    # Try exact match first
    for base_dir in search_paths:
        if not base_dir.exists():
            continue
        for font_file in base_dir.rglob("*.ttf"):
            if font_name in font_file.stem:
                return str(font_file.absolute())

    # Fallback: try DejaVu-Bold as a known-good fallback
    for base_dir in search_paths:
        if not base_dir.exists():
            continue
        dejavu_bold = base_dir / "DejaVuSans-Bold.ttf"
        if dejavu_bold.exists():
            logger.warning("Font %s not found, using DejaVuSans-Bold fallback", font_name)
            return str(dejavu_bold.absolute())

    # Last resort: return fontconfig name and let ffmpeg handle it
    logger.warning("Font %s not found in system paths, passing to fontconfig", font_name)
    return font_name


def _escape_drawtext(text: str) -> str:
    """Escape special characters for ffmpeg drawtext filter.

    Args:
        text: Text to display

    Returns:
        Escaped text safe for drawtext filter
    """
    # Escape ffmpeg-specific special chars
    # Note: Turkish chars (ş,ç,ğ,ı,ö,ü,İ) pass through unmodified
    text = text.replace("\\", "\\\\")  # Backslash must be first
    text = text.replace(":", "\\:")
    text = text.replace("'", "'\\''")  # Escape single quotes
    return text


def _build_drawtext_filter(overlay: OverlaySpec, font_path: str) -> str:
    """Build ffmpeg drawtext filter string from overlay spec.

    Args:
        overlay: Overlay specification
        font_path: Path to font file

    Returns:
        drawtext filter string
    """
    escaped_text = _escape_drawtext(overlay.text)
    position = OVERLAY_POSITIONS.get(overlay.position, OVERLAY_POSITIONS["center"])

    parts = [
        "drawtext",
        f"fontfile={font_path}",
        f"text='{escaped_text}'",
        f"fontsize={overlay.fontsize}",
        f"fontcolor={overlay.fontcolor}",
        position,
    ]

    if overlay.box:
        parts.append(f"box={1 if overlay.box else 0}")
        parts.append(f"boxcolor={overlay.boxcolor}")
        parts.append(f"boxborderw={overlay.boxborderw}")

    # Add timing constraint
    parts.append(f"enable='between(t\\,{overlay.start}\\,{overlay.end})'")

    return ":".join(parts)


def create_segment(
    image_path: str,
    audio_path: str,
    output_path: str,
    duration: float,
    overlays: list[OverlaySpec],
    resolution: str = "1920x1080",
    fps: int = 30,
    crf: int = 23,
    preset: str = "medium",
    audio_bitrate: str = "192k",
    font: str = "NotoSans-Bold",
    timeout_seconds: int = 300,
    dry_run: bool = False,
) -> SegmentResult:
    """Create a video segment from image + audio + overlays.

    Args:
        image_path: Path to image file
        audio_path: Path to audio file
        output_path: Path for output video
        duration: Duration in seconds
        overlays: List of text overlays
        resolution: Output resolution (WxH)
        fps: Framerate
        crf: H.264 quality (0-51, lower = better)
        preset: H.264 encoding speed preset
        audio_bitrate: AAC audio bitrate
        font: Font name for text overlays
        timeout_seconds: Max execution time
        dry_run: If True, build command but don't execute

    Returns:
        SegmentResult with paths and metadata

    Raises:
        FileNotFoundError: If image or audio doesn't exist
        RuntimeError: If ffmpeg execution fails
    """
    # Validate inputs
    if not Path(image_path).exists():
        raise FileNotFoundError(f"Image not found: {image_path}")
    if not Path(audio_path).exists():
        raise FileNotFoundError(f"Audio not found: {audio_path}")

    # Build filter_complex
    width, height = resolution.split("x")
    filter_parts = [
        # Scale and pad image to exact resolution with black bars if needed
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"format=yuv420p[scaled]"
    ]

    if overlays:
        # Build drawtext chain
        font_path = find_font_path(font)
        last_label = "scaled"
        for i, overlay in enumerate(overlays):
            drawtext_filter = _build_drawtext_filter(overlay, font_path)
            out_label = f"overlay{i}" if i < len(overlays) - 1 else "v"
            filter_parts.append(f"[{last_label}]{drawtext_filter}[{out_label}]")
            last_label = out_label
    else:
        # No overlays: just rename final label
        filter_parts.append("[scaled]copy[v]")

    filter_complex = ";".join(filter_parts)

    # Build ffmpeg command
    cmd = [
        "ffmpeg",
        "-y",  # Overwrite output
        "-loop",
        "1",  # Loop image
        "-i",
        image_path,
        "-i",
        audio_path,
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-map",
        "1:a",
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        audio_bitrate,
        "-t",
        str(duration),
        "-shortest",
        output_path,
    ]

    if dry_run:
        logger.info("Dry-run: would run ffmpeg command (not executing)")
        # Create empty placeholder file
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).touch()
        return SegmentResult(
            segment_path=output_path,
            duration_seconds=duration,
            size_bytes=0,
            ffmpeg_command=cmd,
            returncode=0,
            stderr="[dry-run]",
        )

    # Execute ffmpeg
    returncode, stderr = _run_ffmpeg(cmd, timeout_seconds)

    # Get output file size
    size_bytes = 0
    if Path(output_path).exists():
        size_bytes = Path(output_path).stat().st_size

    if returncode != 0:
        raise RuntimeError(f"ffmpeg segment creation failed (exit code {returncode}): {stderr}")

    return SegmentResult(
        segment_path=output_path,
        duration_seconds=duration,
        size_bytes=size_bytes,
        ffmpeg_command=cmd,
        returncode=returncode,
        stderr=stderr,
    )


def concatenate_segments(
    segment_paths: list[str],
    output_path: str,
    timeout_seconds: int = 600,
    dry_run: bool = False,
) -> ConcatResult:
    """Concatenate video segments into single output.

    Args:
        segment_paths: List of segment video paths (in order)
        output_path: Path for concatenated output
        timeout_seconds: Max execution time
        dry_run: If True, build command but don't execute

    Returns:
        ConcatResult with metadata

    Raises:
        ValueError: If segment_paths is empty
        FileNotFoundError: If any segment doesn't exist
        RuntimeError: If ffmpeg execution fails
    """
    if not segment_paths:
        raise ValueError("No segments to concatenate")

    # Validate all segments exist
    for seg_path in segment_paths:
        if not Path(seg_path).exists():
            raise FileNotFoundError(f"Segment not found: {seg_path}")

    # Create concat list file (must use absolute paths)
    concat_list_path = Path(output_path).parent / "concat_list.txt"
    concat_list_path.parent.mkdir(parents=True, exist_ok=True)

    with open(concat_list_path, "w", encoding="utf-8") as f:
        for seg_path in segment_paths:
            abs_path = Path(seg_path).absolute()
            # ffmpeg concat format: file '/absolute/path/to/file.mp4'
            f.write(f"file '{abs_path}'\n")

    # Build ffmpeg command
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(concat_list_path),
        "-c",
        "copy",  # Stream copy (no re-encoding)
        output_path,
    ]

    if dry_run:
        logger.info("Dry-run: would concatenate %d segments", len(segment_paths))
        Path(output_path).touch()
        return ConcatResult(
            output_path=output_path,
            duration_seconds=0.0,
            size_bytes=0,
            segment_count=len(segment_paths),
            ffmpeg_command=cmd,
            returncode=0,
            stderr="[dry-run]",
        )

    # Execute ffmpeg
    returncode, stderr = _run_ffmpeg(cmd, timeout_seconds)

    # Get output metadata
    size_bytes = 0
    duration_seconds = 0.0
    if Path(output_path).exists():
        size_bytes = Path(output_path).stat().st_size
        try:
            media_info = probe_media(output_path)
            duration_seconds = media_info.duration_seconds
        except Exception as e:
            logger.warning("Could not probe concatenated video duration: %s", e)

    if returncode != 0:
        raise RuntimeError(f"ffmpeg concatenation failed (exit code {returncode}): {stderr}")

    return ConcatResult(
        output_path=output_path,
        duration_seconds=duration_seconds,
        size_bytes=size_bytes,
        segment_count=len(segment_paths),
        ffmpeg_command=cmd,
        returncode=returncode,
        stderr=stderr,
    )


def probe_media(file_path: str) -> MediaInfo:
    """Probe media file with ffprobe.

    Args:
        file_path: Path to media file

    Returns:
        MediaInfo with metadata

    Raises:
        FileNotFoundError: If file doesn't exist
        RuntimeError: If ffprobe fails
    """
    if not Path(file_path).exists():
        raise FileNotFoundError(f"Media file not found: {file_path}")

    cmd = [
        "ffprobe",
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        file_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )

        if result.returncode != 0:
            raise RuntimeError(f"ffprobe failed: {result.stderr}")

        data = json.loads(result.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        raise RuntimeError(f"ffprobe execution failed: {e}") from e

    # Extract metadata
    format_info = data.get("format", {})
    duration_seconds = float(format_info.get("duration", 0.0))
    size_bytes = int(format_info.get("size", 0))
    format_name = format_info.get("format_name", "unknown")

    # Find video and audio streams
    video_stream = None
    audio_stream = None
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not video_stream:
            video_stream = stream
        elif stream.get("codec_type") == "audio" and not audio_stream:
            audio_stream = stream

    width = video_stream.get("width") if video_stream else None
    height = video_stream.get("height") if video_stream else None
    codec_video = video_stream.get("codec_name") if video_stream else None
    codec_audio = audio_stream.get("codec_name") if audio_stream else None

    return MediaInfo(
        duration_seconds=duration_seconds,
        width=width,
        height=height,
        codec_video=codec_video,
        codec_audio=codec_audio,
        size_bytes=size_bytes,
        format_name=format_name,
    )


def _run_ffmpeg(cmd: list[str], timeout: int) -> tuple[int, str]:
    """Run ffmpeg command with timeout.

    Args:
        cmd: Command list
        timeout: Timeout in seconds

    Returns:
        Tuple of (returncode, stderr)
    """
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return result.returncode, result.stderr
    except subprocess.TimeoutExpired:
        logger.error("ffmpeg command timed out after %ds", timeout)
        return -1, f"Command timed out after {timeout}s"
    except FileNotFoundError:
        logger.error("ffmpeg executable not found")
        return -1, "ffmpeg not found in PATH"
