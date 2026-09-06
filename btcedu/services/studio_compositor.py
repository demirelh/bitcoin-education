"""Composite one presenter scene into the ALMANYA24 studio.

This module does exactly one thing and does it without touching the database,
the pipeline or the episode: given a studio description, an avatar clip, a piece
of topic media and a narration file, it produces one finished shot. That
isolation is the point -- compositing is the step most likely to be wrong in a
way only a human eye catches, so it has to be runnable and testable on its own.

Three rules are non-negotiable and are enforced here rather than trusted.

The presenter clip must really carry an alpha channel when the studio is in
alpha mode. An opaque file silently treated as transparent produces a black
rectangle standing in the studio, and it produces it for the whole bulletin.

The avatar's own audio never reaches the output. HeyGen returns the audio it was
given, re-encoded; using it would replace the levelled ElevenLabs take with a
generation-loss copy and, worse, could end up mixed alongside it. Only the
original TTS file is mapped.

The output lasts exactly as long as the narration. The picture is padded to fit
the audio -- never the other way round -- because a shot that is cut to the
video's length cuts off the end of a sentence.
"""

import json
import logging
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from btcedu.core.studio_manifest import (
    ALPHA_MODE_OPAQUE,
    ALPHA_MODE_WEBM,
    ASSET_KIND_IMAGE,
    ASSET_KIND_VIDEO,
    FIT_CONTAIN,
    FIT_COVER,
    Rect,
    StudioManifest,
)

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 900
# The encoder rounds the last frame; anything larger than this is a real
# mismatch between narration and picture.
DURATION_TOLERANCE_SECONDS = 0.35

_ALPHA_PIX_FMT = re.compile(
    r"^(yuva\d|gbrap|ya\d|pal8$)|^(argb|abgr|rgba|bgra)|(rgba|bgra)(64(le|be))?$"
)


class StudioCompositeError(RuntimeError):
    """Compositing refused or failed. Never downgraded to a warning."""


@dataclass(frozen=True)
class DisplayMedia:
    """The editorial medium shown in the studio monitor.

    It is always a file the pipeline already produced for this chapter. The
    reporter's blocks show the very same file full-frame; nothing is generated
    a second time for the studio.
    """

    path: str
    kind: str = ASSET_KIND_IMAGE
    fit_mode: str = FIT_COVER
    focus_point: tuple[float, float] = (0.5, 0.5)
    is_fallback: bool = False


@dataclass(frozen=True)
class StudioCompositeRequest:
    """Everything one composited shot needs."""

    manifest: StudioManifest
    audio_path: str
    output_path: str
    scene_id: str = ""
    avatar_clip: str | None = None
    display_media: DisplayMedia | None = None
    background_override: str | None = None
    duration_seconds: float | None = None


@dataclass
class StudioCompositeResult:
    """What was produced, and which optional paths were actually taken."""

    output_path: str
    duration_seconds: float
    size_bytes: int
    width: int
    height: int
    fps: int
    alpha_mode: str
    used_perspective: bool = False
    used_fallback_media: bool = False
    ffmpeg_command: list[str] = field(default_factory=list)
    dry_run: bool = False


def _run(cmd: list[str], timeout: int) -> tuple[int, str]:
    """Run a command as an argument list. Never through a shell."""
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return result.returncode, result.stderr
    except subprocess.TimeoutExpired:
        return -1, f"timed out after {timeout}s"
    except FileNotFoundError:
        return -1, f"{cmd[0]} not found in PATH"


def _ffprobe_streams(path: str) -> list[dict]:
    if not Path(path).exists():
        raise StudioCompositeError(f"Media file not found: {path}")
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_streams",
        "-show_format",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise StudioCompositeError(f"ffprobe could not read {path}: {exc}") from exc
    if result.returncode != 0:
        raise StudioCompositeError(f"ffprobe failed on {path}: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout).get("streams", [])
    except json.JSONDecodeError as exc:
        raise StudioCompositeError(f"ffprobe returned unreadable JSON for {path}") from exc


def _pix_fmt_has_alpha(pix_fmt: str) -> bool:
    return bool(_ALPHA_PIX_FMT.search((pix_fmt or "").strip().lower()))


def probe_has_alpha(path: str) -> bool:
    """Does this file's first video stream actually carry transparency?

    Asked of the file, not of its extension. A ``.webm`` encoded as ``yuv420p``
    is opaque, and the pipeline has no way to notice that later.

    WebM carries VP8/VP9 transparency out of band: the pixel format stays
    ``yuv420p`` and the alpha plane travels in Matroska block additions, flagged
    by an ``alpha_mode`` tag. A pixel-format check alone would therefore call
    every real HeyGen delivery opaque and refuse it.
    """
    for stream in _ffprobe_streams(path):
        if stream.get("codec_type") != "video":
            continue
        if _pix_fmt_has_alpha(str(stream.get("pix_fmt") or "")):
            return True
        tags = {str(k).lower(): str(v) for k, v in (stream.get("tags") or {}).items()}
        return tags.get("alpha_mode") in ("1", "true")
    raise StudioCompositeError(f"No video stream in {path}")


def probe_duration(path: str) -> float:
    """Duration of a media file in seconds."""
    if not Path(path).exists():
        raise StudioCompositeError(f"Media file not found: {path}")
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError) as exc:
        raise StudioCompositeError(f"ffprobe could not read {path}: {exc}") from exc
    try:
        return float((result.stdout or "").strip())
    except ValueError as exc:
        raise StudioCompositeError(f"Could not determine duration of {path}") from exc


