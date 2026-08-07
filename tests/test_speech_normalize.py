"""Turkish speech normalization for the TTS synthesis text.

Two things are under test here. The first is that numbers are spoken the way a
presenter says them rather than the way a spreadsheet writes them. The second,
and the one that would be expensive to get wrong, is that this never escapes
the synthesis path: the approved narration, the chapter titles, the topic cards
and the lower thirds must keep their digits.
"""

import pytest

from btcedu.core.speech_normalize import normalize_speech, number_to_turkish_words


class TestTurkishNumerals:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "sıfır"),
            (1, "bir"),
            (11, "on bir"),
            (20, "yirmi"),
            (99, "doksan dokuz"),
            (100, "yüz"),
            (101, "yüz bir"),
            (200, "iki yüz"),
            (999, "dokuz yüz doksan dokuz"),
            (1_000, "bin"),
            (1_001, "bin bir"),
            (2_026, "iki bin yirmi altı"),
            (98_000, "doksan sekiz bin"),
            (1_000_000, "bir milyon"),
            (2_750_000, "iki milyon yedi yüz elli bin"),
            (1_234_567, "bir milyon iki yüz otuz dört bin beş yüz altmış yedi"),
        ],
    )
    def test_integers_are_spelled_the_turkish_way(self, value, expected):
        assert number_to_turkish_words(value) == expected

    def test_the_silent_one_is_dropped_only_where_turkish_drops_it(self):
        # "bir yüz" and "bir bin" are not said; "bir milyon" is.
        assert number_to_turkish_words(100) == "yüz"
        assert number_to_turkish_words(1_000) == "bin"
        assert number_to_turkish_words(1_000_000) == "bir milyon"

    def test_an_identifier_sized_number_is_left_to_the_engine(self):
        # Past a quadrillion the figure is a code, not a quantity, and reading
        # it as one word would be worse than leaving the digits alone.
        assert number_to_turkish_words(10**15) == str(10**15)


class TestTheRulesTheUserAsked_For:
    @pytest.mark.parametrize(
        ("written", "spoken"),
        [
            ("8 Ağustos", "sekiz Ağustos"),
            ("13.00", "saat bir"),
            ("13.30", "saat bir buçuk"),
            ("20.00", "saat sekiz"),
            ("%70", "yüzde yetmiş"),
            ("2,75 milyon avro", "iki milyon yedi yüz elli bin avro"),
            ("98.000", "doksan sekiz bin"),
            ("2,5", "iki buçuk"),
            ("2,75", "iki virgül yetmiş beş"),
            ("2026", "iki bin yirmi altı"),
        ],
    )
    def test_each_example_reads_as_specified(self, written, spoken):
        assert normalize_speech(written) == spoken


class TestClockTimes:
    def test_the_spoken_hour_is_the_twelve_hour_one(self):
        # Turkish bulletins say "saat sekiz" for a 20:00 broadcast.
        assert normalize_speech("20:00") == "saat sekiz"
        assert normalize_speech("00:00") == "saat on iki"
        assert normalize_speech("12:00") == "saat on iki"

    def test_a_written_saat_is_absorbed_not_repeated(self):
        assert normalize_speech("Saat 13.00") == "Saat bir"
        assert normalize_speech("saat 20.00") == "saat sekiz"

    def test_minutes_off_the_half_hour_are_read_plainly(self):
        # "biri çeyrek geçe" needs the hour in an oblique case and is easy to
        # get wrong; a plain reading is always intelligible.
        assert normalize_speech("13:15") == "saat bir on beş"

    def test_a_thousands_group_is_not_mistaken_for_a_time(self):
        # "13.000" has three digits after the dot, "13.00" has two.
        assert normalize_speech("13.000 kişi") == "on üç bin kişi"

    def test_an_impossible_time_is_left_alone(self):
        assert "saat" not in normalize_speech("25:99")


