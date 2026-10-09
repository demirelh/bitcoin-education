#!/usr/bin/env python3
"""Run one transcription job on a GitHub Actions runner.

The Pi packs an audio file (see ``btcedu/core/remote_transcribe.py``); this
script unpacks it and calls ``FasterWhisperTranscriptionProvider`` -- the same
code the Pi runs locally -- and writes the segments back as JSON.

Usage:
    python scripts/transcribe_job.py --job transcribe-job.tar.gz --out result/transcript.json
"""

import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from btcedu.core.remote_transcribe import read_job_package, transcript_to_dict  # noqa: E402
from btcedu.services.transcription_service import (  # noqa: E402
    FasterWhisperTranscriptionProvider,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--job", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    with tempfile.TemporaryDirectory(prefix="transcribe-job-") as tmp:
        job, audio = read_job_package(args.job, Path(tmp))
        started = time.monotonic()
        transcript = FasterWhisperTranscriptionProvider().transcribe(
            str(audio), model=job["model"], language=job["language"]
        )
        elapsed = time.monotonic() - started

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(
            {
                "model": job["model"],
                "commit": job["commit"],
                "elapsed_seconds": round(elapsed, 1),
                "transcript": transcript_to_dict(transcript),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    print(
        f"Transcribed {transcript.audio_seconds:.0f}s of audio into "
        f"{len(transcript.segments)} segments in {elapsed:.0f}s with {job['model']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
