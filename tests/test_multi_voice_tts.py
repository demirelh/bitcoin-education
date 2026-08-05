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
