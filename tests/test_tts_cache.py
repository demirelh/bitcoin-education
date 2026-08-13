"""Reusing takes the engine has already spoken.

The greeting and the sign-off are word for word the same every evening. What
these tests pin down is that the second evening is free, that nothing else
quietly becomes free with it, and that a bad take never gets frozen in.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

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
    @staticmethod
    def _meta() -> str:
        """Metadata as the cache actually writes it — a sidecar without the
        current version is treated as stale and dropped regardless of size."""
        return json.dumps({"version": tts_cache._CACHE_VERSION})

    def test_nothing_is_dropped_while_it_fits(self, cache_dir):
        cache_dir.mkdir(parents=True)
        (cache_dir / "a.mp3").write_bytes(b"x" * 100)
        (cache_dir / "a.json").write_text(self._meta(), encoding="utf-8")

        assert tts_cache.prune(cache_dir, 10_000) == 0
        assert (cache_dir / "a.mp3").exists()

    def test_the_idle_entries_go_first(self, cache_dir):
        import os
        import time

        cache_dir.mkdir(parents=True)
        for name in ("old", "new"):
            (cache_dir / f"{name}.mp3").write_bytes(b"x" * 600)
            (cache_dir / f"{name}.json").write_text(self._meta(), encoding="utf-8")
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


class TestASupersededCacheVersion:
    """Bumping the version puts old entries out of reach of any lookup, but on
    a cache that never fills they would sit on disk for good. That matters
    here: every entry written before levelling holds a take at the wrong
    level, which is exactly what had to be thrown away."""

    def _entry(self, cache_dir: Path, name: str, version: str | None) -> None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"{name}.mp3").write_bytes(b"x" * 100)
        payload = {} if version is None else {"version": version}
        (cache_dir / f"{name}.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_entries_from_an_older_version_are_dropped(self, cache_dir):
        self._entry(cache_dir, "old", "1")

        assert tts_cache.prune(cache_dir, 10_000) == 1
        assert not (cache_dir / "old.mp3").exists()
        assert not (cache_dir / "old.json").exists()

    def test_entries_from_the_current_version_are_kept(self, cache_dir):
        self._entry(cache_dir, "current", tts_cache._CACHE_VERSION)

        assert tts_cache.prune(cache_dir, 10_000) == 0
        assert (cache_dir / "current.mp3").exists()

    def test_an_unversioned_entry_is_dropped(self, cache_dir):
        self._entry(cache_dir, "nameless", None)

        assert tts_cache.prune(cache_dir, 10_000) == 1
        assert not (cache_dir / "nameless.mp3").exists()

    def test_they_go_even_when_the_cache_has_no_size_limit(self, cache_dir):
        """Without a cap there is no eviction to piggyback on, so this is the
        only thing that ever removes them."""
        self._entry(cache_dir, "old", "1")

        assert tts_cache.prune(cache_dir, 0) == 1
        assert not (cache_dir / "old.mp3").exists()

    def test_a_freshly_written_entry_survives_pruning(self, cache_dir, tmp_path):
        """Guards against the version in ``store`` and the one ``prune`` checks
        drifting apart — that would silently empty the cache after every run."""
        _take(_CountingService(), tmp_path / "a.mp3", cache_dir)

        assert tts_cache.prune(cache_dir, 10_000) == 0
        assert list(cache_dir.glob("*.mp3"))


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


# ---------------------------------------------------------------------------
# A stumble must never be stored
# ---------------------------------------------------------------------------
class StutterModel:
    """Stands in for the recogniser, answering one transcript per take."""

    def __init__(self, *transcripts: str) -> None:
        self.transcripts = list(transcripts)
        self.calls = 0

    def transcribe(self, path, language="tr"):
        text = self.transcripts[min(self.calls, len(self.transcripts) - 1)]
        self.calls += 1
        return [type("Seg", (), {"text": text})()], object()


GREETING = "İyi akşamlar, Almanya Yirmi Dört'e hoş geldiniz."
STUMBLE = "İyi Dağ'ım, İyi Akşamlar, Almanya 24'e hoş geldiniz."
CLEAN = "İyi akşamlar, Almanya 24'e hoş geldiniz."


def _take_with_model(service, target, cache_dir, model, floor_db=-70.0):
    import btcedu.core.tts as tts_module

    original = tts_module._noise_floor_db
    tts_module._noise_floor_db = lambda _path: floor_db
    try:
        with patch(
            "btcedu.core.tts_stutter._transcribe_opening",
            side_effect=lambda audio, model, language: model.transcribe(audio)[0][0].text,
        ):
            return _synthesize_clean_take(
                service,
                _request(text=GREETING),
                target,
                max_attempts=3,
                noise_floor_max_db=-60.0,
                label="test",
                cache_dir=cache_dir,
                stutter_model=model,
            )
    finally:
        tts_module._noise_floor_db = original


class TestAStumbleIsNeverStored:
    """The greeting is identical every evening, so a bad take would be forever.

    This is the same trap the loudness bug fell into: a recurring line, a check
    that could not see the defect, and a cache that froze the result into every
    future episode.
    """

    def test_a_stuttered_take_is_synthesized_again(self, tmp_path, cache_dir):
        service = _CountingService()
        model = StutterModel(STUMBLE, CLEAN)

        _response_out, _floor, takes = _take_with_model(
            service, tmp_path / "part.mp3", cache_dir, model
        )

        assert takes == 2, "the first take stumbled and had to be replaced"
        assert service.calls == 2

    def test_the_stored_take_is_the_clean_one(self, tmp_path, cache_dir):
        """A second episode must not inherit the stumble."""
        service = _CountingService()
        _take_with_model(service, tmp_path / "part.mp3", cache_dir, StutterModel(STUMBLE, CLEAN))

        again = _CountingService()
        _take_with_model(again, tmp_path / "second.mp3", cache_dir, StutterModel(CLEAN))

        assert again.calls == 0, "the clean take should have come from the cache"

    def test_nothing_is_stored_when_every_take_stumbles(self, tmp_path, cache_dir):
        """Three bad takes must not leave a bad take behind for tomorrow."""
        service = _CountingService()

        response, _floor, takes = _take_with_model(
            service, tmp_path / "part.mp3", cache_dir, StutterModel(STUMBLE)
        )

        assert takes == 3
        assert response is not None, "publishing silence is worse than publishing a stumble"
        assert not list(cache_dir.glob("*.mp3")) if cache_dir.exists() else True

    def test_a_clean_take_is_not_checked_twice(self, tmp_path, cache_dir):
        """A cache hit was checked when it was made; checking again costs CPU."""
        service = _CountingService()
        _take_with_model(service, tmp_path / "part.mp3", cache_dir, StutterModel(CLEAN))

        model = StutterModel(CLEAN)
        _take_with_model(_CountingService(), tmp_path / "second.mp3", cache_dir, model)

        assert model.calls == 0

    def test_without_a_model_takes_are_accepted_as_before(self, tmp_path, cache_dir):
        """A missing recogniser must not reject everything and buy three takes."""
        service = _CountingService()

        _r, _f, takes = _take(service, tmp_path / "part.mp3", cache_dir)

        assert takes == 1
        assert service.calls == 1