@lru_cache(maxsize=1)
def has_perspective_filter() -> bool:
    """Is the local ffmpeg able to project the monitor into a tilted wall?

    Cached because it costs a subprocess and cannot change while we run. When it
    is missing the monitor is filled as a plain rectangle instead; that is a
    cosmetic downgrade, not a reason to refuse a bulletin.
    """
    if shutil.which("ffmpeg") is None:
        return False
    returncode, _ = (0, "")
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        returncode = result.returncode
        text = result.stdout or ""
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return False
    if returncode != 0:
        return False
    return any(line.split()[1:2] == ["perspective"] for line in text.splitlines() if line.strip())


def _num(value: float) -> str:
    """Render a number for a filter string, refusing anything that is not one.

    Filter graphs are strings, which makes them the one place a manifest value
    could turn into a second filter. Every number that reaches a graph goes
    through here, and nothing else ever does.
    """
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")):
        raise StudioCompositeError(f"Refusing non-finite number in filter graph: {value!r}")
    return f"{number:.6f}".rstrip("0").rstrip(".") or "0"


def _fit_filters(
    width: int,
    height: int,
    fit_mode: str,
    focus_point: tuple[float, float],
) -> str:
    """Scale a picture into a box, either filling it or fitting inside it."""
    w, h = _num(width), _num(height)
    if fit_mode == FIT_CONTAIN:
        return (
            f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black"
        )
    if fit_mode != FIT_COVER:
        raise StudioCompositeError(f"Unsupported fit mode: {fit_mode!r}")
    fx, fy = _num(focus_point[0]), _num(focus_point[1])
    # The focus point decides which part survives the crop, so a face on the
    # left of a 16:9 photo is not cropped out of a 4:3 monitor.
    return (
        f"scale={w}:{h}:force_original_aspect_ratio=increase,"
        f"crop={w}:{h}:(iw-ow)*{fx}:(ih-oh)*{fy}"
    )


def _corner_bbox(corners: tuple[tuple[int, int], ...]) -> Rect:
    xs = [point[0] for point in corners]
    ys = [point[1] for point in corners]
    return Rect(min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))


def _input_args(path: str, kind: str, fps: int, duration: float) -> list[str]:
    """Read a still as a clip of the required length, a video as itself."""
    if kind == ASSET_KIND_IMAGE:
        return ["-loop", "1", "-framerate", str(fps), "-t", _num(duration), "-i", str(path)]
    return ["-stream_loop", "-1", "-i", str(path)]


def _resolve_background(request: StudioCompositeRequest) -> tuple[str, str]:
    manifest = request.manifest
    if request.background_override:
        override = Path(request.background_override)
        kind = ASSET_KIND_VIDEO if override.suffix.lower() in {".mp4", ".webm", ".mov"} else (
            ASSET_KIND_IMAGE
        )
        return str(override), kind
    return str(manifest.asset_path(manifest.background)), manifest.background.kind


