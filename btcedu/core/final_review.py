"""Final review: deterministic video quality checks for weather chapters.

Runs during review_gate_3 to detect blank/freeze/missing/stale weather visuals
in the rendered video before publish. Produces structured findings with
severity levels that can block publish.

Checks operate on the **rendered segment** (render/segments/<chapterId>.mp4),
not only the source image asset. Source asset existence/provenance are also
validated.
"""

from __future__ import annotations

import hashlib
import json
import logging
import struct
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from btcedu.core.weather.models import (
    FindingSeverity,
    FindingType,
    ValidationFinding,
)

logger = logging.getLogger(__name__)

# Thresholds
_MIN_ASSET_SIZE_BYTES = 5000
_RESOLUTION_WIDTH = 1920
_RESOLUTION_HEIGHT = 1080
# Maximum allowed blank/freeze duration in seconds before CRITICAL
_MAX_BLANK_DURATION_SECONDS = 1.0
# Pixel standard deviation threshold below which a frame is "near-uniform"
_UNIFORMITY_STD_THRESHOLD = 8.0
# Near-black mean threshold (per channel 0-255)
_NEAR_BLACK_THRESHOLD = 15
# Near-white mean threshold (per channel 0-255)
_NEAR_WHITE_THRESHOLD = 240
# Maximum duration mismatch in seconds before emitting finding
_DURATION_MISMATCH_THRESHOLD = 1.0
# Max frames to sample in video freeze detection
_MAX_FRAME_SAMPLES = 30


@dataclass
class FinalReviewResult:
    """Aggregated result of final review weather checks."""

    findings: list[ValidationFinding] = field(default_factory=list)
    publish_blocked: bool = False
    chapters_checked: int = 0
    weather_chapters_found: int = 0

    def add_finding(self, finding: ValidationFinding) -> None:
        self.findings.append(finding)
        if finding.publish_blocked:
            self.publish_blocked = True


