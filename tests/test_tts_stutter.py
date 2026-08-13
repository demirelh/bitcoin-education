"""Catching a take that stumbles over its opening words.

The case these tests are built from reached a finished video on 2026-08-13: the
bulletin opened "İyi Dağ'ım, İyi Akşamlar, Almanya 24'e hoş geldiniz" for a
script reading "İyi akşamlar, Almanya Yirmi Dört'e hoş geldiniz". Every check
in place at the time passed it — the level was right, the duration plausible,
the noise floor -62 dB. Only the words were wrong, and nothing read the words.

The strings below are not invented. They are what recognisers actually returned
for stored takes, including their own mistakes, because the check has to keep
working while the transcript around it is mangled.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from btcedu.core.tts_stutter import StutterVerdict, check_opening, find_repetition

GREETING = "İyi akşamlar, Almanya Yirmi Dört'e hoş geldiniz."
HEADLINES = "Önce günün öne çıkan başlıkları."


# --------------------------------------------------------------------------- #
# What counts as a stumble
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "expected,heard",
    [
        # The take that shipped, as a `small` model heard it.
        (GREETING, "İyi Dağ'ım, İyi Akşamlar, Almanya 24'e hoş geldiniz."),
        # The same take through a `tiny` model: unusable as text, unmistakable
        # as a repetition. That is the whole point of not comparing wording.
        (GREETING, "İyi dam, iyi akşamlar, Almanya 24'e hoş geldiniz."),
        # A stumble folded into neighbouring words rather than repeating one
        # outright — invisible to a word-level comparison.
        (HEADLINES, "Önce gününün akgününe çıkan başlıkları,"),
    ],
)
def test_a_repeated_opening_is_caught(expected, heard):
    assert find_repetition(expected, heard)


@pytest.mark.parametrize(
    "expected,heard",
    [
        (GREETING, "İyi akşamlar! Almanya 24'e hoş geldiniz."),
        # Every case below is a *recogniser* error on a take that was fine.
        # Rejecting any of them would buy a new take for nothing.
        (
            "Almanya gündemine Ren nehrindeki düşük su seviyesiyle başlıyoruz.",
            "Almanya gündemine Rennehrindeki düşük su seviyesiyle başlıyor.",
        ),
        (
            "Bir Suriye mahkemesi ülkenin eski lideri Esad'ı",
            "bir suriye makemesi ülkenin eskili ideri esadığı",
        ),
        (
            "Uluslararası gelişmelerden Almanya'daki çalışma hayatına",
            "uluslar arası gelişmelerden almanyadaki çalışma hayatına geçiyoruz",
        ),
        (
            "Bültenimizi hava durumuyla tamamlıyoruz.",
            "gültenimizi havadurumuyla tamamlıyoruz",
        ),
        (
            "Kolombiya'dan Karayipler'e geçiyoruz. Küba'da Fidel",
            "kolom bir adam kareyiplere geceye geçiyoruz kubada",
        ),
        (
            "Magdeburg'dan on dokuz yaşındaki Johannes",
            "magde borktan on dokuz yaşındaki yuan nes",
        ),
        (
            "Karlsruhe yakınlarındaki Ren sulak alanlarında",
            "karsruya yakınlarındaki ren sula kalanlarında normalde suyun",
        ),
    ],
)
def test_a_misheard_take_is_not_a_stumble(expected, heard):
    """A recogniser substitutes words; it does not invent repetitions.

    Measured over 32 stored takes a `tiny` model misread a third of them. A
    word-by-word check would have rejected every one of those, and each
    rejection is a paid retry for audio that was already correct.
    """
    assert not find_repetition(expected, heard)


def test_a_repetition_the_script_asks_for_is_allowed():
    """Rejecting this would buy a new take to say the same thing."""
    assert not find_repetition("Bugün, bugün her şey değişti.", "Bugün, bugün her şey değişti.")


def test_a_repetition_beyond_the_opening_is_ignored():
    """Only the opening is examined, so a later echo must not reach the verdict.

    Bulletins repeat names constantly once a story is running. Examining the
    whole take would turn ordinary prose into evidence.
    """
    expected = "Almanya gündemine bugün Berlin'deki gelişmelerle başlıyoruz."
    heard = "Almanya gündemine bugün Berlin'deki gelişmelerle başlıyoruz Berlin Berlin"

    assert not find_repetition(expected, heard)


def test_short_function_words_do_not_convict_a_take():
    """Turkish repeats "ve", "bu" and "de" freely and they prove nothing."""
    assert not find_repetition("Bu ve bu konu önemli.", "Bu ve bu konu önemli.")


# --------------------------------------------------------------------------- #
# Running the check
# --------------------------------------------------------------------------- #
class FakeModel:
    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def transcribe(self, path, language="tr"):
        self.calls += 1
        segment = type("Seg", (), {"text": self.text})()
        return [segment], object()


def _audio(tmp_path: Path) -> Path:
    path = tmp_path / "take.mp3"
    path.write_bytes(b"\0" * 64)
    return path


def test_a_stuttered_take_is_reported_with_what_was_heard(tmp_path):
    """"This take stutters" is a claim about a transcript, so it carries it."""
    model = FakeModel("İyi Dağ'ım, İyi Akşamlar, Almanya 24'e")

    with patch("btcedu.core.tts_stutter._transcribe_opening", return_value=model.text):
        verdict = check_opening(_audio(tmp_path), GREETING, model=model)

    assert verdict.stuttered is True
    assert "iyi" in verdict.reason
    assert "Dağ'ım" in verdict.heard


def test_a_clean_take_passes(tmp_path):
    model = FakeModel("İyi akşamlar, Almanya 24'e hoş geldiniz.")

    with patch("btcedu.core.tts_stutter._transcribe_opening", return_value=model.text):
        verdict = check_opening(_audio(tmp_path), GREETING, model=model)

    assert verdict.stuttered is False


def test_no_model_means_no_verdict_rather_than_a_pass(tmp_path):
    """"Not checked" and "checked and fine" must not be the same answer."""
    verdict = check_opening(_audio(tmp_path), GREETING, model=None)

    assert verdict.stuttered is None
    assert verdict.reason == "unavailable"


def test_a_failed_transcription_yields_no_verdict(tmp_path):
    with patch("btcedu.core.tts_stutter._transcribe_opening", return_value=None):
        verdict = check_opening(_audio(tmp_path), GREETING, model=FakeModel(""))

    assert verdict.stuttered is None


def test_a_crashing_recogniser_does_not_escape(tmp_path):
    """A quality check must never be able to fail the episode."""
    with patch(
        "btcedu.core.tts_stutter._transcribe_opening",
        side_effect=RuntimeError("model file is corrupt"),
    ):
        verdict = check_opening(_audio(tmp_path), GREETING, model=FakeModel(""))

    assert verdict.stuttered is None


def test_a_missing_recogniser_library_is_not_an_error():
    from btcedu.core.tts_stutter import load_model

    with patch.dict("sys.modules", {"faster_whisper": None}):
        assert load_model("small") is None


def test_an_empty_model_name_disables_the_check():
    from btcedu.core.tts_stutter import load_model

    assert load_model("") is None


def test_the_verdict_keeps_unavailable_apart_from_clean():
    assert StutterVerdict(None).stuttered is None
    assert StutterVerdict(False).stuttered is False