def build_composite_command(
    request: StudioCompositeRequest,
    duration: float,
    *,
    perspective_available: bool,
) -> tuple[list[str], bool]:
    """Build the ffmpeg invocation. Pure, so the graph itself can be tested."""
    manifest = request.manifest
    width, height, fps = manifest.width, manifest.height, manifest.fps

    args: list[str] = ["ffmpeg", "-y", "-nostdin"]
    filters: list[str] = []
    index = 0

    background_path, background_kind = _resolve_background(request)
    args += _input_args(background_path, background_kind, fps, duration)
    background_index = index
    index += 1

    filters.append(
        f"[{background_index}:v]scale={_num(width)}:{_num(height)},setsar=1,"
        f"fps={_num(fps)},format=rgba[bg]"
    )
    current = "bg"
    used_perspective = False

    if request.display_media is not None:
        zone = manifest.display_zone
        args += _input_args(
            request.display_media.path, request.display_media.kind, fps, duration
        )
        media_index = index
        index += 1

        fit = _fit_filters(
            zone.rect.width,
            zone.rect.height,
            request.display_media.fit_mode,
            request.display_media.focus_point,
        )
        if zone.corners and perspective_available:
            used_perspective = True
            points = ":".join(_num(v) for point in zone.corners for v in point)
            bbox = _corner_bbox(zone.corners)
            # The monitor picture is stretched across the whole canvas and then
            # projected onto the wall's four corners; the projection undoes the
            # stretch. Cropping back to the quad's bounding box keeps whatever
            # the projection leaves at the edges out of the rest of the frame.
            filters.append(
                f"[{media_index}:v]{fit},scale={_num(width)}:{_num(height)},"
                f"setsar=1,fps={_num(fps)},format=rgba,"
                f"perspective={points}:sense=destination,"
                f"crop={_num(bbox.width)}:{_num(bbox.height)}:"
                f"{_num(bbox.x)}:{_num(bbox.y)}[disp]"
            )
            overlay_x, overlay_y = _num(bbox.x), _num(bbox.y)
        else:
            filters.append(
                f"[{media_index}:v]{fit},setsar=1,fps={_num(fps)},format=rgba[disp]"
            )
            overlay_x, overlay_y = _num(zone.rect.x), _num(zone.rect.y)

        filters.append(
            f"[{current}][disp]overlay={overlay_x}:{overlay_y}:format=auto[withdisp]"
        )
        current = "withdisp"

    if manifest.shadow_layer is not None and request.avatar_clip:
        args += _input_args(
            str(manifest.asset_path(manifest.shadow_layer)),
            manifest.shadow_layer.kind,
            fps,
            duration,
        )
        shadow_index = index
        index += 1
        filters.append(
            f"[{shadow_index}:v]scale={_num(width)}:{_num(height)},setsar=1,"
            f"fps={_num(fps)},format=rgba[shadow]"
        )
        filters.append(f"[{current}][shadow]overlay=0:0:format=auto[withshadow]")
        current = "withshadow"

    if request.avatar_clip:
        args += ["-i", str(request.avatar_clip)]
        avatar_index = index
        index += 1
        presenter = manifest.presenter
        pad = _num(max(0.0, duration))
        # If the clip is shorter than the narration the last frame is held.
        # Letting the presenter vanish mid-sentence would be worse than a
        # motionless second.
        filters.append(
            f"[{avatar_index}:v]scale=iw*{_num(presenter.scale)}:ih*{_num(presenter.scale)},"
            f"setsar=1,fps={_num(fps)},format=rgba,"
            f"tpad=stop_mode=clone:stop_duration={pad}[avatar]"
        )
        filters.append(
            f"[{current}][avatar]"
            f"overlay={_num(presenter.anchor_x)}-w/2:{_num(presenter.anchor_y)}-h:"
            f"format=auto:shortest=0[withavatar]"
        )
        current = "withavatar"

    if manifest.foreground_layer is not None:
        args += _input_args(
            str(manifest.asset_path(manifest.foreground_layer)),
            manifest.foreground_layer.kind,
            fps,
            duration,
        )
        foreground_index = index
        index += 1
        filters.append(
            f"[{foreground_index}:v]scale={_num(width)}:{_num(height)},setsar=1,"
            f"fps={_num(fps)},format=rgba[fg]"
        )
        filters.append(f"[{current}][fg]overlay=0:0:format=auto[withfg]")
        current = "withfg"

    filters.append(f"[{current}]format=yuv420p[vout]")

    args += ["-i", str(request.audio_path)]
    audio_index = index

    args += [
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[vout]",
        # Only the narration is mapped. The avatar's own track is never even
        # offered to the muxer, so a second audio stream cannot appear.
        "-map",
        f"{audio_index}:a:0",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-r",
        str(fps),
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ac",
        "2",
        "-ar",
        "44100",
        "-t",
        _num(duration),
        str(request.output_path),
    ]
    return args, used_perspective


def _validate_output(path: Path, request: StudioCompositeRequest, duration: float) -> float:
    """Refuse a result that is empty, truncated, silent or the wrong size."""
    if not path.exists() or path.stat().st_size == 0:
        raise StudioCompositeError(f"Compositing produced no output: {path}")

    streams = _ffprobe_streams(str(path))
    video = [s for s in streams if s.get("codec_type") == "video"]
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if not video:
        raise StudioCompositeError(f"Composited file has no video stream: {path}")
    if len(audio) != 1:
        raise StudioCompositeError(
            f"Composited file must carry exactly one audio track, found {len(audio)}: {path}"
        )

    manifest = request.manifest
    actual_w = int(video[0].get("width") or 0)
    actual_h = int(video[0].get("height") or 0)
    if (actual_w, actual_h) != (manifest.width, manifest.height):
        raise StudioCompositeError(
            f"Composited file is {actual_w}x{actual_h}, expected "
            f"{manifest.width}x{manifest.height}"
        )

    actual_duration = probe_duration(str(path))
    if abs(actual_duration - duration) > DURATION_TOLERANCE_SECONDS:
        raise StudioCompositeError(
            f"Composited file lasts {actual_duration:.3f}s but the narration is "
            f"{duration:.3f}s; the end of the sentence would be lost"
        )
    return actual_duration