def run_weather_video_checks(
    episode_id: str,
    outputs_dir: str | Path,
) -> FinalReviewResult:
    """Run deterministic weather visual checks on a rendered episode.

    Inspects the render manifest to identify weather chapters, then validates:
    - Rendered segment exists in render/segments/
    - Source asset exists and is readable
    - Resolution via ffprobe (video) or header parsing (image)
    - Rendered segment duration vs manifest (weather_duration_mismatch)
    - Stale provenance/cache + source_text_hash drift
    - Persisted weather_validation.json publish_blocked findings
    - Frame analysis on rendered segment: blank/white/uniform/freeze

    Args:
        episode_id: Episode identifier.
        outputs_dir: Base outputs directory.

    Returns:
        FinalReviewResult with findings and publish_blocked flag.
    """
    result = FinalReviewResult()
    base = Path(outputs_dir) / episode_id

    # Load render manifest
    render_manifest_path = base / "render" / "render_manifest.json"
    if not render_manifest_path.exists():
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_RENDER_FAILED,
                severity=FindingSeverity.CRITICAL,
                message=f"Final review cannot read missing render manifest: {render_manifest_path}",
                publish_blocked=True,
            )
        )
        return result

    try:
        render_manifest = json.loads(render_manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_RENDER_FAILED,
                severity=FindingSeverity.CRITICAL,
                message=f"Final review cannot parse render manifest: {exc}",
                publish_blocked=True,
            )
        )
        return result

    # Load chapters to identify weather chapters
    chapters_path = base / "chapters.json"
    if not chapters_path.exists():
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_RENDER_FAILED,
                severity=FindingSeverity.CRITICAL,
                message=f"Final review cannot read missing chapters artifact: {chapters_path}",
                publish_blocked=True,
            )
        )
        return result

    try:
        chapters_data = json.loads(chapters_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_RENDER_FAILED,
                severity=FindingSeverity.CRITICAL,
                message=f"Final review cannot parse chapters artifact: {exc}",
                publish_blocked=True,
            )
        )
        return result

    # Load image manifest for source asset paths
    image_manifest_path = base / "images" / "manifest.json"
    image_manifest: dict = {}
    if image_manifest_path.exists():
        try:
            image_manifest = json.loads(image_manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass

    overrides: dict[str, str] = {}
    overrides_path = base / "images" / "weather_overrides.json"
    if overrides_path.exists():
        try:
            loaded_overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
            if isinstance(loaded_overrides, dict):
                overrides = {
                    str(key): str(value)
                    for key, value in loaded_overrides.items()
                    if value in {"weather", "normal"}
                }
        except (json.JSONDecodeError, OSError):
            pass

    weather_chapter_ids = _identify_weather_chapters(
        chapters_data,
        image_manifest=image_manifest,
        overrides=overrides,
    )

    if not weather_chapter_ids:
        result.chapters_checked = len(chapters_data.get("chapters", []))
        return result

    result.weather_chapters_found = len(weather_chapter_ids)

    # Build narration hash lookup from chapters.json for provenance verification
    narration_hashes: dict[str, str] = {}
    for ch in chapters_data.get("chapters", []):
        cid = ch.get("chapter_id", "")
        text = ch.get("narration", {}).get("text", "")
        if cid and text:
            narration_hashes[cid] = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    # Check each weather chapter
    segments = render_manifest.get("segments", [])
    for chapter_id in weather_chapter_ids:
        _check_weather_chapter(
            chapter_id=chapter_id,
            segments=segments,
            image_manifest=image_manifest,
            base_dir=base,
            narration_hashes=narration_hashes,
            result=result,
        )
        result.chapters_checked += 1

    return result


def _identify_weather_chapters(
    chapters_data: dict,
    *,
    image_manifest: dict | None = None,
    overrides: dict[str, str] | None = None,
) -> list[str]:
    """Identify weather chapters from chapters.json using the weather detector."""
    from btcedu.core.weather.detector import detect_weather_story

    weather_ids: list[str] = []
    manifest_weather_ids = {
        entry.get("chapter_id")
        for entry in (image_manifest or {}).get("images", [])
        if (entry.get("metadata") or {}).get("category") == "weather"
    }
    for ch in chapters_data.get("chapters", []):
        chapter_id = ch.get("chapter_id", "")
        override = (overrides or {}).get(chapter_id)
        if override == "normal":
            continue
        if override == "weather" or chapter_id in manifest_weather_ids:
            weather_ids.append(chapter_id)
            continue
        title = ch.get("title", "")
        narration_text = ch.get("narration", {}).get("text", "")

        detection = detect_weather_story(
            title=title,
            story_type=ch.get("story_type"),
            narration_text=narration_text,
            source_text=ch.get("source_text") or "",
            metadata=ch.get("metadata") or {},
        )
        if detection.is_weather_story:
            weather_ids.append(chapter_id)

    return weather_ids


def _check_weather_chapter(
    *,
    chapter_id: str,
    segments: list[dict],
    image_manifest: dict,
    base_dir: Path,
    narration_hashes: dict[str, str],
    result: FinalReviewResult,
) -> None:
    """Run all checks for a single weather chapter."""
    # Find the segment entry for this chapter in the render manifest
    segment = None
    for seg in segments:
        if seg.get("chapter_id") == chapter_id:
            segment = seg
            break

    if segment is None:
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_SCENE_EMPTY,
                severity=FindingSeverity.CRITICAL,
                message=f"Weather chapter '{chapter_id}' has no render segment",
                publish_blocked=True,
            )
        )
        return

    # --- Rendered segment checks (the actual video used in draft.mp4) ---
    segment_rel_path = segment.get("segment_path", "")
    rendered_segment_path = base_dir / segment_rel_path if segment_rel_path else None

    if not rendered_segment_path or not rendered_segment_path.exists():
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_VISUAL_MISSING,
                severity=FindingSeverity.CRITICAL,
                message=(
                    f"Rendered segment missing for weather chapter '{chapter_id}': "
                    f"{segment_rel_path or '(no path)'}"
                ),
                publish_blocked=True,
            )
        )
        return  # Cannot proceed without rendered segment

    # Check rendered segment resolution via ffprobe
    _check_segment_resolution(rendered_segment_path, chapter_id, result)

    # Check rendered segment duration vs manifest (narration coverage)
    _check_narration_coverage(rendered_segment_path, segment, chapter_id, result)

    # --- Source asset checks (the image/weather PNG fed to the renderer) ---
    source_rel_path = segment.get("image", "")
    if not source_rel_path:
        source_rel_path = _find_image_path(chapter_id, image_manifest)

    if source_rel_path:
        source_asset_path = base_dir / source_rel_path
        _check_source_asset_exists(source_asset_path, chapter_id, result)
        if source_asset_path.exists():
            _check_source_resolution(source_asset_path, chapter_id, result)
            _check_stale_provenance(
                source_asset_path, chapter_id, base_dir, narration_hashes, result
            )
    else:
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_VISUAL_MISSING,
                severity=FindingSeverity.CRITICAL,
                message=f"No source visual asset path for weather chapter '{chapter_id}'",
                publish_blocked=True,
            )
        )

    # --- Persisted weather validation findings ---
    _check_weather_validation_json(chapter_id, base_dir, result)

    # --- Frame analysis on the RENDERED SEGMENT ---
    _check_rendered_segment_frames(rendered_segment_path, chapter_id, segment, result)


