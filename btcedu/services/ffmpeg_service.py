"""ffmpeg service: Video composition via ffmpeg CLI."""

import json
import logging
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Ken Burns movement patterns cycled per chapter
KEN_BURNS_PATTERNS = ["zoom_in", "zoom_out", "pan_left", "pan_right", "pan_up"]


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

    option_parts = [
        f"fontfile={font_path}",
        f"text='{escaped_text}'",
        f"fontsize={overlay.fontsize}",
        f"fontcolor={overlay.fontcolor}",
        position,
    ]

    if overlay.box:
        option_parts.append(f"box={1 if overlay.box else 0}")
        option_parts.append(f"boxcolor={overlay.boxcolor}")
        option_parts.append(f"boxborderw={overlay.boxborderw}")

    # Add timing constraint
    option_parts.append(f"enable='between(t\\,{overlay.start}\\,{overlay.end})'")

    return "drawtext=" + ":".join(option_parts)


def _build_kenburns_filter(
    pattern: str,
    duration: float,
    resolution: str = "1920x1080",
    fps: int = 30,
    zoom_ratio: float = 0.04,
) -> str:
    """Build zoompan filter string for Ken Burns effect.

    Args:
        pattern: Movement pattern (zoom_in, zoom_out, pan_left, pan_right, pan_up)
        duration: Segment duration in seconds
        resolution: Output resolution (WxH)
        fps: Output framerate
        zoom_ratio: Total zoom range (e.g. 0.04 = 1.0 to 1.04)

    Returns:
        zoompan filter string for filter_complex
    """
    width, height = resolution.split("x")
    total_frames = int(duration * fps)
    d = max(total_frames, 1)
    r = zoom_ratio

    if pattern == "zoom_in":
        z_expr = f"1.0+{r}*(1-cos(PI*on/{d}))/2"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    elif pattern == "zoom_out":
        z_expr = f"1.0+{r}*(1+cos(PI*on/{d}))/2"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"
    elif pattern == "pan_left":
        z_expr = f"1.0+{r}/2"
        x_expr = f"iw*{r}*(1-on/{d})"
        y_expr = "ih/2-(ih/zoom/2)"
    elif pattern == "pan_right":
        z_expr = f"1.0+{r}/2"
        x_expr = f"iw*{r}*on/{d}"
        y_expr = "ih/2-(ih/zoom/2)"
    elif pattern == "pan_up":
        z_expr = f"1.0+{r}/2"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = f"ih*{r}*(1-on/{d})"
    else:
        # Fallback to zoom_in
        z_expr = f"1.0+{r}*(1-cos(PI*on/{d}))/2"
        x_expr = "iw/2-(iw/zoom/2)"
        y_expr = "ih/2-(ih/zoom/2)"

    return (
        f"zoompan=z='{z_expr}':x='{x_expr}':y='{y_expr}'"
        f":d={d}:s={width}x{height}:fps={fps}"
    )


def _build_animated_lower_third(
    overlay: OverlaySpec,
    font_path: str,
    slide_duration: float = 0.4,
    accent_color: str = "#F7931A",
) -> list[str]:
    """Build animated lower third with gradient background and slide-in.

    Returns a list of filter strings (drawbox + drawtext) to be chained.
    """
    escaped_text = _escape_drawtext(overlay.text)
    start = overlay.start
    end = overlay.end
    enable = f"enable='between(t\\,{start}\\,{end})'"
    bar_h = 100
    bar_y = f"h-{bar_h}-70"

    filters = []

    # Gradient background: two stacked drawbox at different opacities
    filters.append(
        f"drawbox=x=0:y={bar_y}:w=w:h={bar_h}:color=black@0.7:t=fill:{enable}"
    )
    filters.append(
        f"drawbox=x=0:y={bar_y}:w=w:h={bar_h // 2}"
        f":color=black@0.5:t=fill:{enable}"
    )

    # Accent stripe on left edge
    filters.append(
        f"drawbox=x=0:y={bar_y}:w=4:h={bar_h}"
        f":color={accent_color}:t=fill:{enable}"
    )

    # Slide-in animation for x position
    slide_x = (
        f"if(lt(t-{start}\\,{slide_duration})\\,"
        f"-text_w+(text_w+60)*(t-{start})/{slide_duration}\\,60)"
    )

    # Check for two-line text
    lines = overlay.text.split("\\n") if "\\n" in overlay.text else [overlay.text]
    if len(lines) >= 2:
        headline = _escape_drawtext(lines[0])
        subtext = _escape_drawtext(lines[1])
        # Headline (larger)
        filters.append(
            f"drawtext=fontfile={font_path}:text='{headline}'"
            f":fontsize={overlay.fontsize}:fontcolor={overlay.fontcolor}"
            f":x='{slide_x}':y={bar_y}+15:{enable}"
        )
        # Subtext (smaller)
        filters.append(
            f"drawtext=fontfile={font_path}:text='{subtext}'"
            f":fontsize={max(overlay.fontsize - 12, 24)}"
            f":fontcolor={overlay.fontcolor}@0.8"
            f":x='{slide_x}':y={bar_y}+55:{enable}"
        )
    else:
        filters.append(
            f"drawtext=fontfile={font_path}:text='{escaped_text}'"
            f":fontsize={overlay.fontsize}:fontcolor={overlay.fontcolor}"
            f":x='{slide_x}':y={bar_y}+({bar_h}-text_h)/2:{enable}"
        )

    return filters


