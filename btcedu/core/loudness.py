"""Levelling audio to a single, known loudness.

Why this is shared
------------------
The narration is levelled to -15 LUFS with great care (see
:mod:`btcedu.core.tts`), because ElevenLabs returns the same voice up to 18 dB
apart between generations. Nothing did the same for the sting that opens the
video: the intro asset was mixed at -13.2 LUFS with a true peak of **+0.56
dBTP** — above full scale — and went into the encoder untouched, with only a
trim and two fades. It therefore arrived both louder than the speech that
follows it and clipped, which is exactly what "the intro sounds shrill" means.

So both callers now level through the same two-pass measurement, and the
parameters that differ between speech and music are arguments rather than
copies of this function.

Failure is never fatal here. Levelling is an improvement, not a precondition:
every failure path leaves the file exactly as it was.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

#: Where everything is levelled to. The figure sits just under YouTube's own
#: -14 LUFS target, so the platform has nothing left to turn down.
TARGET_LUFS = -15.0

#: True peak is never allowed above this. Gain is given up rather than clipped.
PEAK_CEILING_DBFS = -1.5

#: Below this a gain change is inaudible and not worth re-encoding for.
MIN_GAIN_DB = 0.3

#: Loudness range handed to the levelling filter. Speech from a single voice
#: sits well inside this, so it never squeezes the delivery.
LOUDNESS_RANGE_LU = 11.0

#: What the pipeline works at throughout. Kept through levelling so segments
#: still join without a resample.
SAMPLE_RATE = 44100


def measure_loudness(path: Path) -> tuple[float, float] | None:
    """Integrated loudness and true peak of *path*, in LUFS and dBFS.

    Returns None when the file cannot be analysed or holds too little sound to
    give an integrated reading — levelling on a guess would be worse than not
    levelling at all.
    """
    try:
        result = subprocess.run(
            # Note the log level: ffmpeg prints the ebur128 summary at info,
            # so quietening it the way the rest of the pipeline does would
            # throw away the very numbers being asked for.
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-v",
                "info",
                "-i",
                str(path),
                "-af",
                "ebur128=peak=true",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        logger.debug("Could not measure the loudness of %s: %s", path, exc)
        return None

    loudness = re.findall(r"I:\s+(-?[0-9.]+) LUFS", result.stderr or "")
    peaks = re.findall(r"Peak:\s+(-?[0-9.]+) dBFS", result.stderr or "")
    if not loudness or not peaks:
        return None
    try:
        integrated, peak = float(loudness[-1]), float(peaks[-1])
    except ValueError:
        return None
    # ffmpeg reports -inf for silence as a very large negative number.
    if integrated < -70.0 or peak < -70.0:
        return None
    return integrated, peak


def parse_loudnorm_json(stderr: str) -> dict[str, str] | None:
    """The measurement block loudnorm prints after its analysis pass.

    It is the last JSON object in the output; anything before it belongs to
    ffmpeg itself. Returns None if the block is missing or incomplete, which
    simply means the file is left at the level it came in at.
    """
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        stats = json.loads(stderr[start : end + 1])
    except ValueError:
        return None
    required = ("input_i", "input_tp", "input_lra", "input_thresh", "target_offset")
    if not all(key in stats for key in required):
        return None
    # Digitally silent audio measures as -inf and cannot be levelled.
    if any(str(stats[key]).lstrip("-").startswith("inf") for key in required):
        return None
    return stats


def normalize_loudness(
    path: Path,
    *,
    target_lufs: float = TARGET_LUFS,
    peak_ceiling_dbfs: float = PEAK_CEILING_DBFS,
    channels: int = 1,
    sample_rate: int = SAMPLE_RATE,
) -> float | None:
    """Level *path* to *target_lufs* in place. Returns the change, in dB.

    Two-pass EBU R128: the file is measured first and the correction applied
    with those figures, which makes it a single linear gain over the whole
    file. The delivery is therefore left exactly as it was and only its level
    is corrected — a one-pass filter would ride the gain within a sentence and
    audibly pump.

    The peak ceiling is what stops quiet material from clipping when it is
    lifted, and what pulls an over-hot master back below full scale.

    A file is re-encoded when its loudness is off target **or** when it peaks
    above the ceiling. The second condition is not redundant: the intro sting
    measured -13.2 LUFS at +0.56 dBTP, and material that is on target for
    loudness can still be mastered hot enough to clip.

    Returns None when nothing was changed.
    """
    measured = measure_loudness(path)
    if measured is None:
        return None
    integrated, peak = measured
    on_target = abs(target_lufs - integrated) < MIN_GAIN_DB
    if on_target and peak <= peak_ceiling_dbfs:
        return None

    common = f"I={target_lufs}:TP={peak_ceiling_dbfs}:LRA={LOUDNESS_RANGE_LU}"
    # The extension has to stay .mp3: ffmpeg picks the output format from it,
    # and anything else makes the encode fail rather than the level change.
    levelled = path.with_name(path.name + ".levelled.mp3")
    try:
        analysis = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-nostats",
                "-v",
                "info",
                "-i",
                str(path),
                "-af",
                f"loudnorm={common}:print_format=json",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        stats = parse_loudnorm_json(analysis.stderr or "")
        if stats is None:
            return None

        result = subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-y",
                "-i",
                str(path),
                "-af",
                f"loudnorm={common}:linear=true"
                f":measured_I={stats['input_i']}"
                f":measured_TP={stats['input_tp']}"
                f":measured_LRA={stats['input_lra']}"
                f":measured_thresh={stats['input_thresh']}"
                f":offset={stats['target_offset']}",
                # loudnorm works at 192 kHz internally; without this the file
                # would come back resampled and no longer match its siblings.
                "-ar",
                str(sample_rate),
                "-ac",
                str(channels),
                "-c:a",
                "libmp3lame",
                "-q:a",
                "2",
                str(levelled),
            ],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0 or not levelled.exists() or levelled.stat().st_size == 0:
            logger.debug("Could not level %s: %s", path, (result.stderr or "")[:200])
            levelled.unlink(missing_ok=True)
            return None
        os.replace(levelled, path)
    except (OSError, subprocess.SubprocessError) as exc:  # noqa: BLE001
        logger.debug("Could not level %s: %s", path, exc)
        levelled.unlink(missing_ok=True)
        return None

    return target_lufs - integrated