def _find_image_path(chapter_id: str, image_manifest: dict) -> str:
    """Find image file_path from image manifest."""
    for img in image_manifest.get("images", []):
        if img.get("chapter_id") == chapter_id:
            return img.get("file_path", "")
    return ""


# ---------------------------------------------------------------------------
# Source asset checks
# ---------------------------------------------------------------------------


def _check_source_asset_exists(
    asset_path: Path, chapter_id: str, result: FinalReviewResult
) -> None:
    """Check source visual asset exists and is readable with minimum size."""
    if not asset_path.exists():
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_VISUAL_MISSING,
                severity=FindingSeverity.CRITICAL,
                message=f"Source weather visual missing: {asset_path.name} for '{chapter_id}'",
                publish_blocked=True,
            )
        )
        return

    try:
        size = asset_path.stat().st_size
    except OSError as exc:
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_VISUAL_MISSING,
                severity=FindingSeverity.CRITICAL,
                message=f"Cannot read source weather visual for '{chapter_id}': {exc}",
                publish_blocked=True,
            )
        )
        return

    if size < _MIN_ASSET_SIZE_BYTES:
        result.add_finding(
            ValidationFinding(
                type=FindingType.BLANK_VISUAL_DURING_NARRATION,
                severity=FindingSeverity.CRITICAL,
                message=(
                    f"Source weather visual too small ({size} bytes) for '{chapter_id}', "
                    "likely blank"
                ),
                publish_blocked=True,
            )
        )


def _check_source_resolution(asset_path: Path, chapter_id: str, result: FinalReviewResult) -> None:
    """Check source image has expected 1920x1080 resolution."""
    width, height = _get_image_dimensions(asset_path)
    if width is None or height is None:
        return  # Cannot determine — not blocking for source

    if width != _RESOLUTION_WIDTH or height != _RESOLUTION_HEIGHT:
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_DURATION_MISMATCH,
                severity=FindingSeverity.MAJOR,
                message=(
                    f"Source weather visual for '{chapter_id}' has resolution "
                    f"{width}x{height}, expected {_RESOLUTION_WIDTH}x{_RESOLUTION_HEIGHT}"
                ),
                publish_blocked=False,
            )
        )


# ---------------------------------------------------------------------------
# Rendered segment checks
# ---------------------------------------------------------------------------


