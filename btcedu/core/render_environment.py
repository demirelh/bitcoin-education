"""WP-8C: the part of a render that is not in any file.

Every byte-bound input is recorded by :mod:`btcedu.core.render_inputs`. What is
not recorded there is the machine that drew the video: which ffmpeg built the
frames, which encoder it chose, which font file the name ``NotoSans-Bold``
actually resolved to on this box. Two runs from identical inputs can differ in
all of those, and nothing in the manifest would say so.

This module writes that down. It deliberately does **not** feed the render
content hash: the hash has to mean the same thing on the Pi and on a GitHub
runner, and folding the ffmpeg build into it would make every remote render
permanently stale — the exact opposite of what the remote path is for. The
environment is evidence for the final review, not an identity for the artefact.

The other reason it exists: a remote result is rebased onto local paths when it
comes back, and after that it would otherwise be indistinguishable from a local
render. :func:`describe` is recorded on both sides so the take-back can keep the
runner's description under its own key and the origin stays visible.
"""

from __future__ import annotations

import logging
import platform
import subprocess
from functools import lru_cache

logger = logging.getLogger(__name__)

#: Filters the render actually relies on. A build without one of these does not
#: fail mysteriously halfway through a nine-minute bulletin; it is visible here.
RELEVANT_FILTERS: tuple[str, ...] = (
    "concat",
    "drawtext",
    "loudnorm",
    "overlay",
    "scale",
    "subtitles",
    "zoompan",
)

#: The codecs the render names explicitly (see the manifest's ``codec`` block).
RELEVANT_ENCODERS: tuple[str, ...] = ("libx264", "aac", "libvpx-vp9")
RELEVANT_DECODERS: tuple[str, ...] = ("h264", "aac", "vp9", "png", "mjpeg")

_UNKNOWN = "unknown"


def _first_line(command: list[str]) -> str:
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Could not read a version for %s: %s", command[0], exc)
        return _UNKNOWN
    if result.returncode != 0:
        return _UNKNOWN
    return (result.stdout.split("\n")[0] or "").strip() or _UNKNOWN


def _available(kind: str, wanted: tuple[str, ...]) -> list[str]:
    """Which of the names we care about this build actually offers.

    Only the names in ``wanted`` are reported. Listing every filter an ffmpeg
    build carries would be several kilobytes of noise in every manifest and
    would churn on any distribution upgrade.
    """
    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", f"-{kind}"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("Could not list %s: %s", kind, exc)
        return []
    if result.returncode != 0:
        return []
    text = result.stdout
    found = []
    for name in wanted:
        # The listings are columnar ("V..... zoompan  Apply Zoom & Pan"), so a
        # whitespace-delimited token match avoids matching a description word.
        if any(name in line.split() for line in text.splitlines()):
            found.append(name)
    return found


def _font_identity(font_name: str) -> dict[str, str]:
    """What the configured font name resolves to here.

    ``find_font_path`` returns a real path when the file is on this machine and
    the bare name when it falls through to fontconfig. The difference matters:
    a name is resolved by the renderer's own machine at draw time, so two boxes
    can legitimately draw different glyphs from the same manifest.
    """
    from btcedu.services.ffmpeg_service import find_font_path

    try:
        resolved = str(find_font_path(font_name) or "")
    except Exception as exc:  # noqa: BLE001 - a font probe may not break a render
        logger.debug("Could not resolve font %s: %s", font_name, exc)
        resolved = ""
    return {
        "name": font_name,
        "resolved": resolved or _UNKNOWN,
        "resolution": "file" if resolved.startswith("/") else "fontconfig",
    }


@lru_cache(maxsize=8)
def _probe(font_name: str) -> tuple:
    """The expensive half, cached: five subprocesses per render, not per call."""
    return (
        _first_line(["ffmpeg", "-version"]),
        _first_line(["ffprobe", "-version"]),
        tuple(_available("filters", RELEVANT_FILTERS)),
        tuple(_available("encoders", RELEVANT_ENCODERS)),
        tuple(_available("decoders", RELEVANT_DECODERS)),
        tuple(sorted(_font_identity(font_name).items())),
    )


def renderer_version() -> str:
    try:
        from importlib.metadata import version

        return version("btcedu")
    except Exception:  # noqa: BLE001 - an editable install may not be registered
        return _UNKNOWN


def describe(font_name: str = "", *, origin: str = "local") -> dict:
    """The system inputs of one render, in a form safe to store and publish.

    No hostname, no user, no absolute home directory — the platform is reported
    as the system and machine architecture only, which is what a reviewer needs
    to explain a visual difference and is not an operational detail worth
    leaking into an artefact.
    """
    ffmpeg, ffprobe, filters, encoders, decoders, font = _probe(font_name or "")
    return {
        "origin": origin,
        "renderer_version": renderer_version(),
        "ffmpeg_version": ffmpeg,
        "ffprobe_version": ffprobe,
        "filters": list(filters),
        "encoders": list(encoders),
        "decoders": list(decoders),
        "font": dict(font),
        "platform": {
            "system": platform.system(),
            "machine": platform.machine(),
            "python": platform.python_version(),
        },
    }


def differences(left: dict | None, right: dict | None) -> list[str]:
    """Where two render environments disagree, in reviewer-readable form.

    Used when a remote result comes back: the reviewer is told what was
    different about the machine that drew the video, rather than being left to
    assume it was this one.
    """
    if not isinstance(left, dict) or not isinstance(right, dict):
        return []
    out: list[str] = []
    for key in ("renderer_version", "ffmpeg_version", "ffprobe_version"):
        if left.get(key) != right.get(key):
            out.append(f"{key}: {left.get(key, _UNKNOWN)} vs {right.get(key, _UNKNOWN)}")
    for key in ("filters", "encoders", "decoders"):
        if sorted(left.get(key) or []) != sorted(right.get(key) or []):
            out.append(f"{key}: {sorted(left.get(key) or [])} vs {sorted(right.get(key) or [])}")
    left_font = (left.get("font") or {}).get("resolved")
    right_font = (right.get("font") or {}).get("resolved")
    if left_font != right_font:
        out.append(f"font: {left_font} vs {right_font}")
    if (left.get("platform") or {}) != (right.get("platform") or {}):
        out.append(f"platform: {left.get('platform')} vs {right.get('platform')}")
    return out
