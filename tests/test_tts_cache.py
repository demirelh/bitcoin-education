"""Reusing takes the engine has already spoken.

The greeting and the sign-off are word for word the same every evening. What
these tests pin down is that the second evening is free, that nothing else
quietly becomes free with it, and that a bad take never gets frozen in.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from btcedu.config import Settings
from btcedu.core import tts_cache
from btcedu.core.tts import _cache_dir, _synthesize_clean_take
from btcedu.services.elevenlabs_service import TTSRequest, TTSResponse

OPENING = (
    "İyi akşamlar. Almanya Yirmi Dört'e hoş geldiniz. Bugünün gündemini ve "
    "sizi doğrudan ilgilendiren gelişmeleri derledik."
)
CLOSING = (
    "Bugünkü bültenimizin sonuna geldik. Bizi izlediğiniz için teşekkür ederiz. "
    "Gelişmeleri izlemeye ve sizlere aktarmaya devam edeceğiz. İyi akşamlar."
)


def _request(text: str = OPENING, **overrides) -> TTSRequest:
    fields = {
        "text": text,
        "voice_id": "nazli",
        "model": "eleven_multilingual_v2",
        "stability": 0.5,
        "similarity_boost": 0.75,
        "style": 0.0,
        "use_speaker_boost": True,
        "speed": 1.0,
    }
    fields.update(overrides)
    return TTSRequest(**fields)


def _response(audio: bytes = b"ID3-audio", cost: float = 0.056) -> TTSResponse:
    return TTSResponse(
        audio_bytes=audio,
        duration_seconds=11.5,
        sample_rate=44100,
        model="eleven_multilingual_v2",
        voice_id="nazli",
        character_count=len(OPENING),
        cost_usd=cost,
    )


class _CountingService:
    """Stands in for ElevenLabs and counts what it was asked to pay for."""

    def __init__(self, response: TTSResponse | None = None):
        self.calls = 0
        self._response = response or _response()

    def synthesize(self, request):
        self.calls += 1
        return self._response


@pytest.fixture
def cache_dir(tmp_path) -> Path:
    return tmp_path / "cache"


def _take(service, target: Path, cache_dir: Path | None, request=None, floor_db=-70.0):
    """Run one synthesis with the noise check answering *floor_db*."""
    import btcedu.core.tts as tts_module

    original = tts_module._noise_floor_db
    tts_module._noise_floor_db = lambda _path: floor_db
    try:
        return _synthesize_clean_take(
            service,
            request or _request(),
            target,
            max_attempts=3,
            noise_floor_max_db=-60.0,
            label="test",
            cache_dir=cache_dir,
        )
    finally:
        tts_module._noise_floor_db = original


# ---------------------------------------------------------------------------
# the point of the whole thing
# ---------------------------------------------------------------------------


class TestTheSecondEveningIsFree:
    def test_the_same_line_is_not_bought_twice(self, tmp_path, cache_dir):
        service = _CountingService()

        _take(service, tmp_path / "first.mp3", cache_dir)
        _take(service, tmp_path / "second.mp3", cache_dir)

        assert service.calls == 1

    def test_the_reused_take_is_the_same_audio(self, tmp_path, cache_dir):
        service = _CountingService(_response(audio=b"ID3-the-greeting"))
        first, second = tmp_path / "first.mp3", tmp_path / "second.mp3"

        _take(service, first, cache_dir)
        _take(service, second, cache_dir)

        assert second.read_bytes() == first.read_bytes() == b"ID3-the-greeting"

    def test_a_reused_take_costs_nothing(self, tmp_path, cache_dir):
        """Charging again for audio that came off the disk would misreport the
        episode price and eat into the cost guard for money nobody spent."""
        service = _CountingService()

        first, _, _ = _take(service, tmp_path / "first.mp3", cache_dir)
        second, _, takes = _take(service, tmp_path / "second.mp3", cache_dir)

        assert first.cost_usd == pytest.approx(0.056)
        assert second.cost_usd == 0.0
        assert takes == 0

    def test_duration_survives_the_round_trip(self, tmp_path, cache_dir):
        """The renderer times segments by this number; a wrong one desyncs."""
        service = _CountingService()

        _take(service, tmp_path / "first.mp3", cache_dir)
        reused, _, _ = _take(service, tmp_path / "second.mp3", cache_dir)

        assert reused.duration_seconds == pytest.approx(11.5)
        assert reused.sample_rate == 44100
        assert reused.model == "eleven_multilingual_v2"

    def test_opening_and_closing_are_separate_entries(self, tmp_path, cache_dir):
        service = _CountingService()

        _take(service, tmp_path / "a.mp3", cache_dir, _request(OPENING))
        _take(service, tmp_path / "b.mp3", cache_dir, _request(CLOSING))
        _take(service, tmp_path / "c.mp3", cache_dir, _request(OPENING))
        _take(service, tmp_path / "d.mp3", cache_dir, _request(CLOSING))

        assert service.calls == 2


# ---------------------------------------------------------------------------
# what must still be paid for
# ---------------------------------------------------------------------------


class TestWhatStillCosts:
    def test_a_changed_word_is_a_new_recording(self, tmp_path, cache_dir):
        service = _CountingService()

        _take(service, tmp_path / "a.mp3", cache_dir, _request(OPENING))
        _take(service, tmp_path / "b.mp3", cache_dir, _request(OPENING + " Bir ekleme."))

        assert service.calls == 2

    def test_a_different_voice_is_a_new_recording(self, tmp_path, cache_dir):
        service = _CountingService()

        _take(service, tmp_path / "a.mp3", cache_dir, _request(voice_id="nazli"))
        _take(service, tmp_path / "b.mp3", cache_dir, _request(voice_id="cavit"))

        assert service.calls == 2

    @pytest.mark.parametrize(
        "field,value",
        [
            ("model", "eleven_turbo_v2"),
            ("stability", 0.9),
            ("similarity_boost", 0.4),
            ("style", 0.3),
            ("use_speaker_boost", False),
            ("speed", 1.1),
        ],
    )
    def test_every_parameter_that_shapes_the_sound_splits_the_key(self, field, value):
        """A parameter left out of the key would serve the wrong audio."""
        assert tts_cache.cache_key(_request()) != tts_cache.cache_key(_request(**{field: value}))

    def test_an_identical_request_always_lands_on_the_same_key(self):
        assert tts_cache.cache_key(_request()) == tts_cache.cache_key(_request())


# ---------------------------------------------------------------------------
# quality
# ---------------------------------------------------------------------------


class TestABadTakeIsNeverFrozenIn:
    def test_a_hissy_take_is_not_stored(self, tmp_path, cache_dir):
        """Retrying exists to escape a noise bed. Caching one would make it
        permanent for every future episode."""
        service = _CountingService()

        _take(service, tmp_path / "a.mp3", cache_dir, floor_db=-30.0)

        assert list(cache_dir.glob("*.mp3")) == []

    def test_the_next_run_tries_again_after_a_hissy_take(self, tmp_path, cache_dir):
        service = _CountingService()

        _take(service, tmp_path / "a.mp3", cache_dir, floor_db=-30.0)
        calls_after_bad = service.calls
        _take(service, tmp_path / "b.mp3", cache_dir, floor_db=-70.0)

        assert service.calls > calls_after_bad
        assert list(cache_dir.glob("*.mp3"))

    def test_a_clean_take_records_its_noise_floor(self, tmp_path, cache_dir):
        service = _CountingService()

        _take(service, tmp_path / "a.mp3", cache_dir, floor_db=-72.0)
        _, floor, _ = _take(service, tmp_path / "b.mp3", cache_dir)

        assert floor == pytest.approx(-72.0)


# ---------------------------------------------------------------------------
# a cache must never be the reason something breaks
# ---------------------------------------------------------------------------


class TestDamageIsNotFatal:
    def test_a_missing_sidecar_falls_back_to_synthesis(self, tmp_path, cache_dir):
        service = _CountingService()
        _take(service, tmp_path / "a.mp3", cache_dir)
        for meta in cache_dir.glob("*.json"):
            meta.unlink()

        _take(service, tmp_path / "b.mp3", cache_dir)

        assert service.calls == 2

    def test_a_corrupt_sidecar_falls_back_to_synthesis(self, tmp_path, cache_dir):
        service = _CountingService()
        _take(service, tmp_path / "a.mp3", cache_dir)
        for meta in cache_dir.glob("*.json"):
            meta.write_text("{not json", encoding="utf-8")

        _take(service, tmp_path / "b.mp3", cache_dir)

        assert service.calls == 2

    def test_an_empty_audio_file_falls_back_to_synthesis(self, tmp_path, cache_dir):
        service = _CountingService()
        _take(service, tmp_path / "a.mp3", cache_dir)
        for audio in cache_dir.glob("*.mp3"):
            audio.write_bytes(b"")

        _take(service, tmp_path / "b.mp3", cache_dir)

        assert service.calls == 2

    def test_an_unwritable_cache_does_not_stop_the_episode(self, tmp_path):
        service = _CountingService()
        blocked = tmp_path / "blocked"
        blocked.write_text("I am a file, not a directory", encoding="utf-8")

        response, _, _ = _take(service, tmp_path / "a.mp3", blocked)

        assert response is not None
        assert service.calls == 1

    def test_a_version_bump_retires_old_entries(self, tmp_path, cache_dir, monkeypatch):
        service = _CountingService()
        _take(service, tmp_path / "a.mp3", cache_dir)
        monkeypatch.setattr(tts_cache, "_CACHE_VERSION", "99")

        _take(service, tmp_path / "b.mp3", cache_dir)

        assert service.calls == 2


# ---------------------------------------------------------------------------
# switch and housekeeping
# ---------------------------------------------------------------------------


class TestTheSwitch:
    def test_disabled_means_every_line_is_synthesized(self, tmp_path):
        service = _CountingService()

        _take(service, tmp_path / "a.mp3", None)
        _take(service, tmp_path / "b.mp3", None)

        assert service.calls == 2

    def test_the_setting_turns_it_off(self):
        assert _cache_dir(Settings(tts_cache_enabled=False)) is None

    def test_the_setting_points_at_the_configured_directory(self):
        assert _cache_dir(Settings(tts_cache_dir="/tmp/somewhere")) == Path("/tmp/somewhere")


class TestPruning:
    def test_nothing_is_dropped_while_it_fits(self, cache_dir):
        cache_dir.mkdir(parents=True)
        (cache_dir / "a.mp3").write_bytes(b"x" * 100)
        (cache_dir / "a.json").write_text("{}", encoding="utf-8")

        assert tts_cache.prune(cache_dir, 10_000) == 0
        assert (cache_dir / "a.mp3").exists()

    def test_the_idle_entries_go_first(self, cache_dir):
        import os
        import time

        cache_dir.mkdir(parents=True)
        for name in ("old", "new"):
            (cache_dir / f"{name}.mp3").write_bytes(b"x" * 600)
            (cache_dir / f"{name}.json").write_text("{}", encoding="utf-8")
        old_time = time.time() - 86400
        os.utime(cache_dir / "old.mp3", (old_time, old_time))

        tts_cache.prune(cache_dir, 900)

        assert not (cache_dir / "old.mp3").exists()
        assert (cache_dir / "new.mp3").exists()

    def test_zero_means_no_limit(self, cache_dir):
        cache_dir.mkdir(parents=True)
        (cache_dir / "a.mp3").write_bytes(b"x" * 10_000)

        assert tts_cache.prune(cache_dir, 0) == 0
        assert (cache_dir / "a.mp3").exists()

    def test_a_missing_directory_is_not_an_error(self, cache_dir):
        assert tts_cache.prune(cache_dir, 100) == 0


class TestTheStoredEntry:
    def test_it_carries_a_readable_hint_of_what_it_is(self, tmp_path, cache_dir):
        """Someone looking at the directory should be able to tell the
        greeting from the sign-off without playing every file."""
        _take(_CountingService(), tmp_path / "a.mp3", cache_dir, _request(OPENING))

        meta = json.loads(next(cache_dir.glob("*.json")).read_text(encoding="utf-8"))

        assert meta["text_preview"].startswith("İyi akşamlar")
        assert meta["text_length"] == len(OPENING)

    def test_no_half_written_files_are_left_behind(self, tmp_path, cache_dir):
        _take(_CountingService(), tmp_path / "a.mp3", cache_dir)

        assert list(cache_dir.glob("*.part")) == []


# ---------------------------------------------------------------------------
# what this actually saves on a real bulletin
# ---------------------------------------------------------------------------


class TestTheBulletinFraming:
    """The greeting and the sign-off are their own TTS segments already.

    ``_speaker_segments`` splits a chapter by ``purpose``, so the opening is
    never glued to that evening's headlines. The profile draws both from a
    short fixed list, so after a handful of bulletins every variant is on
    disk and the framing is never billed again — while still not sounding
    identical every night.
    """

    def _framing_variants(self, key: str) -> list[str]:
        from btcedu.config import Settings as _Settings
        from btcedu.profiles import get_registry

        profile = get_registry(_Settings()).get("tagesschau_tr")
        script_cfg = profile.stage_config.get("script", {}) or {}
        variants = script_cfg.get(key) or []
        assert variants, f"profile no longer defines {key}"
        return [str(v) for v in variants]

    @pytest.mark.parametrize("key", ["openings", "closings", "weather_handovers"])
    def test_the_framing_saturates_after_a_few_evenings(self, tmp_path, cache_dir, key):
        variants = self._framing_variants(key)
        service = _CountingService()

        # Three weeks of bulletins cycling through the profile's variants.
        for evening in range(21):
            text = variants[evening % len(variants)].replace("{show_name}", "Almanya24")
            _take(service, tmp_path / f"{evening}.mp3", cache_dir, _request(text))

        assert service.calls == len(variants)

    def test_the_greeting_and_the_sign_off_do_not_share_a_take(self, tmp_path, cache_dir):
        service = _CountingService()

        _take(service, tmp_path / "open.mp3", cache_dir, _request(OPENING))
        _take(service, tmp_path / "close.mp3", cache_dir, _request(CLOSING))

        assert service.calls == 2
        assert len(list(cache_dir.glob("*.mp3"))) == 2

    def test_todays_headlines_are_never_reused(self, tmp_path, cache_dir):
        """Different news every evening must never be served yesterday's audio."""
        service = _CountingService()

        _take(service, tmp_path / "a.mp3", cache_dir, _request("Bugünün ilk haberi."))
        _take(service, tmp_path / "b.mp3", cache_dir, _request("Bugünün ikinci haberi."))

        assert service.calls == 2

    def test_the_opening_segment_is_split_off_from_the_headlines(self):
        """If the two ever merged, the greeting would carry the day's news
        into its key and could never be reused again."""
        from btcedu.core.tts import _speaker_segments

        class _Narration:
            text = "İyi akşamlar. Bugünün başlıkları. Bir haber."

        class _Chapter:
            chapter_id = "ch01"
            narration = _Narration()
            metadata = {
                "speaker_segments": [
                    {"role": "anchor_female", "purpose": "opening", "text": "İyi akşamlar."},
                    {
                        "role": "anchor_female",
                        "purpose": "headlines",
                        "text": "Bugünün başlıkları. Bir haber.",
                    },
                ]
            }

        segments = _speaker_segments(_Chapter())

        assert [s["purpose"] for s in segments] == ["opening", "headlines"]
        assert segments[0]["text"] == "İyi akşamlar."


class TestASettingsObjectThatIsNotOne:
    """A stand-in settings object must switch the cache off, not build a
    directory named after itself in the working tree."""

    def test_a_placeholder_directory_is_refused(self):
        from unittest.mock import MagicMock

        assert _cache_dir(MagicMock()) is None

    def test_a_placeholder_switch_is_refused(self):
        from unittest.mock import MagicMock

        settings = MagicMock()
        settings.tts_cache_dir = "/tmp/real-path"
        assert _cache_dir(settings) is None

    def test_an_empty_directory_setting_is_refused(self):
        assert _cache_dir(Settings(tts_cache_dir="   ")) is None