def _check_segment_resolution(
    segment_path: Path, chapter_id: str, result: FinalReviewResult
) -> None:
    """Validate rendered segment resolution via ffprobe."""
    width, height = _probe_video_dimensions(segment_path)
    if width is None or height is None:
        # For MP4 segments, ffprobe should work; warn if it doesn't
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_RENDER_FAILED,
                severity=FindingSeverity.WARNING,
                message=(
                    f"Cannot determine rendered segment resolution for '{chapter_id}' "
                    f"(ffprobe unavailable or failed)"
                ),
                publish_blocked=False,
            )
        )
        return

    if width != _RESOLUTION_WIDTH or height != _RESOLUTION_HEIGHT:
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_DURATION_MISMATCH,
                severity=FindingSeverity.MAJOR,
                message=(
                    f"Rendered segment for '{chapter_id}' has resolution "
                    f"{width}x{height}, expected {_RESOLUTION_WIDTH}x{_RESOLUTION_HEIGHT}"
                ),
                publish_blocked=False,
            )
        )


def _check_narration_coverage(
    segment_path: Path, segment: dict, chapter_id: str, result: FinalReviewResult
) -> None:
    """Compare actual rendered segment duration against manifest duration.

    Emits CRITICAL for zero/missing duration, MAJOR for mismatch > 1s.
    """
    manifest_duration = segment.get("duration_seconds", 0)
    if manifest_duration <= 0:
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_SCENE_EMPTY,
                severity=FindingSeverity.CRITICAL,
                message=f"Weather chapter '{chapter_id}' has zero or negative manifest duration",
                publish_blocked=True,
            )
        )
        return

    # Probe actual duration from the rendered segment
    actual_duration = _probe_video_duration(segment_path)
    if actual_duration is None:
        # Cannot probe — non-blocking warning
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_RENDER_FAILED,
                severity=FindingSeverity.WARNING,
                message=(f"Cannot probe actual duration of rendered segment for '{chapter_id}'"),
                publish_blocked=False,
            )
        )
        return

    diff = abs(actual_duration - manifest_duration)
    if diff > _DURATION_MISMATCH_THRESHOLD:
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_DURATION_MISMATCH,
                severity=FindingSeverity.MAJOR,
                message=(
                    f"Weather chapter '{chapter_id}' duration mismatch: "
                    f"rendered={actual_duration:.1f}s vs manifest={manifest_duration:.1f}s "
                    f"(diff={diff:.1f}s)"
                ),
                publish_blocked=False,
            )
        )


# ---------------------------------------------------------------------------
# Stale provenance + source_text_hash verification
# ---------------------------------------------------------------------------


def _check_stale_provenance(
    asset_path: Path,
    chapter_id: str,
    base_dir: Path,
    narration_hashes: dict[str, str],
    result: FinalReviewResult,
) -> None:
    """Check stale marker, renderer version, and source_text_hash drift."""
    # Check .stale marker
    stale_marker = asset_path.with_suffix(asset_path.suffix + ".stale")
    if stale_marker.exists():
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_VISUAL_STALE,
                severity=FindingSeverity.CRITICAL,
                message=f"Weather visual for '{chapter_id}' is marked stale",
                publish_blocked=True,
            )
        )
        return

    # Check provenance file
    provenance_path = asset_path.with_suffix(".provenance.json")
    if not provenance_path.exists():
        return  # No provenance is acceptable (older renders)

    try:
        prov = json.loads(provenance_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    # Renderer version check
    from btcedu.core.weather.models import RENDERER_VERSION

    stored_version = prov.get("renderer_version", "")
    if stored_version and stored_version != RENDERER_VERSION:
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_VISUAL_STALE,
                severity=FindingSeverity.MAJOR,
                message=(
                    f"Weather visual for '{chapter_id}' was rendered with "
                    f"v{stored_version}, current is v{RENDERER_VERSION}"
                ),
                publish_blocked=False,
            )
        )

    # source_text_hash verification: compare provenance hash with current
    # approved narration hash
    stored_hash = prov.get("source_text_hash", "")
    current_hash = narration_hashes.get(chapter_id, "")
    if stored_hash and current_hash and stored_hash != current_hash:
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_VISUAL_STALE,
                severity=FindingSeverity.CRITICAL,
                message=(
                    f"Weather visual for '{chapter_id}' source_text_hash does not match "
                    "current approved narration — asset is stale"
                ),
                publish_blocked=True,
            )
        )