class TestDecimalsAndMoney:
    def test_a_half_is_bucuk(self):
        assert normalize_speech("2,5 puan") == "iki buçuk puan"

    def test_a_lone_half_is_yarim(self):
        # "sıfır buçuk" is not Turkish.
        assert normalize_speech("0,5 milyon") == "yarım milyon"

    def test_leading_zeros_stay_audible(self):
        # Dropping the zero would turn 2,05 into 2,5.
        assert normalize_speech("2,05") == "iki virgül sıfır beş"

    def test_a_scaled_decimal_is_read_as_the_amount_it_is(self):
        # "iki virgül yetmiş beş milyon" makes the listener do the arithmetic.
        assert normalize_speech("2,75 milyar") == "iki milyar yedi yüz elli milyon"

    def test_a_currency_symbol_moves_behind_the_whole_amount(self):
        assert normalize_speech("€5 milyon") == "beş milyon avro"
        assert normalize_speech("$20") == "yirmi dolar"


class TestDates:
    def test_a_numeric_date_beats_the_thousands_separator(self):
        # Read as separators, "07.08.2026" would become one absurd number.
        assert normalize_speech("07.08.2026") == "yedi Ağustos iki bin yirmi altı"

    def test_an_impossible_date_is_left_to_the_other_rules(self):
        assert "Ağustos" not in normalize_speech("40.13.2026")


class TestSignsAndRanges:
    def test_a_leading_minus_becomes_eksi(self):
        assert normalize_speech("-5 derece") == "eksi beş derece"

    def test_a_hyphen_between_numbers_is_a_range_not_a_minus(self):
        assert normalize_speech("18-24 derece") == "on sekiz ile yirmi dört derece"


class TestWhatItLeavesAlone:
    def test_text_without_digits_is_returned_untouched(self):
        text = "Bültenimizi hava durumuyla tamamlıyoruz."
        assert normalize_speech(text) is text

    def test_a_sentence_full_stop_is_not_a_thousands_separator(self):
        assert normalize_speech("Yıl 2026. Buna göre.") == "Yıl iki bin yirmi altı. Buna göre."

    def test_empty_input_survives(self):
        assert normalize_speech("") == ""

    def test_percentages_keep_their_neighbours(self):
        assert normalize_speech("Enflasyon %2,5 arttı.") == "Enflasyon yüzde iki buçuk arttı."


class TestItNeverTouchesTheDisplaySide:
    """The rule the user was most explicit about.

    Chapter numbers, topic cards, lower thirds, file names, links and IDs must
    keep their digits. That is guaranteed structurally rather than by a filter:
    normalization is applied in ``_synthesis_text`` only, which nothing on the
    display path calls. These tests hold that structure in place.
    """

    def test_normalization_is_confined_to_the_synthesis_helper(self):
        import inspect

        from btcedu.core import tts

        source = inspect.getsource(tts)
        # One import, inside _synthesis_text. A second call site would mean the
        # rule had leaked onto the display path.
        assert source.count("normalize_speech") == 2

    def test_the_synthesis_helper_leaves_the_original_text_alone(self):
        from btcedu.core.tts import _synthesis_text

        original = "8 Ağustos'ta %70 yağış bekleniyor."
        spoken = _synthesis_text(original, {}, True)
        assert spoken == "sekiz Ağustos'ta yüzde yetmiş yağış bekleniyor."
        assert original == "8 Ağustos'ta %70 yağış bekleniyor."

    def test_a_profile_can_turn_it_off(self):
        from btcedu.core.tts import _synthesis_text

        assert _synthesis_text("%70", {}, False) == "%70"

    def test_the_lexicon_runs_before_the_numbers(self):
        # A lexicon entry must still be able to match the digits it was
        # written against.
        from btcedu.core.tts import _synthesis_text

        spoken = _synthesis_text("G7 zirvesi", {"G7": "Ge yedi"}, True)
        assert spoken == "Ge yedi zirvesi"

    def test_the_spoken_text_changes_the_idempotency_hash(self):
        # Otherwise today's episodes would keep yesterday's audio.
        from btcedu.core.tts import _chapter_tts_hash

        plain = _chapter_tts_hash("ch01", "%70", "%70", {})
        spoken = _chapter_tts_hash("ch01", "%70", "yüzde yetmiş", {})
        assert plain != spoken
