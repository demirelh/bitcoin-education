"""Fetching a finished avatar clip, and refusing to believe it until probed.

Two mistakes are easy to make here and both are expensive. The first is reading
a video into memory: a ninety-second 1080p clip is tens of megabytes, and this
runs on a Raspberry Pi that also has ffmpeg open. The second is trusting the
download because the HTTP call returned 200 — a truncated file, an HTML error
page served with the wrong content type, or an opaque MP4 where a transparent
WebM was ordered all pass that test and fail much later, in the renderer, on a
clip that has already been paid for.

So: stream to a temporary file next to the target, cap the size, probe it with
ffprobe, and only then move it into place. A file that fails the probe is
quarantined rather than deleted — the bytes are the only evidence of what the
provider actually sent, and the job is billed either way.

The download URL itself is treated as what it is: a short-lived, signed handle.
It is never persisted, never logged and never handed to a browser. When it
expires, the answer is another read-only status call, never another generation.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Nothing the presenter says in one scene justifies more than this. A larger
#: response is a mistake or an attack, not a clip.
DEFAULT_MAX_BYTES = 512 * 1024 * 1024
DEFAULT_CHUNK_BYTES = 256 * 1024
#: A clip this small cannot contain a rendered person; it is an error page.
MIN_PLAUSIBLE_BYTES = 4096

VALIDATION_VALID = "valid"
VALIDATION_INVALID = "invalid"
VALIDATION_QUARANTINED = "quarantined"

#: Pixel formats that actually carry an alpha channel. A WebM without one is
#: not a transparent presenter, whatever the container says.
ALPHA_PIXEL_FORMATS = frozenset(
    {"yuva420p", "yuva422p", "yuva444p", "yuva420p10le", "rgba", "bgra", "argb", "abgr"}
)


class DownloadError(RuntimeError):
    """The bytes could not be fetched."""


class ValidationError(RuntimeError):
    """The bytes were fetched but are not the clip that was ordered."""

    def __init__(self, message: str, quarantine_path: Path | None = None):
        self.quarantine_path = quarantine_path
        super().__init__(message)


@dataclass(frozen=True)
class ClipProbe:
    """What ffprobe found in a downloaded clip."""

    width: int
    height: int
    fps: float
    duration_seconds: float
    container: str
    video_codec: str
    pixel_format: str
    has_alpha: bool
    has_audio: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "width": self.width,
            "height": self.height,
            "fps": round(self.fps, 3),
            "duration_seconds": round(self.duration_seconds, 3),
            "container": self.container,
            "video_codec": self.video_codec,
            "pixel_format": self.pixel_format,
            "has_alpha": self.has_alpha,
            "has_audio": self.has_audio,
        }


@dataclass(frozen=True)
class ClipExpectation:
    """What the ordered clip has to look like for the renderer to use it."""

    output_format: str = "mp4"
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    expected_duration_seconds: float = 0.0
    #: Generous on purpose. The provider decides the exact length from the
    #: audio; we only want to catch a clip that is obviously the wrong one.
    duration_tolerance_ratio: float = 0.25
    duration_tolerance_seconds: float = 2.0
    require_alpha: bool = False
    fps_tolerance: float = 0.5


@dataclass
class DownloadResult:
    """A clip that is on disk and has been checked."""

    path: Path
    size_bytes: int
    sha256: str
    probe: ClipProbe
    warnings: list[str] = field(default_factory=list)


def _iter_response_chunks(response, chunk_bytes: int) -> Iterable[bytes]:
    iterator = getattr(response, "iter_content", None)
    if iterator is None:  # pragma: no cover - requests always provides it
        raise DownloadError("download response does not support streaming")
    return iterator(chunk_size=chunk_bytes)


def stream_to_file(
    url: str,
    destination: Path,
    *,
    opener: Callable[[str], Any] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    chunk_bytes: int = DEFAULT_CHUNK_BYTES,
    timeout_seconds: float = 120.0,
) -> tuple[int, str]:
    """Stream a URL into ``destination`` atomically, hashing as it goes.

    Returns ``(size_bytes, sha256)``. The bytes land in a ``.part`` file beside
    the target and are renamed only after the last chunk, so an interrupted
    download can never be mistaken for a finished clip.
    """
    if not url:
        raise DownloadError("completed job has no download URL")

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".part")

    if opener is None:

        def opener(target: str):  # type: ignore[misc]
            import requests

            return requests.get(target, stream=True, timeout=timeout_seconds)

    response = opener(url)
    status_code = getattr(response, "status_code", 200)
    if isinstance(status_code, int) and status_code >= 400:
        raise DownloadError(f"download failed with HTTP {status_code}")

    digest = hashlib.sha256()
    written = 0
    try:
        with temporary.open("wb") as handle:
            for chunk in _iter_response_chunks(response, chunk_bytes):
                if not chunk:
                    continue
                written += len(chunk)
                if written > max_bytes:
                    raise DownloadError(
                        f"download exceeded the {max_bytes} byte ceiling; aborted"
                    )
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    finally:
        closer = getattr(response, "close", None)
        if callable(closer):
            closer()

    if written < MIN_PLAUSIBLE_BYTES:
        temporary.unlink(missing_ok=True)
        raise DownloadError(
            f"download is only {written} bytes, which cannot be a rendered clip"
        )

    os.replace(temporary, destination)
    return written, digest.hexdigest()


def _parse_fps(value: str) -> float:
    text = str(value or "").strip()
    if not text or text in {"0/0", "N/A"}:
        return 0.0
    if "/" in text:
        numerator, _, denominator = text.partition("/")
        try:
            den = float(denominator)
            return float(numerator) / den if den else 0.0
        except ValueError:
            return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def probe_clip(
    path: Path,
    *,
    runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
) -> ClipProbe:
    """Ask ffprobe what this file really is."""
    command = [
        "ffprobe",
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    if runner is None:
        # Only the real path needs a real binary. An injected runner is the
        # test seam and must not be second-guessed.
        if shutil.which("ffprobe") is None:
            raise ValidationError("ffprobe is not available; a clip cannot be validated")

        def runner(cmd: list[str]) -> subprocess.CompletedProcess:  # type: ignore[misc]
            return subprocess.run(cmd, capture_output=True, text=True, timeout=120)

    completed = runner(command)
    if completed.returncode != 0:
        raise ValidationError(
            f"ffprobe rejected the downloaded clip: {(completed.stderr or '').strip()[:300]}"
        )
    try:
        payload = json.loads(completed.stdout or "{}")
    except (TypeError, ValueError) as exc:
        raise ValidationError("ffprobe returned output that is not JSON") from exc

    streams = payload.get("streams") or []
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    if video is None:
        raise ValidationError("downloaded clip contains no video stream")
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    fmt = payload.get("format") or {}
    try:
        duration = float(fmt.get("duration") or video.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0

    pixel_format = str(video.get("pix_fmt") or "").lower()
    return ClipProbe(
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=_parse_fps(video.get("avg_frame_rate") or video.get("r_frame_rate") or ""),
        duration_seconds=duration,
        container=str(fmt.get("format_name") or "").lower(),
        video_codec=str(video.get("codec_name") or "").lower(),
        pixel_format=pixel_format,
        has_alpha=pixel_format in ALPHA_PIXEL_FORMATS,
        has_audio=audio is not None,
    )


def validate_clip(probe: ClipProbe, expectation: ClipExpectation) -> list[str]:
    """Check a probed clip against the order. Raises on anything fatal.

    Returns a list of warnings for the differences that are worth telling an
    operator about but do not make the clip unusable.
    """
    warnings: list[str] = []
    container = probe.container

    wanted = expectation.output_format.lower()
    if wanted == "webm" and "webm" not in container and "matroska" not in container:
        raise ValidationError(f"expected a WebM clip, ffprobe reports container {container!r}")
    if wanted == "mp4" and not {"mp4", "mov", "m4a", "3gp", "isom"} & set(container.split(",")):
        raise ValidationError(f"expected an MP4 clip, ffprobe reports container {container!r}")

    if expectation.require_alpha and not probe.has_alpha:
        # The whole point of the transparent path. An opaque clip would be
        # composited as a rectangle over the studio, which is worse than a stop.
        raise ValidationError(
            "transparent presenter was ordered but the clip has no alpha channel "
            f"(pixel format {probe.pixel_format!r})"
        )

    if probe.width <= 0 or probe.height <= 0:
        raise ValidationError("downloaded clip has no usable resolution")
    if expectation.width and expectation.height:
        if (probe.width, probe.height) != (expectation.width, expectation.height):
            raise ValidationError(
                f"clip is {probe.width}x{probe.height}, "
                f"expected {expectation.width}x{expectation.height}"
            )

    if expectation.fps and probe.fps:
        if abs(probe.fps - expectation.fps) > expectation.fps_tolerance:
            warnings.append(
                f"clip runs at {probe.fps:.2f} fps, renderer expects {expectation.fps:.2f}"
            )

    if probe.duration_seconds <= 0:
        raise ValidationError("downloaded clip has no duration")
    expected = expectation.expected_duration_seconds
    if expected > 0:
        allowed = max(
            expectation.duration_tolerance_seconds,
            expected * expectation.duration_tolerance_ratio,
        )
        if abs(probe.duration_seconds - expected) > allowed:
            warnings.append(
                f"clip is {probe.duration_seconds:.1f}s, narration suggested {expected:.1f}s"
            )
    return warnings


def quarantine(path: Path, reason: str) -> Path:
    """Move a rejected clip aside instead of deleting it.

    It was paid for. Whatever is wrong with it is evidence, both for the
    reconciliation conversation with the provider and for working out whether
    our expectation or their output is the thing that is wrong.
    """
    target_dir = path.parent / "quarantine"
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / path.name
    counter = 1
    while target.exists():
        target = target_dir / f"{path.stem}.{counter}{path.suffix}"
        counter += 1
    if path.exists():
        os.replace(path, target)
    (target.with_suffix(target.suffix + ".reason.txt")).write_text(
        f"{reason}\n", encoding="utf-8"
    )
    logger.error("Quarantined avatar clip %s: %s", target.name, reason)
    return target


def download_and_validate(
    url: str,
    destination: Path,
    expectation: ClipExpectation,
    *,
    opener: Callable[[str], Any] | None = None,
    probe_runner: Callable[[list[str]], subprocess.CompletedProcess] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    timeout_seconds: float = 120.0,
) -> DownloadResult:
    """Stream, probe and accept one clip — or quarantine it and refuse."""
    size, digest = stream_to_file(
        url,
        destination,
        opener=opener,
        max_bytes=max_bytes,
        timeout_seconds=timeout_seconds,
    )
    try:
        probe = probe_clip(destination, runner=probe_runner)
        warnings = validate_clip(probe, expectation)
    except ValidationError as exc:
        held = quarantine(destination, str(exc))
        raise ValidationError(str(exc), quarantine_path=held) from exc

    return DownloadResult(
        path=destination,
        size_bytes=size,
        sha256=digest,
        probe=probe,
        warnings=warnings,
    )