def _build_ticker_filters(
    ticker_text: str,
    font_path: str,
    speed: int = 80,
    height: int = 50,
    fontsize: int = 28,
) -> list[str]:
    """Build scrolling news ticker filter strings.

    Returns list of filter strings (separator line + background + scrolling text).
    """
    escaped = _escape_drawtext(ticker_text)
    filters = [
        # Separator line
        f"drawbox=x=0:y=h-{height}-1:w=w:h=1:color=white@0.8:t=fill",
        # Ticker background
        f"drawbox=x=0:y=h-{height}:w=w:h={height}:color=black@0.8:t=fill",
        # Scrolling text
        (
            f"drawtext=fontfile={font_path}:text='{escaped}'"
            f":fontsize={fontsize}:fontcolor=white"
            f":x='w-mod(t*{speed}\\,w+text_w)'"
            f":y=h-{height}+({height}-text_h)/2"
        ),
    ]
    return filters


def _build_color_correction_filter(
    saturation: float = 0.85,
    brightness: float = 0.02,
    blue_shift: float = 0.05,
) -> str:
    """Build color correction filter for news film look."""
    return (
        f"eq=saturation={saturation}:brightness={brightness},"
        f"colorbalance=rs=-0.03:gs=-0.01:bs={blue_shift}"
    )


def create_intro_segment(
    output_path: str,
    show_name: str,
    episode_title: str,
    episode_date: str,
    duration: float = 4.0,
    resolution: str = "1920x1080",
    fps: int = 30,
    bg_color: str = "#004B87",
    accent_color: str = "#F7931A",
    font: str = "NotoSans-Bold",
    crf: int = 23,
    preset: str = "medium",
    timeout_seconds: int = 60,
    dry_run: bool = False,
) -> SegmentResult:
    """Create intro segment with show name, episode title, and date."""
    font_path = find_font_path(font)
    escaped_show = _escape_drawtext(show_name)
    escaped_title = _escape_drawtext(episode_title)
    escaped_date = _escape_drawtext(episode_date)

    fade_out_start = max(0, duration - 0.5)

    filter_parts = [
        f"[0:v]"
        f"drawtext=fontfile={font_path}:text='{escaped_show}'"
        f":fontsize=80:fontcolor={accent_color}"
        f":x=(w-text_w)/2:y=(h/2)-80"
        f":enable='between(t\\,0.5\\,{duration})',"
        f"drawtext=fontfile={font_path}:text='{escaped_title}'"
        f":fontsize=48:fontcolor=white"
        f":x=(w-text_w)/2:y=(h/2)+20"
        f":enable='between(t\\,1.0\\,{duration})',"
        f"drawtext=fontfile={font_path}:text='{escaped_date}'"
        f":fontsize=32:fontcolor=white@0.7"
        f":x=(w-text_w)/2:y=(h/2)+80"
        f":enable='between(t\\,1.5\\,{duration})',"
        f"fade=t=in:st=0:d=0.5,fade=t=out:st={fade_out_start}:d=0.5"
        f"[v]"
    ]

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c={bg_color}:s={resolution}:d={duration}:r={fps}",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-t", str(duration),
        output_path,
    ]

    if dry_run:
        logger.info("Dry-run: would create intro segment")
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

    returncode, stderr = _run_ffmpeg(cmd, timeout_seconds)
    size_bytes = Path(output_path).stat().st_size if Path(output_path).exists() else 0

    if returncode != 0:
        raise RuntimeError(f"Intro segment creation failed (exit {returncode}): {stderr}")

    return SegmentResult(
        segment_path=output_path,
        duration_seconds=duration,
        size_bytes=size_bytes,
        ffmpeg_command=cmd,
        returncode=returncode,
        stderr=stderr,
    )


