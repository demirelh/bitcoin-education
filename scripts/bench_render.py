#!/usr/bin/env python3
"""Compare render speed between the Raspberry Pi and a cloud runner.

Rendering a single episode takes about an hour on the Pi, and measurements
showed that neither the x264 preset nor the hardware encoder is the reason: a
large part of the time is spent feeding 1080p frames through ffmpeg at all.
Before moving the render stage anywhere, the gain has to be measured on the
same input with the same code.

The script is deliberately dependency-free -- ``btcedu.services.ffmpeg_service``
imports nothing but the standard library, so this runs on a bare GitHub runner
without installing the project.

Usage::

    python scripts/bench_render.py                 # all measurements
    python scripts/bench_render.py --seconds 20    # shorter run

Every measurement uses a generated test picture, so the Pi and the runner
process byte-identical input.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from btcedu.config import Settings  # noqa: E402
from btcedu.services.ffmpeg_service import OverlaySpec, create_segment  # noqa: E402

# Defaults come from Settings so the benchmark cannot silently drift away from
# the production render again. The runner has no .env, so it sees the code
# defaults; pass --preset/--crf explicitly to measure a deployment that
# overrides them (the Pi runs ultrafast, not the medium default).
_DEFAULTS = Settings()
RESOLUTION = _DEFAULTS.render_resolution
FPS = _DEFAULTS.render_fps
CRF = _DEFAULTS.render_crf
PRESET = _DEFAULTS.render_preset


def _run(cmd: list[str]) -> None:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed ({result.returncode}): {result.stderr[-800:]}")


def _timed(label: str, func) -> tuple[str, float]:
    start = time.monotonic()
    func()
    elapsed = round(time.monotonic() - start, 1)
    print(f"  {label}: {elapsed}s", flush=True)
    return label, elapsed


def host_info() -> dict[str, str]:
    """What machine produced these numbers."""
    model = ""
    try:
        for line in Path("/proc/cpuinfo").read_text(encoding="utf-8").splitlines():
            if line.startswith(("Model", "model name")):
                model = line.split(":", 1)[1].strip()
                break
    except OSError:
        pass
    ffmpeg_version = ""
    try:
        out = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True).stdout
        ffmpeg_version = out.splitlines()[0] if out else ""
    except FileNotFoundError:
        ffmpeg_version = "missing"
    return {
        "host": os.environ.get("GITHUB_RUNNER_NAME") or platform.node(),
        "machine": platform.machine(),
        "cpu": model,
        "cores": str(os.cpu_count() or 0),
        "ffmpeg": ffmpeg_version,
        "environment": "github-actions" if os.environ.get("GITHUB_ACTIONS") else "local",
    }


def prepare_inputs(workdir: Path, seconds: float) -> tuple[Path, Path]:
    """A deterministic 1080p picture and a silent narration track."""
    image = workdir / "bench.png"
    audio = workdir / "bench.m4a"
    _run(
        # fmt: off
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"testsrc2=size={RESOLUTION}:duration=1:rate=1",
            "-frames:v",
            "1",
            str(image),
        ]
        # fmt: on
    )
    _run(
        # fmt: off
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-t",
            str(seconds),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            str(audio),
        ]
        # fmt: on
    )
    return image, audio


def bench_frame_pipeline(image: Path, seconds: float) -> None:
    """Frames only, no encoding: the cost of moving pixels at all."""
    _run(
        # fmt: off
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-loop",
            "1",
            "-i",
            str(image),
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(FPS),
            "-t",
            str(seconds),
            "-f",
            "null",
            "-",
        ]
        # fmt: on
    )


def bench_raw_encode(image: Path, output: Path, seconds: float) -> None:
    """Plain still-image encode with the production preset and quality."""
    _run(
        # fmt: off
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-loop",
            "1",
            "-i",
            str(image),
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=44100:cl=stereo",
            "-vf",
            f"scale={RESOLUTION.replace('x', ':')}:force_original_aspect_ratio="
            f"decrease,pad={RESOLUTION.split('x')[0]}:{RESOLUTION.split('x')[1]}:"
            "(ow-iw)/2:(oh-ih)/2,format=yuv420p",
            "-c:v",
            "libx264",
            "-preset",
            PRESET,
            "-crf",
            str(CRF),
            "-r",
            str(FPS),
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            "-t",
            str(seconds),
            str(output),
        ]
        # fmt: on
    )


def bench_pipeline_segment(image: Path, audio: Path, output: Path, seconds: float) -> None:
    """The real production path: btcedu's own segment renderer with overlays.

    This is the number that matters -- it includes the drawtext overlays, the
    fades and the colour correction the bulletin actually uses.
    """
    overlays = [
        OverlaySpec(
            text="EMEKLILIK REFORMU",
            overlay_type="lower_third",
            fontsize=54,
            fontcolor="white",
            font="NotoSans-Bold",
            position="bottom_center",
            start=1.0,
            end=7.0,
        )
    ]
    create_segment(
        image_path=str(image),
        audio_path=str(audio),
        output_path=str(output),
        duration=seconds,
        overlays=overlays,
        resolution=RESOLUTION,
        fps=FPS,
        crf=CRF,
        preset=PRESET,
        animated_lower_thirds=True,
        color_correction=True,
        fade_in_duration=0.5,
        timeout_seconds=3600,
    )


def main() -> int:
    # The bench_* helpers read these module-level values; --fps etc. override them.
    global RESOLUTION, FPS, CRF, PRESET

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seconds",
        type=float,
        default=60.0,
        help="Length of the rendered test segment (default: 60)",
    )
    parser.add_argument(
        "--workdir",
        default="",
        help="Where to write the benchmark files (default: a temp directory)",
    )
    parser.add_argument("--fps", type=int, default=FPS, help=f"Frame rate (default: {FPS})")
    parser.add_argument("--crf", type=int, default=CRF, help=f"x264 CRF (default: {CRF})")
    parser.add_argument("--preset", default=PRESET, help=f"x264 preset (default: {PRESET})")
    parser.add_argument(
        "--resolution", default=RESOLUTION, help=f"Output resolution (default: {RESOLUTION})"
    )
    args = parser.parse_args()

    RESOLUTION, FPS, CRF, PRESET = args.resolution, args.fps, args.crf, args.preset

    if not shutil.which("ffmpeg"):
        print("ffmpeg not found on PATH", file=sys.stderr)
        return 2

    workdir = Path(args.workdir) if args.workdir else Path("/tmp/btcedu-bench")
    workdir.mkdir(parents=True, exist_ok=True)

    info = host_info()
    print("Host:")
    for key, value in info.items():
        print(f"  {key}: {value}")
    print(
        f"\nRendering {args.seconds:.0f}s at {RESOLUTION}@{FPS} (preset={PRESET}, crf={CRF})",
        flush=True,
    )

    image, audio = prepare_inputs(workdir, args.seconds)
    results = dict(
        [
            _timed("frame_pipeline_no_encode", lambda: bench_frame_pipeline(image, args.seconds)),
            _timed(
                "raw_ffmpeg_encode",
                lambda: bench_raw_encode(image, workdir / "raw.mp4", args.seconds),
            ),
            _timed(
                "btcedu_create_segment",
                lambda: bench_pipeline_segment(image, audio, workdir / "segment.mp4", args.seconds),
            ),
        ]
    )

    realtime_factor = round(results["btcedu_create_segment"] / args.seconds, 2)
    # A ten-minute bulletin is the actual production workload.
    projected_minutes = round(realtime_factor * 600 / 60, 1)
    document = {
        "host": info,
        "settings": {
            "seconds": args.seconds,
            "resolution": RESOLUTION,
            "fps": FPS,
            "crf": CRF,
            "preset": PRESET,
        },
        "seconds_per_measurement": results,
        "realtime_factor": realtime_factor,
        "projected_minutes_for_10min_bulletin": projected_minutes,
    }
    print("\n" + json.dumps(document, indent=2))

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        lines = [
            "## Render benchmark",
            "",
            f"`{info['cpu'] or info['machine']}` · {info['cores']} cores · {info['environment']}",
            "",
            "| Measurement | Seconds |",
            "| --- | ---: |",
            *(f"| {name} | {value} |" for name, value in results.items()),
            "",
            f"**Realtime factor:** {realtime_factor}x → a 10-minute bulletin takes about "
            f"**{projected_minutes} minutes**.",
            "",
            "Raspberry Pi 4 reference: 6.1x → about 54 minutes.",
        ]
        with open(summary_path, "a", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
