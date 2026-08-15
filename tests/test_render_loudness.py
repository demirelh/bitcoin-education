"""Levelling the stings that top and tail the video.

The narration is levelled take by take to -15 LUFS. Nothing did the same for
the sting assets, and the one in use shipped hot: -13.2 LUFS with a true peak
of **+0.56 dBTP**, above full scale. It reached the encoder with only a trim
and two fades applied, so it was simultaneously louder than the narration it
introduces and clipping — which is what "the intro sounds shrill" meant.

These tests pin the two halves of that: an over-hot asset is pulled below the
ceiling, and the result lands on the same target the speech does.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from btcedu.core import loudness
from btcedu.core.renderer import _levelled_sting

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is not installed")


#: ffmpeg's ``sine`` peaks at -18 dBFS, so the gains below are the measured
#: distance from there to the level each test needs.
_SINE_PEAK_DBFS = -18.0


def _sting(path: Path, *, volume: str, seconds: float = 3.0) -> bytes:
    """A short stereo sting at a chosen level, like the real asset."""
    subprocess.run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:duration={seconds}:sample_rate=44100",
            "-af",
            f"volume={volume},pan=stereo|c0=c0|c1=c0",
            "-c:a",
            "libmp3lame",
            "-q:a",
            "2",
            str(path),
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return path.read_bytes()


@needs_ffmpeg
class TestTheStingIsLevelledLikeTheSpeech:
    def test_an_over_hot_sting_is_pulled_below_the_ceiling(self, tmp_path: Path) -> None:
        """The real asset peaked at +0.56 dBTP and was clipped by the encoder."""
        raw = _sting(tmp_path / "hot.mp3", volume="7.5")  # peaks around -0.5 dBFS

        written = _levelled_sting(raw, tmp_path / "inputs" / "intro.mp3", "intro")

        assert written is not None
        after = loudness.measure_loudness(written)
        assert after is not None
        assert after[1] <= loudness.PEAK_CEILING_DBFS + 0.5

    def test_the_sting_lands_on_the_same_target_as_the_narration(self, tmp_path: Path) -> None:
        """A sting 1.8 dB louder than the speech is audible as a jump."""
        raw = _sting(tmp_path / "loud.mp3", volume="1.9")  # about -13 LUFS

        written = _levelled_sting(raw, tmp_path / "inputs" / "intro.mp3", "intro")

        assert written is not None
        after = loudness.measure_loudness(written)
        assert after is not None
        assert abs(after[0] - loudness.TARGET_LUFS) < 1.5

    def test_a_quiet_sting_is_brought_up_too(self, tmp_path: Path) -> None:
        raw = _sting(tmp_path / "quiet.mp3", volume="0.05")

        written = _levelled_sting(raw, tmp_path / "inputs" / "intro.mp3", "intro")

        assert written is not None
        after = loudness.measure_loudness(written)
        assert after is not None
        assert abs(after[0] - loudness.TARGET_LUFS) < 1.5

    def test_the_sting_stays_stereo_and_at_the_pipeline_sample_rate(self, tmp_path: Path) -> None:
        """Levelling must not quietly turn the sting into mono or resample it."""
        raw = _sting(tmp_path / "hot.mp3", volume="7.5")

        written = _levelled_sting(raw, tmp_path / "inputs" / "intro.mp3", "intro")

        assert written is not None
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=channels,sample_rate",
                "-of",
                "csv=p=0",
                str(written),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert set(probe.stdout.strip().split(",")) == {"2", "44100"}


class TestLevellingNeverFailsARender:
    def test_no_asset_means_no_snapshot(self, tmp_path: Path) -> None:
        assert _levelled_sting(None, tmp_path / "inputs" / "intro.mp3", "intro") is None

    def test_unreadable_audio_is_kept_as_delivered(self, tmp_path: Path) -> None:
        """A sting at the wrong level is still better than a failed render."""
        written = _levelled_sting(b"not audio at all", tmp_path / "inputs" / "intro.mp3", "intro")

        assert written is not None
        assert written.read_bytes() == b"not audio at all"

    def test_a_crash_in_the_leveller_is_survived(self, tmp_path: Path, monkeypatch) -> None:
        def explode(*args: object, **kwargs: object) -> float | None:
            raise RuntimeError("ffmpeg went missing")

        monkeypatch.setattr(loudness, "normalize_loudness", explode)

        written = _levelled_sting(b"payload", tmp_path / "inputs" / "intro.mp3", "intro")

        assert written is not None
        assert written.read_bytes() == b"payload"


@needs_ffmpeg
class TestTheCeilingIsCheckedIndependently:
    def test_material_on_target_but_clipping_is_still_corrected(self, tmp_path: Path) -> None:
        """The asset's fault was its peak, not its loudness — both must count.

        A file whose integrated loudness already sits on target would be left
        alone by a loudness-only check, however hot its peaks are.
        """
        path = tmp_path / "sting.mp3"
        _sting(path, volume="7.5")
        measured = loudness.measure_loudness(path)
        assert measured is not None
        # Bring it to target loudness while leaving the peak above the ceiling.
        assert measured[1] > loudness.PEAK_CEILING_DBFS

        loudness.normalize_loudness(path, channels=2)

        after = loudness.measure_loudness(path)
        assert after is not None
        assert after[1] <= loudness.PEAK_CEILING_DBFS + 0.5