def create_outro_segment(
    output_path: str,
    source_text: str,
    duration: float = 3.0,
    resolution: str = "1920x1080",
    fps: int = 30,
    bg_color: str = "#004B87",
    accent_color: str = "#F7931A",
    font: str = "NotoSans-Bold",
    crf: int = 23,
    preset: str = "medium",
    timeout_seconds: int = 60,
    dry_run: bool = False,
) -> SegmentResult:
    """Create outro segment with source attribution."""
    font_path = find_font_path(font)
    escaped_source = _escape_drawtext(source_text)

    fade_out_start = max(0, duration - 0.5)

    filter_complex = (
        f"[0:v]"
        f"drawtext=fontfile={font_path}:text='{escaped_source}'"
        f":fontsize=40:fontcolor=white"
        f":x=(w-text_w)/2:y=(h/2)-20"
        f":enable='between(t\\,0.5\\,{duration})',"
        f"drawtext=fontfile={font_path}"
        f":text='Bir sonraki bölümde görüşürüz'"
        f":fontsize=28:fontcolor={accent_color}"
        f":x=(w-text_w)/2:y=(h/2)+40"
        f":enable='between(t\\,1.0\\,{duration})',"
        f"fade=t=in:st=0:d=0.5,fade=t=out:st={fade_out_start}:d=0.5"
        f"[v]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c={bg_color}:s={resolution}:d={duration}:r={fps}",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "1:a",
        "-c:v", "libx264", "-preset", preset, "-crf", str(crf),
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-t", str(duration),
        output_path,
    ]

    if dry_run:
        logger.info("Dry-run: would create outro segment")
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

    returncode, stderr = _run_ffmpeg(cmd, timeout_seconds)
    size_bytes = Path(output_path).stat().st_size if Path(output_path).exists() else 0

    if returncode != 0:
        raise RuntimeError(f"Outro segment creation failed (exit {returncode}): {stderr}")

    return SegmentResult(
        segment_path=output_path,
        duration_seconds=duration,
        size_bytes=size_bytes,
        ffmpeg_command=cmd,
        returncode=returncode,
        stderr=stderr,
    )


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
    fade_in_duration: float = 0.0,  # Sprint 10: fade in duration (seconds)
    fade_out_duration: float = 0.0,  # Sprint 10: fade out duration (seconds)
    timeout_seconds: int = 300,
    dry_run: bool = False,
    # Video quality enhancements
    ken_burns_pattern: str | None = None,
    ken_burns_zoom_ratio: float = 0.04,
    animated_lower_thirds: bool = False,
    lower_third_slide_duration: float = 0.4,
    lower_third_accent_color: str = "#F7931A",
    ticker_text: str | None = None,
    ticker_speed: int = 80,
    ticker_height: int = 50,
    ticker_fontsize: int = 28,
    color_correction: bool = False,
    color_saturation: float = 0.85,
    color_brightness: float = 0.02,
    color_blue_shift: float = 0.05,
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
        fade_in_duration: Fade in duration (Sprint 10)
        fade_out_duration: Fade out duration (Sprint 10)
        timeout_seconds: Max execution time
        dry_run: If True, build command but don't execute
        ken_burns_pattern: Movement pattern or None to disable
        ken_burns_zoom_ratio: Total zoom range for Ken Burns
        animated_lower_thirds: Enable animated lower third overlays
        lower_third_slide_duration: Slide-in animation duration
        lower_third_accent_color: Accent color for lower third stripe
        ticker_text: Scrolling ticker text or None to disable
        ticker_speed: Ticker scroll speed in px/sec
        ticker_height: Ticker bar height in pixels
        ticker_fontsize: Ticker text font size
        color_correction: Enable news film color correction
        color_saturation: Color correction saturation
        color_brightness: Color correction brightness
        color_blue_shift: Color correction blue shift

    Returns:
        SegmentResult with paths and metadata

    Raises:
        FileNotFoundError: If image or audio doesn't exist
        RuntimeError: If ffmpeg execution fails
    """
    t_start = time.monotonic()

    # Validate inputs
    if not Path(image_path).exists():
        raise FileNotFoundError(f"Image not found: {image_path}")
    if not Path(audio_path).exists():
        raise FileNotFoundError(f"Audio not found: {audio_path}")

    # Build filter_complex
    width, height = resolution.split("x")

    if ken_burns_pattern:
        # Ken Burns: zoompan generates frames from still image (no -loop 1)
        kb_filter = _build_kenburns_filter(
            ken_burns_pattern, duration, resolution, fps, ken_burns_zoom_ratio,
        )
        filter_parts = [
            f"[0:v]scale=-1:-1,{kb_filter},format=yuv420p[raw]"
        ]
    else:
        # Standard: scale and pad image to exact resolution
        filter_parts = [
            f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
            f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
            f"format=yuv420p[raw]"
        ]

    # Color correction (applied before overlays)
    if color_correction:
        cc_filter = _build_color_correction_filter(
            color_saturation, color_brightness, color_blue_shift,
        )
        filter_parts.append(f"[raw]{cc_filter}[scaled]")
    else:
        filter_parts.append("[raw]copy[scaled]")

    # Build overlay chain
    font_path = find_font_path(font) if (overlays or ticker_text) else None
    has_overlays = bool(overlays) or bool(ticker_text)

    if has_overlays:
        last_label = "scaled"
        filter_idx = 0

        # Text overlays
        for overlay in overlays:
            if (
                animated_lower_thirds
                and overlay.overlay_type == "lower_third"
            ):
                # Animated lower third: multiple filter strings
                lt_filters = _build_animated_lower_third(
                    overlay, font_path,
                    slide_duration=lower_third_slide_duration,
                    accent_color=lower_third_accent_color,
                )
                for lt_f in lt_filters:
                    out_label = f"ov{filter_idx}"
                    filter_parts.append(f"[{last_label}]{lt_f}[{out_label}]")
                    last_label = out_label
                    filter_idx += 1
            else:
                drawtext_filter = _build_drawtext_filter(overlay, font_path)
                out_label = f"ov{filter_idx}"
                filter_parts.append(
                    f"[{last_label}]{drawtext_filter}[{out_label}]"
                )
                last_label = out_label
                filter_idx += 1

        # News ticker (after overlays, before fade)
        if ticker_text:
            ticker_filters = _build_ticker_filters(
                ticker_text, font_path, ticker_speed, ticker_height,
                ticker_fontsize,
            )
            for tf in ticker_filters:
                out_label = f"ov{filter_idx}"
                filter_parts.append(f"[{last_label}]{tf}[{out_label}]")
                last_label = out_label
                filter_idx += 1

        # Rename last overlay label to pre_fade
        filter_parts.append(f"[{last_label}]copy[pre_fade]")
    else:
        filter_parts.append("[scaled]copy[pre_fade]")

    # Add fade filters (Sprint 10)
    if fade_in_duration > 0 or fade_out_duration > 0:
        fade_filters = []
        if fade_in_duration > 0:
            fade_filters.append(f"fade=t=in:st=0:d={fade_in_duration}")
        if fade_out_duration > 0:
            fade_out_start = max(0, duration - fade_out_duration)
            fade_filters.append(f"fade=t=out:st={fade_out_start}:d={fade_out_duration}")
        fade_chain = ",".join(fade_filters)
        filter_parts.append(f"[pre_fade]{fade_chain}[v]")
    else:
        # No fades: rename final label
        filter_parts.append("[pre_fade]copy[v]")

    filter_complex = ";".join(filter_parts)

    # Build ffmpeg command
    if ken_burns_pattern:
        # Ken Burns: no -loop 1, zoompan generates frames
        cmd = [
            "ffmpeg", "-y",
            "-i", image_path,
            "-i", audio_path,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "1:a",
        ]
    else:
        cmd = [
            "ffmpeg", "-y",
            "-loop", "1",  # Loop image
            "-i", image_path,
            "-i", audio_path,
            "-filter_complex", filter_complex,
            "-map", "[v]", "-map", "1:a",
        ]

    # Add audio fade filter (Sprint 10)
    if fade_in_duration > 0 or fade_out_duration > 0:
        audio_filters = []
        if fade_in_duration > 0:
            audio_filters.append(f"afade=t=in:st=0:d={fade_in_duration}")
        if fade_out_duration > 0:
            afade_out_start = max(0, duration - fade_out_duration)
            audio_filters.append(f"afade=t=out:st={afade_out_start}:d={fade_out_duration}")
        cmd.extend(["-af", ",".join(audio_filters)])

    # Continue with codec and output settings
    cmd.extend(
        [
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", audio_bitrate,
            "-t", str(duration),
            "-shortest",
            output_path,
        ]
    )

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

    elapsed = time.monotonic() - t_start
    logger.info("Segment render took %.1fs: %s", elapsed, output_path)

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


def normalize_video_clip(
    input_path: str,
    output_path: str,
    target_duration: float | None = None,
    resolution: str = "1920x1080",
    fps: int = 30,
    crf: int = 23,
    preset: str = "medium",
    timeout_seconds: int = 300,
    dry_run: bool = False,
) -> SegmentResult:
    """Normalize a stock video clip for render pipeline compatibility.

    - Scale and pad to target resolution (preserving aspect ratio)
    - Transcode to H.264/yuv420p
    - Match target FPS
    - Strip audio track
    - Optionally trim to target_duration

    Args:
        input_path: Path to input video file
        output_path: Path for normalized output video
        target_duration: Optional duration to trim to (seconds)
        resolution: Target resolution (WxH)
        fps: Target framerate
        crf: H.264 quality (0-51, lower = better)
        preset: H.264 encoding speed preset
        timeout_seconds: Max execution time
        dry_run: If True, build command but don't execute

    Returns:
        SegmentResult with paths and metadata

    Raises:
        FileNotFoundError: If input doesn't exist
        RuntimeError: If ffmpeg execution fails
    """
    if not Path(input_path).exists():
        raise FileNotFoundError(f"Video not found: {input_path}")

    width, height = resolution.split("x")

    filter_complex = (
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"format=yuv420p,"
        f"fps={fps}[v]"
    )

    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        input_path,
        "-filter_complex",
        filter_complex,
        "-map",
        "[v]",
        "-an",  # Strip audio
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        str(crf),
    ]

    if target_duration is not None:
        cmd.extend(["-t", str(target_duration)])

    cmd.append(output_path)

    if dry_run:
        logger.info("Dry-run: would normalize video clip (not executing)")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).touch()
        return SegmentResult(
            segment_path=output_path,
            duration_seconds=target_duration or 0.0,
            size_bytes=0,
            ffmpeg_command=cmd,
            returncode=0,
            stderr="[dry-run]",
        )

    returncode, stderr = _run_ffmpeg(cmd, timeout_seconds)

    size_bytes = 0
    if Path(output_path).exists():
        size_bytes = Path(output_path).stat().st_size

    if returncode != 0:
        raise RuntimeError(f"ffmpeg video normalization failed (exit code {returncode}): {stderr}")

    return SegmentResult(
        segment_path=output_path,
        duration_seconds=target_duration or 0.0,
        size_bytes=size_bytes,
        ffmpeg_command=cmd,
        returncode=returncode,
        stderr=stderr,
    )


def create_video_segment(
    video_path: str,
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
    fade_in_duration: float = 0.0,
    fade_out_duration: float = 0.0,
    timeout_seconds: int = 300,
    dry_run: bool = False,
    # Video quality enhancements (subset — no Ken Burns for video clips)
    animated_lower_thirds: bool = False,
    lower_third_slide_duration: float = 0.4,
    lower_third_accent_color: str = "#F7931A",
    ticker_text: str | None = None,
    ticker_speed: int = 80,
    ticker_height: int = 50,
    ticker_fontsize: int = 28,
    color_correction: bool = False,
    color_saturation: float = 0.85,
    color_brightness: float = 0.02,
    color_blue_shift: float = 0.05,
) -> SegmentResult:
    """Create a video segment from a video clip + TTS audio + overlays.

    Similar to create_segment() but input is a video file, not a still image.
    Uses -stream_loop -1 to loop short clips to fill the chapter duration.
    """
    t_start = time.monotonic()

    if not Path(video_path).exists():
        raise FileNotFoundError(f"Video not found: {video_path}")
    if not Path(audio_path).exists():
        raise FileNotFoundError(f"Audio not found: {audio_path}")

    width, height = resolution.split("x")

    # Build filter_complex
    filter_parts = [
        f"[0:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        f"format=yuv420p[raw]"
    ]

    # Color correction
    if color_correction:
        cc_filter = _build_color_correction_filter(
            color_saturation, color_brightness, color_blue_shift,
        )
        filter_parts.append(f"[raw]{cc_filter}[scaled]")
    else:
        filter_parts.append("[raw]copy[scaled]")

    # Build overlay chain
    font_path = find_font_path(font) if (overlays or ticker_text) else None
    has_overlays = bool(overlays) or bool(ticker_text)

    if has_overlays:
        last_label = "scaled"
        filter_idx = 0

        for overlay in overlays:
            if animated_lower_thirds and overlay.overlay_type == "lower_third":
                lt_filters = _build_animated_lower_third(
                    overlay, font_path,
                    slide_duration=lower_third_slide_duration,
                    accent_color=lower_third_accent_color,
                )
                for lt_f in lt_filters:
                    out_label = f"ov{filter_idx}"
                    filter_parts.append(f"[{last_label}]{lt_f}[{out_label}]")
                    last_label = out_label
                    filter_idx += 1
            else:
                drawtext_filter = _build_drawtext_filter(overlay, font_path)
                out_label = f"ov{filter_idx}"
                filter_parts.append(
                    f"[{last_label}]{drawtext_filter}[{out_label}]"
                )
                last_label = out_label
                filter_idx += 1

        if ticker_text:
            ticker_filters = _build_ticker_filters(
                ticker_text, font_path, ticker_speed, ticker_height,
                ticker_fontsize,
            )
            for tf in ticker_filters:
                out_label = f"ov{filter_idx}"
                filter_parts.append(f"[{last_label}]{tf}[{out_label}]")
                last_label = out_label
                filter_idx += 1

        filter_parts.append(f"[{last_label}]copy[pre_fade]")
    else:
        filter_parts.append("[scaled]copy[pre_fade]")

    if fade_in_duration > 0 or fade_out_duration > 0:
        fade_filters = []
        if fade_in_duration > 0:
            fade_filters.append(f"fade=t=in:st=0:d={fade_in_duration}")
        if fade_out_duration > 0:
            fade_out_start = max(0, duration - fade_out_duration)
            fade_filters.append(f"fade=t=out:st={fade_out_start}:d={fade_out_duration}")
        fade_chain = ",".join(fade_filters)
        filter_parts.append(f"[pre_fade]{fade_chain}[v]")
    else:
        filter_parts.append("[pre_fade]copy[v]")

    filter_complex = ";".join(filter_parts)

    # Build ffmpeg command — key difference: -stream_loop -1 before -i video
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", video_path,
        "-i", audio_path,
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "1:a",
    ]

    # Add audio fade filter
    if fade_in_duration > 0 or fade_out_duration > 0:
        audio_filters = []
        if fade_in_duration > 0:
            audio_filters.append(f"afade=t=in:st=0:d={fade_in_duration}")
        if fade_out_duration > 0:
            afade_out_start = max(0, duration - fade_out_duration)
            audio_filters.append(f"afade=t=out:st={afade_out_start}:d={fade_out_duration}")
        cmd.extend(["-af", ",".join(audio_filters)])

    cmd.extend(
        [
            "-c:v", "libx264",
            "-preset", preset,
            "-crf", str(crf),
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", audio_bitrate,
            "-t", str(duration),
            "-shortest",
            output_path,
        ]
    )

    if dry_run:
        logger.info("Dry-run: would run ffmpeg video segment command (not executing)")
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

    returncode, stderr = _run_ffmpeg(cmd, timeout_seconds)

    elapsed = time.monotonic() - t_start
    logger.info("Video segment render took %.1fs: %s", elapsed, output_path)

    size_bytes = 0
    if Path(output_path).exists():
        size_bytes = Path(output_path).stat().st_size

    if returncode != 0:
        raise RuntimeError(
            f"ffmpeg video segment creation failed (exit code {returncode}): {stderr}"
        )

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


def generate_test_video(
    output_path: str,
    duration: float = 2.0,
    resolution: str = "1920x1080",
    fps: int = 30,
    dry_run: bool = False,
) -> SegmentResult:
    """Generate a synthetic test video using ffmpeg's testsrc2 filter.

    No external input files needed — uses lavfi source. Intended for
    hardware smoke-testing the video pipeline on the target device.

    Args:
        output_path: Destination path for the generated MP4.
        duration: Duration in seconds (default 2s).
        resolution: Output resolution as WxH (default 1920x1080).
        fps: Frame rate (default 30).
        dry_run: If True, build command but don't execute.

    Returns:
        SegmentResult with command and metadata.

    Raises:
        RuntimeError: If ffmpeg fails.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=duration={duration}:size={resolution}:rate={fps}",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-crf",
        "23",
        output_path,
    ]

    if dry_run:
        logger.info("Dry-run: would generate test video (not executing)")
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

    returncode, stderr = _run_ffmpeg(cmd, 60)
    size_bytes = Path(output_path).stat().st_size if Path(output_path).exists() else 0

    if returncode != 0:
        raise RuntimeError(f"Test video generation failed (exit {returncode}): {stderr}")

    return SegmentResult(
        segment_path=output_path,
        duration_seconds=duration,
        size_bytes=size_bytes,
        ffmpeg_command=cmd,
        returncode=returncode,
        stderr=stderr,
    )