# ---------------------------------------------------------------------------
# Persisted weather_validation.json check
# ---------------------------------------------------------------------------


def _check_weather_validation_json(
    chapter_id: str, base_dir: Path, result: FinalReviewResult
) -> None:
    """Load persisted weather validation findings; propagate blocking ones."""
    validation_path = base_dir / "images" / f"{chapter_id}_weather_validation.json"
    if not validation_path.exists():
        return

    try:
        val_data = json.loads(validation_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return

    findings_list = val_data.get("findings", [])
    for f in findings_list:
        if f.get("publish_blocked"):
            result.add_finding(
                ValidationFinding(
                    type=FindingType.UNSUPPORTED_WEATHER_VISUAL_CLAIM,
                    severity=FindingSeverity.CRITICAL,
                    message=(
                        f"Weather validation for '{chapter_id}' has blocking finding: "
                        f"{f.get('message', f.get('type', 'unsupported_weather_claim'))}"
                    ),
                    publish_blocked=True,
                )
            )


# ---------------------------------------------------------------------------
# Frame analysis on rendered segment
# ---------------------------------------------------------------------------


def _check_rendered_segment_frames(
    segment_path: Path, chapter_id: str, segment: dict, result: FinalReviewResult
) -> None:
    """Analyze rendered segment frames for blank/freeze content.

    For MP4/video segments: sample frames via ffmpeg and detect blank/freeze.
    Falls back to source image check if segment is not a video.
    """
    suffix = segment_path.suffix.lower()

    if suffix in (".mp4", ".webm", ".mkv"):
        _check_video_frames(segment_path, chapter_id, segment, result)
    elif suffix in (".png", ".jpg", ".jpeg", ".webp"):
        _check_still_image(segment_path, chapter_id, segment, result)
    else:
        # Try video analysis first (segments are typically .mp4)
        _check_video_frames(segment_path, chapter_id, segment, result)


def _check_still_image(
    asset_path: Path, chapter_id: str, segment: dict, result: FinalReviewResult
) -> None:
    """Analyze a still image for blank/uniform content."""
    try:
        from PIL import Image
    except ImportError:
        logger.debug("Pillow not available for frame analysis")
        return

    try:
        img = Image.open(asset_path)
    except Exception as exc:
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_RENDER_FAILED,
                severity=FindingSeverity.CRITICAL,
                message=f"Cannot open weather visual for '{chapter_id}': {exc}",
                publish_blocked=True,
            )
        )
        return

    # Check for transparency (RGBA with all-transparent)
    if img.mode == "RGBA":
        alpha_data = img.getchannel("A")
        alpha_extrema = alpha_data.getextrema()
        if alpha_extrema[1] == 0:
            duration = segment.get("duration_seconds", 0)
            if duration > _MAX_BLANK_DURATION_SECONDS:
                result.add_finding(
                    ValidationFinding(
                        type=FindingType.TRANSPARENT_WEATHER_FRAME,
                        severity=FindingSeverity.CRITICAL,
                        message=(
                            f"Weather visual for '{chapter_id}' is fully transparent "
                            f"(covers {duration:.1f}s of narration)"
                        ),
                        publish_blocked=True,
                    )
                )
            return

    # Convert to RGB for analysis
    rgb = img.convert("RGB")
    analysis = _analyze_rgb_image(rgb)
    duration = segment.get("duration_seconds", 0)

    if analysis["is_near_black"] and duration > _MAX_BLANK_DURATION_SECONDS:
        result.add_finding(
            ValidationFinding(
                type=FindingType.BLANK_VISUAL_DURING_NARRATION,
                severity=FindingSeverity.CRITICAL,
                message=(
                    f"Weather visual for '{chapter_id}' is near-black "
                    f"(mean={analysis['mean']:.1f}, covers {duration:.1f}s)"
                ),
                publish_blocked=True,
            )
        )
    elif analysis["is_near_white"] and duration > _MAX_BLANK_DURATION_SECONDS:
        result.add_finding(
            ValidationFinding(
                type=FindingType.BLANK_VISUAL_DURING_NARRATION,
                severity=FindingSeverity.CRITICAL,
                message=(
                    f"Weather visual for '{chapter_id}' is near-white "
                    f"(mean={analysis['mean']:.1f}, covers {duration:.1f}s)"
                ),
                publish_blocked=True,
            )
        )
    elif analysis["is_near_uniform"] and duration > _MAX_BLANK_DURATION_SECONDS:
        result.add_finding(
            ValidationFinding(
                type=FindingType.BLANK_VISUAL_DURING_NARRATION,
                severity=FindingSeverity.CRITICAL,
                message=(
                    f"Weather visual for '{chapter_id}' is near-uniform "
                    f"(std={analysis['std']:.1f}, covers {duration:.1f}s)"
                ),
                publish_blocked=True,
            )
        )