def composite_studio_scene(
    request: StudioCompositeRequest,
    *,
    dry_run: bool = False,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
) -> StudioCompositeResult:
    """Render one presenter scene into the studio."""
    manifest = request.manifest
    output_path = Path(request.output_path)

    if not Path(request.audio_path).exists():
        raise StudioCompositeError(f"Narration audio not found: {request.audio_path}")
    if request.avatar_clip and not Path(request.avatar_clip).exists():
        raise StudioCompositeError(f"Avatar clip not found: {request.avatar_clip}")
    if request.display_media is not None and not Path(request.display_media.path).exists():
        raise StudioCompositeError(f"Topic medium not found: {request.display_media.path}")

    if request.avatar_clip and manifest.alpha_mode == ALPHA_MODE_WEBM and not dry_run:
        if not probe_has_alpha(request.avatar_clip):
            raise StudioCompositeError(
                f"Studio is in {ALPHA_MODE_WEBM} mode but {request.avatar_clip} has no alpha "
                "channel. Compositing it would put an opaque rectangle in the studio."
            )

    if manifest.alpha_mode == ALPHA_MODE_OPAQUE and request.display_media is not None:
        zone = manifest.display_zone
        if not zone.presenter_free and manifest.occlusion_mask is None:
            raise StudioCompositeError(
                "Opaque fallback: the topic monitor may only be drawn where the presenter "
                "provably is not. Mark the display zone presenter_free or supply an "
                "occlusion mask."
            )

    duration = request.duration_seconds
    if duration is None:
        duration = 0.0 if dry_run else probe_duration(request.audio_path)
    if not dry_run and duration <= 0:
        raise StudioCompositeError(
            f"Narration {request.audio_path} has no usable duration ({duration})"
        )

    perspective_available = False if dry_run else has_perspective_filter()
    # Written beside the target and moved into place only once it has passed
    # every check, so an interrupted run never leaves a half video that the
    # next run would treat as finished. The real suffix is kept because ffmpeg
    # picks the muxer from it; the leading dot keeps it out of media globs.
    temp_path = output_path.with_name(f".{output_path.stem}.part{output_path.suffix}")
    build_request = StudioCompositeRequest(
        manifest=manifest,
        audio_path=request.audio_path,
        output_path=str(temp_path),
        scene_id=request.scene_id,
        avatar_clip=request.avatar_clip,
        display_media=request.display_media,
        background_override=request.background_override,
        duration_seconds=request.duration_seconds,
    )
    cmd, used_perspective = build_composite_command(
        build_request, duration, perspective_available=perspective_available
    )

    if dry_run:
        return StudioCompositeResult(
            output_path=str(output_path),
            duration_seconds=duration,
            size_bytes=0,
            width=manifest.width,
            height=manifest.height,
            fps=manifest.fps,
            alpha_mode=manifest.alpha_mode,
            used_perspective=used_perspective,
            used_fallback_media=bool(
                request.display_media is not None and request.display_media.is_fallback
            ),
            ffmpeg_command=cmd,
            dry_run=True,
        )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path.unlink(missing_ok=True)
    returncode, stderr = _run(cmd, timeout_seconds)
    if returncode != 0:
        temp_path.unlink(missing_ok=True)
        raise StudioCompositeError(
            f"Studio compositing failed for scene {request.scene_id or '?'} "
            f"(exit {returncode}): {stderr.strip()[-800:]}"
        )

    try:
        actual_duration = _validate_output(temp_path, request, duration)
    except StudioCompositeError:
        temp_path.unlink(missing_ok=True)
        raise

    os.replace(temp_path, output_path)
    logger.info(
        "Composited scene %s into the studio: %.3fs, %s",
        request.scene_id or output_path.stem,
        actual_duration,
        output_path.name,
    )
    return StudioCompositeResult(
        output_path=str(output_path),
        duration_seconds=actual_duration,
        size_bytes=output_path.stat().st_size,
        width=manifest.width,
        height=manifest.height,
        fps=manifest.fps,
        alpha_mode=manifest.alpha_mode,
        used_perspective=used_perspective,
        used_fallback_media=bool(
            request.display_media is not None and request.display_media.is_fallback
        ),
        ffmpeg_command=cmd,
    )