def generate_silent_audio(
    output_path: str,
    duration: float = 2.0,
    sample_rate: int = 44100,
    dry_run: bool = False,
) -> SegmentResult:
    """Generate a silent AAC audio file using ffmpeg's anullsrc filter.

    No external input files needed. Intended for hardware smoke-testing
    the TTS audio path on the target device alongside generate_test_video().

    Args:
        output_path: Destination path for the generated M4A/AAC file.
        duration: Duration in seconds (default 2s).
        sample_rate: Sample rate in Hz (default 44100).
        dry_run: If True, build command but don't execute.

    Returns:
        SegmentResult with command and metadata.

    Raises:
        RuntimeError: If ffmpeg fails.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"anullsrc=r={sample_rate}:cl=stereo",
        "-t",
        str(duration),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        output_path,
    ]

    if dry_run:
        logger.info("Dry-run: would generate silent audio (not executing)")
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

    returncode, stderr = _run_ffmpeg(cmd, 60)
    size_bytes = Path(output_path).stat().st_size if Path(output_path).exists() else 0

    if returncode != 0:
        raise RuntimeError(f"Silent audio generation failed (exit {returncode}): {stderr}")

    return SegmentResult(
        segment_path=output_path,
        duration_seconds=duration,
        size_bytes=size_bytes,
        ffmpeg_command=cmd,
        returncode=returncode,
        stderr=stderr,
    )


# ---------------------------------------------------------------------------
# Frame extraction & style filtering (frame-extraction pipeline)
# ---------------------------------------------------------------------------

_STYLE_FILTER_PRESETS: dict[str, str] = {
    "news_recolor": "hue=h=15:s=1.1,curves=vintage,vignette=PI/4,eq=contrast=1.05",
    "warm_tint": "colortemperature=temperature=6500,eq=saturation=1.1",
    "cool_tint": "colortemperature=temperature=4500,eq=saturation=0.9:contrast=1.05",
}


@dataclass
class ExtractedFrame:
    """Metadata for a single extracted keyframe."""

    frame_path: str
    timestamp_seconds: float
    scene_score: float
    width: int
    height: int
    size_bytes: int


def extract_keyframes(
    video_path: str,
    output_dir: str,
    scene_threshold: float = 0.3,
    min_interval_seconds: float = 2.0,
    max_frames: int = 100,
    timeout: int = 300,
    dry_run: bool = False,
) -> list[ExtractedFrame]:
    """Extract keyframes from *video_path* using ffmpeg scene-change detection.

    Writes PNG files into *output_dir* (``frame_0001.png``, …).

    Args:
        video_path: Input video file.
        output_dir: Directory to write frame images.
        scene_threshold: Scene-change threshold (0.0–1.0). Higher → fewer frames.
        min_interval_seconds: Minimum gap between consecutive frames.
        max_frames: Hard cap on frames extracted.
        timeout: Subprocess timeout in seconds.
        dry_run: If True, skip execution and return empty list.

    Returns:
        List of :class:`ExtractedFrame`, sorted by timestamp.
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    if dry_run:
        logger.info("Dry-run: would extract keyframes from %s", video_path)
        return []

    # --- Pass 1: detect scene-change timestamps via showinfo ---------------
    detect_cmd = [
        "ffmpeg",
        "-i",
        video_path,
        "-filter:v",
        f"select='gt(scene,{scene_threshold})',showinfo",
        "-vsync",
        "vfr",
        "-f",
        "null",
        "-",
    ]
    returncode, stderr = _run_ffmpeg(detect_cmd, timeout)
    if returncode != 0:
        logger.warning("Scene detection failed (exit %d), falling back to interval", returncode)
        return _extract_uniform_frames(video_path, output_dir, max_frames, timeout)

    # Parse timestamps from showinfo lines:  pts_time:12.345
    import re

    timestamps: list[float] = []
    for m in re.finditer(r"pts_time:\s*([\d.]+)", stderr):
        ts = float(m.group(1))
        if not timestamps or (ts - timestamps[-1]) >= min_interval_seconds:
            timestamps.append(ts)
            if len(timestamps) >= max_frames:
                break

    if not timestamps:
        logger.warning("No scene changes detected, falling back to interval extraction")
        return _extract_uniform_frames(video_path, output_dir, max_frames, timeout)

    # --- Pass 2: extract frames at those timestamps ------------------------
    frames: list[ExtractedFrame] = []
    for idx, ts in enumerate(timestamps, 1):
        frame_file = str(out / f"frame_{idx:04d}.png")
        cmd = [
            "ffmpeg",
            "-ss",
            f"{ts:.3f}",
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-y",
            frame_file,
        ]
        rc, err = _run_ffmpeg(cmd, 30)
        if rc != 0:
            logger.warning("Failed to extract frame at %.1fs: %s", ts, err)
            continue
        fp = Path(frame_file)
        if not fp.exists():
            continue
        info = _probe_image_dimensions(frame_file)
        frames.append(
            ExtractedFrame(
                frame_path=frame_file,
                timestamp_seconds=ts,
                scene_score=scene_threshold,  # actual score not available in pass-2
                width=info[0],
                height=info[1],
                size_bytes=fp.stat().st_size,
            )
        )
    return frames


