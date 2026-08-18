"""Tests for per-speaker-role TTS synthesis.

The TTS provider is fully mocked — no ElevenLabs call is made. Audio joining
uses the real ffmpeg path, which is what production uses.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from btcedu.config import Settings
from btcedu.core.tts import (
    _concat_mp3,
    _generate_multi_voice_audio,
    _resolve_role_voices,
    _speaker_segments,
)
from btcedu.models.chapter_schema import Chapter, Narration, Transitions, Visual
from btcedu.services.elevenlabs_service import TTSResponse

ANCHOR_VOICE = "voice-anchor-female"
REPORTER_VOICE = "voice-reporter-male"

SEGMENTS = [
    {"role": "anchor_female", "purpose": "introduction", "text": "İyi akşamlar, gündemi açıyoruz."},
    {"role": "reporter_male", "purpose": "report", "text": "Ayrıntılar şöyle gelişti."},
]


class FakeTTSService:
    """Records every synthesis request and returns a real, tiny MP3."""

    def __init__(self, tmp_path: Path):
        self.requests = []
        self._audio = _tone(tmp_path / "tone.mp3", 440)

    def synthesize(self, request):
        self.requests.append(request)
        return TTSResponse(
            audio_bytes=self._audio,
            duration_seconds=1.5,
            sample_rate=44100,
            model=request.model,
            voice_id=request.voice_id,
            character_count=len(request.text),
            cost_usd=0.001 * len(request.text),
        )


def _tone(path: Path, frequency: int) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency={frequency}:duration=1.5",
            "-ac",
            "1",
            "-codec:a",
            "libmp3lame",
            str(path),
        ],
        check=True,
    )
    return path.read_bytes()


def _chapter(segments=None, narration=None):
    text = narration or " ".join(s["text"] for s in (segments or SEGMENTS))
    return Chapter(
        chapter_id="ch01",
        title="Test",
        order=1,
        narration=Narration(
            text=text,
            word_count=max(1, len(text.split())),
            estimated_duration_seconds=max(1, len(text.split()) * 60 // 150),
        ),
        visual=Visual(type="b_roll", description="test", image_prompt="test"),
        transitions=Transitions(**{"in": "fade", "out": "cut"}),
        metadata={"speaker_segments": segments} if segments is not None else {},
    )


class TestSpeakerSegmentDetection:
    def test_valid_segments_are_returned(self):
        assert len(_speaker_segments(_chapter(SEGMENTS))) == 2

    def test_chapter_without_metadata_is_single_voice(self):
        assert _speaker_segments(_chapter()) == []

    def test_one_presenter_still_decides_the_voice(self):
        """The opening, the closing and the weather have a single speaker — the
        anchor — and must be spoken in her voice, not the profile default."""
        same_role = [
            {"role": "anchor_female", "purpose": "opening", "text": "Bir."},
            {"role": "anchor_female", "purpose": "report", "text": "İki."},
        ]
        segments = _speaker_segments(_chapter(same_role))
        assert [s["role"] for s in segments] == ["anchor_female", "anchor_female"]

    def test_a_single_segment_chapter_keeps_its_role(self):
        one = [{"role": "anchor_female", "purpose": "closing", "text": "Bir. İki."}]
        segments = _speaker_segments(_chapter(one, narration="Bir. İki."))
        assert [s["role"] for s in segments] == ["anchor_female"]

    def test_segments_that_do_not_match_narration_are_rejected(self):
        """A stale metadata block must never change what is spoken."""
        chapter = _chapter(SEGMENTS, narration="Tamamen farklı bir metin burada duruyor.")
        assert _speaker_segments(chapter) == []

    def test_empty_segment_text_is_rejected(self):
        broken = [
            {"role": "anchor_female", "purpose": "opening", "text": ""},
            {"role": "reporter_male", "purpose": "report", "text": "Bir şey."},
        ]
        assert _speaker_segments(_chapter(broken)) == []


class TestRoleVoiceResolution:
    def test_profile_without_voices_block_stays_single_voice(self):
        assert _resolve_role_voices({"voice_id": "v1"}, Settings()) == {}

    def test_missing_role_voice_falls_back_to_the_main_voice(self):
        resolved = _resolve_role_voices(
            {"voice_id": "main", "voices": {"anchor_female": {"voice_id": ""}}},
            Settings(),
        )
        assert resolved["anchor_female"]["voice_id"] == "main"
        assert resolved["anchor_female"]["voice_id_configured"] is False

    def test_role_overrides_win_over_the_shared_config(self):
        resolved = _resolve_role_voices(
            {
                "voice_id": "main",
                "speed": 1.0,
                "voices": {"reporter_male": {"voice_id": REPORTER_VOICE, "speed": 1.2}},
            },
            Settings(),
        )
        assert resolved["reporter_male"]["voice_id"] == REPORTER_VOICE
        assert resolved["reporter_male"]["speed"] == 1.2


@pytest.fixture
def role_voices():
    return {
        "anchor_female": {"voice_id": ANCHOR_VOICE, "model": "m", "voice_id_configured": True},
        "reporter_male": {"voice_id": REPORTER_VOICE, "model": "m", "voice_id_configured": True},
    }


@pytest.fixture
def fallback():
    return {"voice_id": "fallback-voice", "model": "m", "voice_id_configured": True}


class TestMultiVoiceSynthesis:
    def test_each_role_is_synthesized_with_its_own_voice(self, tmp_path, role_voices, fallback):
        service = FakeTTSService(tmp_path)
        entry = _generate_multi_voice_audio(
            _chapter(SEGMENTS),
            SEGMENTS,
            service,
            tmp_path,
            Settings(dry_run=False),
            role_voices=role_voices,
            fallback=fallback,
            lexicon={},
            text_hash="sha256:x",
        )
        assert [r.voice_id for r in service.requests] == [ANCHOR_VOICE, REPORTER_VOICE]
        assert entry.metadata["multi_voice"] is True
        assert [p["role"] for p in entry.metadata["speaker_parts"]] == [
            "anchor_female",
            "reporter_male",
        ]

    def test_previous_parts_of_the_chapter_are_removed(self, tmp_path, role_voices, fallback):
        """A re-cast chapter must not leave its old speaker parts on disk."""
        parts_dir = tmp_path / "parts"
        parts_dir.mkdir()
        stale = parts_dir / "ch01_02_reporter_male.mp3"
        stale.write_bytes(b"old")
        other_chapter = parts_dir / "ch02_00_anchor_female.mp3"
        other_chapter.write_bytes(b"keep")

        service = FakeTTSService(tmp_path)
        _generate_multi_voice_audio(
            _chapter(SEGMENTS),
            SEGMENTS,
            service,
            tmp_path,
            Settings(dry_run=False),
            role_voices=role_voices,
            fallback=fallback,
            lexicon={},
            text_hash="sha256:x",
        )

        assert not stale.exists()
        assert other_chapter.exists()

    def test_parts_are_joined_into_one_chapter_file(self, tmp_path, role_voices, fallback):
        service = FakeTTSService(tmp_path)
        entry = _generate_multi_voice_audio(
            _chapter(SEGMENTS),
            SEGMENTS,
            service,
            tmp_path,
            Settings(dry_run=False),
            role_voices=role_voices,
            fallback=fallback,
            lexicon={},
            text_hash="sha256:x",
        )
        joined = tmp_path / "ch01.mp3"
        assert entry.file_path == "tts/ch01.mp3"
        assert joined.exists() and joined.stat().st_size > 0
        # Two 1.5s parts plus one pause.
        assert entry.duration_seconds == pytest.approx(3.35, abs=0.01)

    def test_cost_is_the_sum_of_all_segments(self, tmp_path, role_voices, fallback):
        service = FakeTTSService(tmp_path)
        entry = _generate_multi_voice_audio(
            _chapter(SEGMENTS),
            SEGMENTS,
            service,
            tmp_path,
            Settings(dry_run=False),
            role_voices=role_voices,
            fallback=fallback,
            lexicon={},
            text_hash="sha256:x",
        )
        expected = sum(0.001 * len(s["text"]) for s in SEGMENTS)
        assert entry.cost_usd == pytest.approx(expected)

    def test_unconfigured_role_uses_the_fallback_voice(self, tmp_path, fallback):
        service = FakeTTSService(tmp_path)
        voices = {
            "anchor_female": {"voice_id": "fallback-voice", "voice_id_configured": False},
            "reporter_male": {"voice_id": REPORTER_VOICE, "voice_id_configured": True},
        }
        _generate_multi_voice_audio(
            _chapter(SEGMENTS),
            SEGMENTS,
            service,
            tmp_path,
            Settings(dry_run=False),
            role_voices=voices,
            fallback=fallback,
            lexicon={},
            text_hash="sha256:x",
        )
        assert service.requests[0].voice_id == "fallback-voice"

    def test_pronunciation_lexicon_applies_per_segment(self, tmp_path, role_voices, fallback):
        service = FakeTTSService(tmp_path)
        _generate_multi_voice_audio(
            _chapter(SEGMENTS),
            SEGMENTS,
            service,
            tmp_path,
            Settings(dry_run=False),
            role_voices=role_voices,
            fallback=fallback,
            lexicon={"gündemi": "gyundemi"},
            text_hash="sha256:x",
        )
        assert "gyundemi" in service.requests[0].text

    def test_dry_run_makes_no_provider_call(self, tmp_path, role_voices, fallback):
        service = FakeTTSService(tmp_path)
        entry = _generate_multi_voice_audio(
            _chapter(SEGMENTS),
            SEGMENTS,
            service,
            tmp_path,
            Settings(dry_run=True),
            role_voices=role_voices,
            fallback=fallback,
            lexicon={},
            text_hash="sha256:x",
        )
        assert service.requests == []
        assert entry.cost_usd == 0.0
        assert (tmp_path / "ch01.mp3").exists()


class TestConcat:
    def test_pause_is_inserted_between_parts(self, tmp_path):
        parts = [_write_tone(tmp_path / f"p{i}.mp3", 440 + i * 220) for i in range(3)]
        target = tmp_path / "joined.mp3"
        _concat_mp3(parts, target, 0.35)
        assert _duration(target) == pytest.approx(3 * 1.5 + 2 * 0.35, abs=0.15)

    def test_single_part_is_copied_verbatim(self, tmp_path):
        part = _write_tone(tmp_path / "only.mp3", 440)
        target = tmp_path / "joined.mp3"
        _concat_mp3([part], target, 0.35)
        assert target.read_bytes() == part.read_bytes()


def _write_tone(path: Path, frequency: int) -> Path:
    _tone(path, frequency)
    return path


def _duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
    )
    return float(result.stdout.strip())


def test_the_hash_changes_when_a_chapter_changes_speaker():
    """A chapter that moves from one presenter to another must be re-synthesized."""
    from btcedu.core.tts import _chapter_tts_hash

    voice_sig = {"voice_id": "v1", "model": "m"}
    reporter = _chapter_tts_hash("ch01", "Bir.", "Bir.", voice_sig, ["reporter_male"])
    anchor = _chapter_tts_hash("ch01", "Bir.", "Bir.", voice_sig, ["anchor_female"])
    assert reporter != anchor


def test_the_noise_floor_is_the_first_percentile_of_the_window_levels():
    """A raised background hiss must be measurable, not only audible."""
    from unittest.mock import patch

    from btcedu.core.tts import _noise_floor_db

    levels = [-90.0] * 2 + [-60.0] * 18 + [-20.0] * 80
    stdout = "\n".join(f"lavfi.astats.Overall.RMS_level={v}" for v in levels)
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    with patch("subprocess.run", return_value=completed):
        assert _noise_floor_db(Path("any.mp3")) == -90.0


def test_the_noise_floor_ignores_digitally_silent_frames():
    """-inf frames are true silence, not the hiss the check is looking for."""
    from unittest.mock import patch

    from btcedu.core.tts import _noise_floor_db

    levels = ["-inf"] * 50 + ["-70.0"] * 30 + ["-15.0"] * 70
    stdout = "\n".join(f"lavfi.astats.Overall.RMS_level={v}" for v in levels)
    completed = subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")

    with patch("subprocess.run", return_value=completed):
        assert _noise_floor_db(Path("any.mp3")) == -70.0


def test_the_noise_floor_is_none_when_the_file_cannot_be_analysed():
    """The measurement is diagnostic and must never break a run."""
    from unittest.mock import patch

    from btcedu.core.tts import _noise_floor_db

    completed = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
    with patch("subprocess.run", return_value=completed):
        assert _noise_floor_db(Path("any.mp3")) is None

    with patch("subprocess.run", side_effect=OSError("no ffmpeg")):
        assert _noise_floor_db(Path("any.mp3")) is None


def _take(noise_bytes: bytes) -> TTSResponse:
    return TTSResponse(
        audio_bytes=noise_bytes,
        duration_seconds=10.0,
        sample_rate=44100,
        model="eleven_turbo_v2_5",
        voice_id=ANCHOR_VOICE,
        character_count=40,
        cost_usd=0.05,
    )


def test_a_noisy_take_is_synthesized_again(tmp_path):
    """Generation is stochastic: about one take in four comes back with a hiss."""
    from unittest.mock import patch

    from btcedu.core.tts import _synthesize_clean_take

    service = type("S", (), {})()
    service.synthesize = lambda request: _take(b"take")
    floors = iter([-38.0, -41.0, -80.0])
    target = tmp_path / "part.mp3"

    with patch("btcedu.core.tts._noise_floor_db", side_effect=lambda _p: next(floors)):
        response, floor, takes = _synthesize_clean_take(
            service, object(), target, max_attempts=3, noise_floor_max_db=-55.0, label="ch01"
        )

    assert takes == 3
    assert floor == -80.0
    assert response is not None


def test_a_clean_take_is_not_paid_for_twice(tmp_path):
    """Every retry costs the same as the original, so a good take must stop it."""
    from unittest.mock import patch

    from btcedu.core.tts import _synthesize_clean_take

    calls = []
    service = type("S", (), {})()
    service.synthesize = lambda request: (calls.append(1), _take(b"take"))[1]
    target = tmp_path / "part.mp3"

    with patch("btcedu.core.tts._noise_floor_db", return_value=-83.0):
        _, floor, takes = _synthesize_clean_take(
            service, object(), target, max_attempts=3, noise_floor_max_db=-55.0, label="ch01"
        )

    assert takes == 1
    assert len(calls) == 1
    assert floor == -83.0


def test_the_quietest_take_is_kept_when_none_is_clean(tmp_path):
    """A noisy programme is still better than an even noisier one."""
    from unittest.mock import patch

    from btcedu.core.tts import _synthesize_clean_take

    audio = iter([b"loud-hiss", b"quietest", b"medium"])
    service = type("S", (), {})()
    service.synthesize = lambda request: _take(next(audio))
    floors = iter([-38.0, -49.0, -44.0])
    target = tmp_path / "part.mp3"

    with patch("btcedu.core.tts._noise_floor_db", side_effect=lambda _p: next(floors)):
        _, floor, takes = _synthesize_clean_take(
            service, object(), target, max_attempts=3, noise_floor_max_db=-55.0, label="ch01"
        )

    assert takes == 3
    assert floor == -49.0
    assert target.read_bytes() == b"quietest"


def test_an_unmeasurable_take_is_accepted(tmp_path):
    """Without ffmpeg the check must not spend money on endless retries."""
    from unittest.mock import patch

    from btcedu.core.tts import _synthesize_clean_take

    calls = []
    service = type("S", (), {})()
    service.synthesize = lambda request: (calls.append(1), _take(b"take"))[1]
    target = tmp_path / "part.mp3"

    with patch("btcedu.core.tts._noise_floor_db", return_value=None):
        _, floor, takes = _synthesize_clean_take(
            service, object(), target, max_attempts=3, noise_floor_max_db=-55.0, label="ch01"
        )

    assert takes == 1
    assert len(calls) == 1
    assert floor is None


class _Line:
    """A request stand-in that carries the text the voice was given."""

    def __init__(self, text: str):
        self.text = text


GREETING = (
    "İyi akşamlar, ALMANYA24'e hoş geldiniz. "
    "Almanya'nın ve dünyanın gündemindeki gelişmelerle karşınızdayız."
)


class TestExpectedDuration:
    def test_a_short_line_is_not_judged(self):
        """Two words carry too little text for the estimate to mean anything,
        and a wrong verdict costs a paid retry."""
        from btcedu.core.tts import _expected_duration_seconds

        assert _expected_duration_seconds("Hava durumu.") is None
        assert _expected_duration_seconds("") is None
        assert _expected_duration_seconds(None) is None

    def test_a_full_sentence_is_estimated_from_its_length(self):
        from btcedu.core.tts import _expected_duration_seconds

        estimate = _expected_duration_seconds(GREETING)
        assert estimate is not None
        # The real greeting takes about 8.4 seconds to say. The estimate only
        # has to land in the same neighbourhood — the bounds applied to it are
        # a factor wide either way.
        assert 5.0 < estimate < 11.0


class TestATakeThatRunsOff:
    """ElevenLabs returned 77 seconds of unbroken tone for a 9-second greeting,
    with no recognisable speech in it. The noise floor caught that one only
    because the noise happened to be loud, and the stutter check reads the
    opening alone, so anything derailing later is invisible to it."""

    def test_a_take_nine_times_too_long_is_rejected(self, tmp_path):
        from unittest.mock import patch

        from btcedu.core.tts import _take_runs_off

        target = tmp_path / "part.mp3"
        with patch("btcedu.core.tts._media_duration_seconds", return_value=77.0):
            assert _take_runs_off(target, _Line(GREETING), "ch01", 1, 3) is True

    def test_a_take_that_stops_almost_immediately_is_rejected(self, tmp_path):
        from unittest.mock import patch

        from btcedu.core.tts import _take_runs_off

        target = tmp_path / "part.mp3"
        with patch("btcedu.core.tts._media_duration_seconds", return_value=1.2):
            assert _take_runs_off(target, _Line(GREETING), "ch01", 1, 3) is True

    def test_a_take_of_the_right_length_is_kept(self, tmp_path):
        from unittest.mock import patch

        from btcedu.core.tts import _take_runs_off

        target = tmp_path / "part.mp3"
        with patch("btcedu.core.tts._media_duration_seconds", return_value=8.4):
            assert _take_runs_off(target, _Line(GREETING), "ch01", 1, 3) is False

    def test_a_slow_reading_is_still_acceptable(self, tmp_path):
        """The bounds are wide on purpose: this catches a take that ran away,
        it does not police delivery. Eleven seconds for a line that normally
        takes seven is a laboured reading, not a broken one."""
        from unittest.mock import patch

        from btcedu.core.tts import _take_runs_off

        target = tmp_path / "part.mp3"
        with patch("btcedu.core.tts._media_duration_seconds", return_value=11.0):
            assert _take_runs_off(target, _Line(GREETING), "ch01", 1, 3) is False

    def test_an_unreadable_file_is_not_a_rejection(self, tmp_path):
        """A measurement that cannot be taken stays the noise check's problem;
        it must never become a rejection on its own."""
        from unittest.mock import patch

        from btcedu.core.tts import _take_runs_off

        target = tmp_path / "part.mp3"
        with patch("btcedu.core.tts._media_duration_seconds", return_value=None):
            assert _take_runs_off(target, _Line(GREETING), "ch01", 1, 3) is False
        with patch("btcedu.core.tts._media_duration_seconds", return_value=0.0):
            assert _take_runs_off(target, _Line(GREETING), "ch01", 1, 3) is False


class TestRunawayTakesInTheRetryLoop:
    @staticmethod
    def _service():
        service = type("S", (), {})()
        service.synthesize = lambda request: _take(b"take")
        return service

    def test_a_runaway_take_is_synthesized_again(self, tmp_path):
        from unittest.mock import patch

        from btcedu.core.tts import _synthesize_clean_take

        durations = iter([77.0, 8.4])
        target = tmp_path / "part.mp3"

        with (
            patch(
                "btcedu.core.tts._media_duration_seconds",
                side_effect=lambda _p: next(durations),
            ),
            patch("btcedu.core.tts._noise_floor_db", return_value=-80.0),
        ):
            response, floor, takes = _synthesize_clean_take(
                self._service(),
                _Line(GREETING),
                target,
                max_attempts=3,
                noise_floor_max_db=-55.0,
                label="ch01",
            )

        assert takes == 2
        assert floor == -80.0
        assert response is not None

    def test_a_runaway_take_is_never_the_one_kept(self, tmp_path):
        """A quiet 77-second tone would otherwise win the noise comparison
        outright and be chosen as the cleanest of the three."""
        from unittest.mock import patch

        from btcedu.core.tts import _synthesize_clean_take

        # The runaway take is by far the quietest, so on noise alone it wins.
        durations = iter([77.0, 8.4, 8.4])
        floors = iter([-90.0, -40.0, -38.0])
        target = tmp_path / "part.mp3"

        with (
            patch(
                "btcedu.core.tts._media_duration_seconds",
                side_effect=lambda _p: next(durations),
            ),
            patch("btcedu.core.tts._noise_floor_db", side_effect=lambda _p: next(floors)),
        ):
            _, floor, takes = _synthesize_clean_take(
                self._service(),
                _Line(GREETING),
                target,
                max_attempts=3,
                noise_floor_max_db=-55.0,
                label="ch01",
            )

        assert takes == 3
        assert floor == -40.0  # the cleanest of the takes that read the line

    def test_audio_still_comes_back_when_every_take_runs_off(self, tmp_path):
        """Publishing a bad take is bad; handing the caller nothing at all is
        worse, because there is then no audio for the chapter."""
        from unittest.mock import patch

        from btcedu.core.tts import _synthesize_clean_take

        target = tmp_path / "part.mp3"

        with (
            patch("btcedu.core.tts._media_duration_seconds", return_value=77.0),
            patch("btcedu.core.tts._noise_floor_db", return_value=-80.0),
        ):
            response, _floor, takes = _synthesize_clean_take(
                self._service(),
                _Line(GREETING),
                target,
                max_attempts=3,
                noise_floor_max_db=-55.0,
                label="ch01",
            )

        assert takes == 3
        assert response is not None

    def test_a_runaway_take_is_never_stored_for_reuse(self, tmp_path):
        """Storing one would hand the same broken recording to every later
        episode that says the same line."""
        from unittest.mock import patch

        from btcedu.core.tts import _synthesize_clean_take
        from btcedu.services.elevenlabs_service import TTSRequest

        cache_dir = tmp_path / "cache"
        target = tmp_path / "part.mp3"

        with (
            patch("btcedu.core.tts._media_duration_seconds", return_value=77.0),
            patch("btcedu.core.tts._noise_floor_db", return_value=-80.0),
        ):
            _synthesize_clean_take(
                self._service(),
                TTSRequest(text=GREETING, voice_id=ANCHOR_VOICE),
                target,
                max_attempts=2,
                noise_floor_max_db=-55.0,
                label="ch01",
                cache_dir=cache_dir,
            )

        stored = list(cache_dir.glob("*.mp3")) if cache_dir.exists() else []
        assert stored == []