def _check_video_frames(
    video_path: Path, chapter_id: str, segment: dict, result: FinalReviewResult
) -> None:
    """Analyze video frames at intervals for blank/freeze detection."""
    import shutil

    duration = segment.get("duration_seconds", 0)
    if duration <= 0:
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_RENDER_FAILED,
                severity=FindingSeverity.CRITICAL,
                message=f"Cannot inspect weather frames for '{chapter_id}': invalid duration",
                publish_blocked=True,
            )
        )
        return

    ffmpeg_path = shutil.which("ffmpeg")
    if not ffmpeg_path:
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_RENDER_FAILED,
                severity=FindingSeverity.CRITICAL,
                message=f"Cannot inspect weather frames for '{chapter_id}': ffmpeg unavailable",
                publish_blocked=True,
            )
        )
        return

    # Calculate number of samples (cap at _MAX_FRAME_SAMPLES)
    num_frames = max(1, int(duration / 0.5))
    num_frames = min(num_frames, _MAX_FRAME_SAMPLES)

    frames_data = _extract_frame_samples(str(video_path), num_frames, duration, ffmpeg_path)

    if not frames_data:
        result.add_finding(
            ValidationFinding(
                type=FindingType.WEATHER_RENDER_FAILED,
                severity=FindingSeverity.CRITICAL,
                message=f"Cannot inspect weather frames for '{chapter_id}': no frames decoded",
                publish_blocked=True,
            )
        )
        return

    # Actual sample spacing = total duration / number of returned samples
    sample_spacing = duration / len(frames_data)

    # Analyze extracted frames
    blank_frames = 0
    prev_hash: str | None = None
    consecutive_freeze = 0
    max_consecutive_freeze = 0

    for frame_rgb_data, width, height in frames_data:
        analysis = _analyze_raw_rgb(frame_rgb_data, width, height)

        if analysis["is_near_black"] or analysis["is_near_white"] or analysis["is_near_uniform"]:
            blank_frames += 1

        # Freeze detection: compare frame hash with previous
        frame_hash = hashlib.md5(frame_rgb_data).hexdigest()  # noqa: S324
        if prev_hash is not None and frame_hash == prev_hash:
            consecutive_freeze += 1
            max_consecutive_freeze = max(max_consecutive_freeze, consecutive_freeze)
        else:
            consecutive_freeze = 0
        prev_hash = frame_hash

    # Calculate blank duration using actual sample spacing
    blank_duration = blank_frames * sample_spacing

    if blank_duration > _MAX_BLANK_DURATION_SECONDS:
        result.add_finding(
            ValidationFinding(
                type=FindingType.BLANK_VISUAL_DURING_NARRATION,
                severity=FindingSeverity.CRITICAL,
                message=(
                    f"Weather segment for '{chapter_id}' has ~{blank_duration:.1f}s "
                    f"of blank frames ({blank_frames}/{len(frames_data)} sampled)"
                ),
                publish_blocked=True,
            )
        )

    # Freeze: consecutive identical frames spanning > threshold
    freeze_duration = max_consecutive_freeze * sample_spacing
    if freeze_duration > _MAX_BLANK_DURATION_SECONDS and max_consecutive_freeze >= 3:
        result.add_finding(
            ValidationFinding(
                type=FindingType.BLANK_VISUAL_DURING_NARRATION,
                severity=FindingSeverity.MAJOR,
                message=(
                    f"Weather segment for '{chapter_id}' has ~{freeze_duration:.1f}s "
                    "of identical (freeze) frames"
                ),
                publish_blocked=False,
            )
        )