def _extract_uniform_frames(
    video_path: str,
    output_dir: str,
    max_frames: int,
    timeout: int,
) -> list[ExtractedFrame]:
    """Fallback: extract evenly-spaced frames when scene detection fails."""
    info = probe_media(video_path)
    if info.duration_seconds <= 0:
        return []
    interval = max(info.duration_seconds / max_frames, 2.0)
    out = Path(output_dir)
    frames: list[ExtractedFrame] = []
    ts = 0.0
    idx = 0
    while ts < info.duration_seconds and idx < max_frames:
        idx += 1
        frame_file = str(out / f"frame_{idx:04d}.png")
        cmd = [
            "ffmpeg",
            "-ss",
            f"{ts:.3f}",
            "-i",
            video_path,
            "-frames:v",
            "1",
            "-y",
            frame_file,
        ]
        rc, _ = _run_ffmpeg(cmd, 30)
        fp = Path(frame_file)
        if rc == 0 and fp.exists():
            dim = _probe_image_dimensions(frame_file)
            frames.append(
                ExtractedFrame(
                    frame_path=frame_file,
                    timestamp_seconds=ts,
                    scene_score=0.0,
                    width=dim[0],
                    height=dim[1],
                    size_bytes=fp.stat().st_size,
                )
            )
        ts += interval
    return frames


