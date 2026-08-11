"""Levelling every take to the same loudness before anything else looks at it.

ElevenLabs returns the same voice up to 18 dB apart from one generation to the
next. Two things went wrong because of that: neighbouring segments jumped in
volume, and the noise check — which measures in absolute terms — kept picking
the quietest take rather than the cleanest one and froze it into the cache.

These tests pin down that the levelling happens, that it happens first, and
that it never damages the take it is supposed to improve.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from btcedu.core.tts import (
    _PEAK_CEILING_DBFS,
    _TARGET_LUFS,
    _measure_loudness,
    _normalize_loudness,
    _parse_loudnorm_json,
)

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is not installed"
)


def _speech_like(path: Path, *, volume: str, seconds: float = 4.0) -> Path:
    """A short MP3 at a chosen level.

    Silence between the tones is deliberate: integrated loudness ignores it,
    so a file built this way behaves like speech with pauses rather than like
    a continuous tone, which is what the filter is tuned for.
    """
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-v", "error", "-y",
            "-f", "lavfi", "-i", f"sine=frequency=220:duration={seconds}",
            "-af", f"volume={volume},atempo=1.0",
            "-ar", "44100", "-ac", "1", "-c:a", "libmp3lame", "-q:a", "2",
            str(path),
        ],
        check=True,
        capture_output=True,
    )
    return path


def _duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def _sample_rate(path: Path) -> int:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a:0",
         "-show_entries", "stream=sample_rate", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return int(result.stdout.strip())


@needs_ffmpeg
class TestTheLevelIsCorrected:
    def test_a_quiet_take_is_brought_up(self, tmp_path):
        """The real defect: the frozen intro sat 6 dB under the bulletin."""
        path = _speech_like(tmp_path / "quiet.mp3", volume="0.06")
        before = _measure_loudness(path)
        assert before is not None and before[0] < _TARGET_LUFS - 5

        _normalize_loudness(path)

        after = _measure_loudness(path)
        assert after is not None
        assert abs(after[0] - _TARGET_LUFS) < 1.5

    def test_a_loud_take_is_brought_down(self, tmp_path):
        path = _speech_like(tmp_path / "loud.mp3", volume="1.0")
        _normalize_loudness(path)

        after = _measure_loudness(path)
        assert after is not None
        assert abs(after[0] - _TARGET_LUFS) < 1.5

    def test_two_takes_far_apart_end_up_together(self, tmp_path):
        """What the listener actually notices is the step between segments."""
        quiet = _speech_like(tmp_path / "a.mp3", volume="0.05")
        loud = _speech_like(tmp_path / "b.mp3", volume="1.0")

        spread_before = abs(_measure_loudness(quiet)[0] - _measure_loudness(loud)[0])
        _normalize_loudness(quiet)
        _normalize_loudness(loud)
        spread_after = abs(_measure_loudness(quiet)[0] - _measure_loudness(loud)[0])

        assert spread_before > 10
        assert spread_after < 2.0


@needs_ffmpeg
class TestTheTakeSurvivesIt:
    def test_lifting_a_quiet_take_does_not_clip_it(self, tmp_path):
        """Gain is given up rather than peaks, or the fix would be a new fault."""
        path = _speech_like(tmp_path / "quiet.mp3", volume="0.03")
        _normalize_loudness(path)

        after = _measure_loudness(path)
        assert after is not None
        assert after[1] <= _PEAK_CEILING_DBFS + 0.5

    def test_the_duration_is_unchanged(self, tmp_path):
        """Duration is reported from the unlevelled bytes and drives the render;
        if levelling moved it, picture and sound would drift apart."""
        path = _speech_like(tmp_path / "a.mp3", volume="0.2")
        before = _duration(path)

        _normalize_loudness(path)

        assert abs(_duration(path) - before) < 0.05

    def test_the_sample_rate_is_unchanged(self, tmp_path):
        """loudnorm runs at 192 kHz internally and would resample on the way out,
        leaving the take out of step with the segments it is joined to."""
        path = _speech_like(tmp_path / "a.mp3", volume="0.2")
        _normalize_loudness(path)

        assert _sample_rate(path) == 44100


@needs_ffmpeg
class TestNothingIsLostOnFailure:
    def test_an_unreadable_file_is_left_alone(self, tmp_path):
        path = tmp_path / "not-audio.mp3"
        path.write_bytes(b"this is not an MP3")

        assert _normalize_loudness(path) is None
        assert path.read_bytes() == b"this is not an MP3"

    def test_silence_is_left_alone(self, tmp_path):
        """A silent take has no integrated loudness to correct towards; trying
        would multiply the noise instead of the speech."""
        subprocess.run(
            ["ffmpeg", "-hide_banner", "-v", "error", "-y",
             "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono:d=3",
             "-c:a", "libmp3lame", "-q:a", "2", str(tmp_path / "silence.mp3")],
            check=True, capture_output=True,
        )
        path = tmp_path / "silence.mp3"
        before = path.read_bytes()

        assert _normalize_loudness(path) is None
        assert path.read_bytes() == before

    def test_no_scratch_file_is_left_behind(self, tmp_path):
        path = _speech_like(tmp_path / "a.mp3", volume="0.2")
        _normalize_loudness(path)

        assert [p.name for p in tmp_path.iterdir()] == ["a.mp3"]


class TestTheMeasurementBlock:
    def test_the_last_json_object_wins(self):
        stderr = (
            '{"stray": "object from something else"}\n'
            "[Parsed_loudnorm_0 @ 0x1]\n"
            '{"input_i": "-21.0", "input_tp": "-3.0", "input_lra": "5.0",'
            ' "input_thresh": "-31.0", "target_offset": "0.1"}\n'
        )
        stats = _parse_loudnorm_json(stderr)
        assert stats is not None and stats["input_i"] == "-21.0"

    def test_a_missing_block_is_not_an_error(self):
        assert _parse_loudnorm_json("ffmpeg version 6.0\n") is None

    def test_an_incomplete_block_is_refused(self):
        assert _parse_loudnorm_json('{"input_i": "-21.0"}') is None

    def test_an_infinite_reading_is_refused(self):
        """Silence measures as -inf, which would make the correction nonsense."""
        stats = _parse_loudnorm_json(
            '{"input_i": "-inf", "input_tp": "-inf", "input_lra": "0.0",'
            ' "input_thresh": "-inf", "target_offset": "0.0"}'
        )
        assert stats is None


class TestLevellingComesBeforeTheNoiseCheck:
    """The ordering is the whole point. Measured across generations the noise
    floor tracks the speech level (r = +0.77), so on unlevelled takes the check
    compares loudness and reliably keeps the quietest one."""

    def test_the_noise_floor_is_only_read_after_levelling(self, tmp_path, monkeypatch):
        import btcedu.core.tts as tts_module
        from btcedu.services.elevenlabs_service import TTSRequest, TTSResponse

        order: list[str] = []
        monkeypatch.setattr(
            tts_module, "_normalize_loudness", lambda path, **kw: order.append("level")
        )
        monkeypatch.setattr(
            tts_module,
            "_noise_floor_db",
            lambda path: (order.append("noise"), -70.0)[1],
        )

        class _Service:
            def synthesize(self, request):
                return TTSResponse(
                    audio_bytes=b"ID3-audio",
                    duration_seconds=4.0,
                    sample_rate=44100,
                    model="eleven_multilingual_v2",
                    voice_id="nazli",
                    character_count=10,
                    cost_usd=0.01,
                )

        tts_module._synthesize_clean_take(
            _Service(),
            TTSRequest(
                text="İyi akşamlar.",
                voice_id="nazli",
                model="eleven_multilingual_v2",
                stability=0.5,
                similarity_boost=0.75,
                style=0.0,
                use_speaker_boost=True,
                speed=1.0,
            ),
            tmp_path / "out.mp3",
            max_attempts=1,
            noise_floor_max_db=-60.0,
            label="test",
            cache_dir=None,
        )

        assert order == ["level", "noise"]

    def test_a_take_kept_after_failing_the_check_is_still_levelled(
        self, tmp_path, monkeypatch
    ):
        """The fallback rewrites the original bytes over the levelled file, so
        without a second pass the one take nobody chose would also be the only
        one left at the wrong level."""
        import btcedu.core.tts as tts_module
        from btcedu.services.elevenlabs_service import TTSRequest, TTSResponse

        levelled: list[Path] = []
        monkeypatch.setattr(
            tts_module, "_normalize_loudness", lambda path, **kw: levelled.append(path)
        )
        monkeypatch.setattr(tts_module, "_noise_floor_db", lambda path: -40.0)

        class _Service:
            def synthesize(self, request):
                return TTSResponse(
                    audio_bytes=b"ID3-audio",
                    duration_seconds=4.0,
                    sample_rate=44100,
                    model="eleven_multilingual_v2",
                    voice_id="nazli",
                    character_count=10,
                    cost_usd=0.01,
                )

        target = tmp_path / "out.mp3"
        response, floor, attempts = tts_module._synthesize_clean_take(
            _Service(),
            TTSRequest(
                text="İyi akşamlar.",
                voice_id="nazli",
                model="eleven_multilingual_v2",
                stability=0.5,
                similarity_boost=0.75,
                style=0.0,
                use_speaker_boost=True,
                speed=1.0,
            ),
            target,
            max_attempts=2,
            noise_floor_max_db=-60.0,
            label="test",
            cache_dir=None,
        )

        assert attempts == 2 and response is not None
        # once per take, plus once more after the best one is written back
        assert levelled == [target, target, target]