# ---------------------------------------------------------------------------
# ffprobe helpers
# ---------------------------------------------------------------------------


def _probe_video_dimensions(path: Path) -> tuple[int | None, int | None]:
    """Get video dimensions via ffprobe. Returns (width, height) or (None, None)."""
    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "csv=p=0",
            str(path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            parts = proc.stdout.strip().split(",")
            if len(parts) >= 2:
                return int(parts[0]), int(parts[1])
    except (subprocess.TimeoutExpired, ValueError, OSError, FileNotFoundError):
        pass
    return None, None


def _probe_video_duration(path: Path) -> float | None:
    """Get video duration in seconds via ffprobe. Returns None on failure."""
    try:
        cmd = [
            "ffprobe",
            "-v",
            "quiet",
            "-show_entries",
            "format=duration",
            "-of",
            "csv=p=0",
            str(path),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        if proc.returncode == 0 and proc.stdout.strip():
            return float(proc.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError, FileNotFoundError):
        pass
    return None


def _extract_frame_samples(
    video_path: str,
    num_frames: int,
    duration: float,
    ffmpeg_path: str,
) -> list[tuple[bytes, int, int]]:
    """Extract frame samples from video using ffmpeg.

    Returns list of (raw_rgb_bytes, width, height) tuples.
    """
    # Use ffprobe to get resolution
    width, height = 1920, 1080
    dims = _probe_video_dimensions(Path(video_path))
    if dims[0] is not None:
        width, height = dims

    # Extract frames at equal intervals
    results: list[tuple[bytes, int, int]] = []
    frame_size = width * height * 3  # RGB24

    for i in range(num_frames):
        timestamp = (i + 0.5) * (duration / num_frames)
        try:
            cmd = [
                ffmpeg_path,
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                video_path,
                "-vframes",
                "1",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "rgb24",
                "-v",
                "quiet",
                "pipe:1",
            ]
            proc = subprocess.run(cmd, capture_output=True, timeout=15)
            if proc.returncode == 0 and len(proc.stdout) == frame_size:
                results.append((proc.stdout, width, height))
        except (subprocess.TimeoutExpired, OSError):
            continue

    return results


# ---------------------------------------------------------------------------
# Pixel analysis helpers
# ---------------------------------------------------------------------------


def _analyze_rgb_image(img) -> dict:
    """Analyze a PIL RGB Image for blank/uniform content."""
    import statistics

    w, h = img.size
    step_x = max(1, w // 20)
    step_y = max(1, h // 20)

    r_vals = []
    g_vals = []
    b_vals = []
    for x in range(0, w, step_x):
        for y in range(0, h, step_y):
            r, g, b = img.getpixel((x, y))
            r_vals.append(r)
            g_vals.append(g)
            b_vals.append(b)

    if not r_vals:
        return {
            "is_near_black": False,
            "is_near_white": False,
            "is_near_uniform": False,
            "mean": 128.0,
            "std": 50.0,
        }

    r_mean = sum(r_vals) / len(r_vals)
    g_mean = sum(g_vals) / len(g_vals)
    b_mean = sum(b_vals) / len(b_vals)
    overall_mean = (r_mean + g_mean + b_mean) / 3

    all_vals = r_vals + g_vals + b_vals
    std = statistics.stdev(all_vals) if len(all_vals) > 1 else 0.0

    return {
        "is_near_black": overall_mean < _NEAR_BLACK_THRESHOLD,
        "is_near_white": overall_mean > _NEAR_WHITE_THRESHOLD,
        "is_near_uniform": std < _UNIFORMITY_STD_THRESHOLD,
        "mean": overall_mean,
        "std": std,
    }


def _analyze_raw_rgb(data: bytes, width: int, height: int) -> dict:
    """Analyze raw RGB24 frame data for blank/uniform content."""
    import statistics

    total_pixels = width * height
    if len(data) < total_pixels * 3:
        return {
            "is_near_black": False,
            "is_near_white": False,
            "is_near_uniform": False,
            "mean": 128.0,
            "std": 50.0,
        }

    # Sample ~400 pixels evenly distributed
    sample_count = min(400, total_pixels)
    step = max(1, total_pixels // sample_count)

    r_vals = []
    g_vals = []
    b_vals = []
    for i in range(0, total_pixels, step):
        offset = i * 3
        if offset + 2 < len(data):
            r_vals.append(data[offset])
            g_vals.append(data[offset + 1])
            b_vals.append(data[offset + 2])

    if not r_vals:
        return {
            "is_near_black": False,
            "is_near_white": False,
            "is_near_uniform": False,
            "mean": 128.0,
            "std": 50.0,
        }

    r_mean = sum(r_vals) / len(r_vals)
    g_mean = sum(g_vals) / len(g_vals)
    b_mean = sum(b_vals) / len(b_vals)
    overall_mean = (r_mean + g_mean + b_mean) / 3

    all_vals = r_vals + g_vals + b_vals
    std = statistics.stdev(all_vals) if len(all_vals) > 1 else 0.0

    return {
        "is_near_black": overall_mean < _NEAR_BLACK_THRESHOLD,
        "is_near_white": overall_mean > _NEAR_WHITE_THRESHOLD,
        "is_near_uniform": std < _UNIFORMITY_STD_THRESHOLD,
        "mean": overall_mean,
        "std": std,
    }


# ---------------------------------------------------------------------------
# Image dimension helpers (for source assets)
# ---------------------------------------------------------------------------


def _get_image_dimensions(path: Path) -> tuple[int | None, int | None]:
    """Get image dimensions from PNG/JPEG headers, fallback to Pillow."""
    suffix = path.suffix.lower()
    try:
        data = path.read_bytes()
    except OSError:
        return None, None

    # PNG: width/height at bytes 16-24 in IHDR
    if suffix == ".png" and data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w = struct.unpack(">I", data[16:20])[0]
        h = struct.unpack(">I", data[20:24])[0]
        return w, h

    # JPEG: scan for SOF0/SOF2 markers
    if suffix in (".jpg", ".jpeg") and data[:2] == b"\xff\xd8":
        idx = 2
        while idx < len(data) - 9:
            if data[idx] != 0xFF:
                idx += 1
                continue
            marker = data[idx + 1]
            if marker in (0xC0, 0xC2):  # SOF0, SOF2
                h = struct.unpack(">H", data[idx + 5 : idx + 7])[0]
                w = struct.unpack(">H", data[idx + 7 : idx + 9])[0]
                return w, h
            if marker == 0xD9:  # EOI
                break
            if idx + 3 < len(data):
                seg_len = struct.unpack(">H", data[idx + 2 : idx + 4])[0]
                idx += 2 + seg_len
            else:
                break

    # Fallback to Pillow
    try:
        from PIL import Image

        with Image.open(path) as img:
            return img.size
    except Exception:
        return None, None