def _probe_image_dimensions(path: str) -> tuple[int, int]:
    """Return (width, height) for an image via ffprobe, or (0, 0) on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        data = json.loads(result.stdout)
        stream = data.get("streams", [{}])[0]
        return int(stream.get("width", 0)), int(stream.get("height", 0))
    except Exception:
        return 0, 0


def crop_frame(
    input_path: str,
    output_path: str,
    crop_region: tuple[int, int, int, int] | None = None,
    target_size: tuple[int, int] = (1920, 1080),
    timeout: int = 30,
    dry_run: bool = False,
) -> str:
    """Crop and scale a frame image.

    Args:
        input_path: Source image file.
        output_path: Destination image file.
        crop_region: ``(x, y, width, height)`` or *None* for full frame.
        target_size: ``(width, height)`` to scale to after cropping.
        timeout: Subprocess timeout.
        dry_run: If True, create an empty file and return immediately.

    Returns:
        Path to the output image.
    """
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if dry_run:
        Path(output_path).touch()
        return output_path

    filters: list[str] = []
    if crop_region:
        x, y, w, h = crop_region
        filters.append(f"crop={w}:{h}:{x}:{y}")
    tw, th = target_size
    filters.append(f"scale={tw}:{th}")
    vf = ",".join(filters)

    cmd = ["ffmpeg", "-i", input_path, "-filter:v", vf, "-frames:v", "1", "-y", output_path]
    rc, err = _run_ffmpeg(cmd, timeout)
    if rc != 0:
        raise RuntimeError(f"crop_frame failed (exit {rc}): {err}")
    return output_path


def apply_style_filter(
    input_path: str,
    output_path: str,
    filter_preset: str = "news_recolor",
    timeout: int = 30,
    dry_run: bool = False,
) -> str:
    """Apply a visual style filter to an image for copyright differentiation.

    Args:
        input_path: Source image.
        output_path: Destination image.
        filter_preset: Key in :data:`_STYLE_FILTER_PRESETS`.
        timeout: Subprocess timeout.
        dry_run: If True, copy *input_path* to *output_path* unchanged.

    Returns:
        Path to the styled image.

    Raises:
        ValueError: If *filter_preset* is unknown.
    """
    if filter_preset not in _STYLE_FILTER_PRESETS:
        raise ValueError(
            f"Unknown filter preset '{filter_preset}', choose from {list(_STYLE_FILTER_PRESETS)}"
        )
    Path(output_path).parent.mkdir(parents=True, exist_ok=True)

    if dry_run:
        import shutil

        shutil.copy2(input_path, output_path)
        return output_path

    vf = _STYLE_FILTER_PRESETS[filter_preset]
    cmd = ["ffmpeg", "-i", input_path, "-filter:v", vf, "-frames:v", "1", "-y", output_path]
    rc, err = _run_ffmpeg(cmd, timeout)
    if rc != 0:
        raise RuntimeError(f"apply_style_filter failed (exit {rc}): {err}")
    return output_path


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
