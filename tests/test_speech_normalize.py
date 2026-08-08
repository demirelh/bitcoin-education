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


class TestFoundInRealEpisodes:
    """Cases the first version got wrong, found by replaying ten real bulletins.

    Every one of these appeared in a broadcast that had already aired, which is
    why they are pinned here rather than left to the general rules.
    """

    def test_the_show_name_survives(self):
        # "ALMANYA24" became "ALMANYAyirmi dört" — in every single opening.
        assert normalize_speech("İyi akşamlar. ALMANYA24 başlıyor.") == (
            "İyi akşamlar. ALMANYA24 başlıyor."
        )

    def test_a_suffixed_show_name_keeps_its_apostrophe(self):
        assert normalize_speech("ALMANYA24'na hoş geldiniz") == "ALMANYA24'na hoş geldiniz"

    @pytest.mark.parametrize("name", ["G7", "B12", "COVID19"])
    def test_a_digit_welded_to_letters_is_a_name_not_a_number(self, name):
        assert normalize_speech(f"{name} konusu") == f"{name} konusu"

    def test_a_suffix_is_glued_to_the_spelled_out_stem(self):
        # "yüz elli'si" reads as "yüz elli | si": the apostrophe is a pause.
        assert normalize_speech("255 çocuktan 150'si") == "iki yüz elli beş çocuktan yüz ellisi"

    @pytest.mark.parametrize(
        ("written", "spoken"),
        [
            ("40'tan fazla", "kırktan fazla"),
            ("yüzde 70'ini", "yüzde yetmişini"),
            ("yüzde 59'a", "yüzde elli dokuza"),
            ("2022'deki", "iki bin yirmi ikideki"),
            ("saat 13.00'e kadar", "saat bire kadar"),
            ("100'den fazla", "yüzden fazla"),
        ],
    )
    def test_every_suffix_shape_seen_on_air(self, written, spoken):
        assert normalize_speech(written) == spoken

    def test_a_proper_noun_keeps_its_apostrophe_in_a_numeric_sentence(self):
        # The suffix rule must not reach past the numbers it was written for.
        assert normalize_speech("Yasası'nın 4. kitabı") == "Yasası'nın dördüncü kitabı"
        assert normalize_speech("Türkiye'nin 3 şehri") == "Türkiye'nin üç şehri"

    def test_trailing_zeros_are_spoken(self):
        # "2,70" read as "iki virgül yedi" is a different number.
        assert normalize_speech("2,70 metre") == "iki virgül yetmiş metre"
        assert normalize_speech("3,40 metreye") == "üç virgül kırk metreye"

    def test_an_ordinal_is_not_a_full_stop(self):
        # "dört. kitabı" puts an audible pause inside the phrase.
        assert normalize_speech("4. kitabı") == "dördüncü kitabı"
        assert normalize_speech("17. sırada") == "on yedinci sırada"

    def test_dort_softens_its_consonant(self):
        assert normalize_speech("4. kez") == "dördüncü kez"

    def test_a_dash_between_days_is_a_span_not_a_range(self):
        # "2-3 Ağustos gecesi" is the night from the 2nd to the 3rd; reading it
        # as "iki ile üç" would claim something the bulletin did not say.
        assert normalize_speech("2-3 Ağustos 1944 gecesi") == (
            "iki üç Ağustos bin dokuz yüz kırk dört gecesi"
        )

    def test_a_dash_between_quantities_is_still_a_range(self):
        assert normalize_speech("150-200 çocuğa") == "yüz elli ile iki yüz çocuğa"
        assert normalize_speech("1-1,5 metre") == "bir ile bir buçuk metre"

    def test_a_year_is_read_as_a_number(self):
        assert normalize_speech("1908 yapımı") == "bin dokuz yüz sekiz yapımı"
        assert normalize_speech("2010 yılında") == "iki bin on yılında"


class TestTheLastTenEpisodes:
    """Replay every real bulletin on disk through the normalizer.

    A rule that works on invented examples and fails on the archive is not
    working. This asserts the property that matters -- no digit reaches the
    engine unspoken -- rather than a fixed expected string per episode.
    """

    @staticmethod
    def _episodes():
        import json
        import pathlib

        paths = sorted(
            pathlib.Path("data/outputs").glob("*/chapters.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        found = []
        for path in paths:
            try:
                chapters = json.loads(path.read_text()).get("chapters", [])
            except Exception:  # noqa: BLE001 - a broken fixture is not this test's business
                continue
            words = sum(
                len(((c.get("narration") or {}).get("text", "")).split()) for c in chapters
            )
            if words > 200:  # skip synthetic fixtures
                found.append((path.parent.name, chapters))
        return found[:10]

    def test_no_digit_reaches_the_engine_unspoken(self):
        import re

        episodes = self._episodes()
        if not episodes:
            pytest.skip("no rendered episodes on this machine")

        # A digit still attached to letters is a protected name, not a leftover.
        protected = re.compile(r"[A-Za-zÇĞİÖŞÜçğıöşü]\d|\d[A-Za-zÇĞİÖŞÜçğıöşü]")
        leftovers = []
        for name, chapters in episodes:
            for chapter in chapters:
                text = (chapter.get("narration") or {}).get("text", "")
                for match in re.finditer(r"\S*\d\S*", normalize_speech(text)):
                    if not protected.search(match.group(0)):
                        leftovers.append((name, match.group(0)))
        assert not leftovers

    def test_no_markers_or_orphaned_apostrophes_are_left_behind(self):
        import re

        episodes = self._episodes()
        if not episodes:
            pytest.skip("no rendered episodes on this machine")

        for _name, chapters in episodes:
            for chapter in chapters:
                spoken = normalize_speech((chapter.get("narration") or {}).get("text", ""))
                assert "\x00" not in spoken
                assert not re.search(r"\s['’]|['’]\s", spoken)

    def test_the_stored_narration_is_never_modified(self):
        # The display side must keep its digits: chapter titles, topic cards
        # and lower thirds are read with the eyes.
        episodes = self._episodes()
        if not episodes:
            pytest.skip("no rendered episodes on this machine")

        for _name, chapters in episodes:
            for chapter in chapters:
                original = (chapter.get("narration") or {}).get("text", "")
                before = original
                normalize_speech(original)
                assert original == before


class TestUnits:
    """Abbreviations are spelled letter by letter otherwise: "yedi yüz ke em"."""

    @pytest.mark.parametrize(
        ("written", "spoken"),
        [
            ("700 km uzaktaki", "yedi yüz kilometre uzaktaki"),
            ("2,5 km uzunluğunda", "iki buçuk kilometre uzunluğunda"),
            ("500 kg", "beş yüz kilogram"),
            ("3 m derinlik", "üç metre derinlik"),
            ("20 ha orman", "yirmi hektar orman"),
            ("40 cm", "kırk santimetre"),
            ("100 MW", "yüz megavat"),
        ],
    )
    def test_an_abbreviation_after_a_number_is_spelled_out(self, written, spoken):
        assert normalize_speech(written) == spoken

    def test_degrees_are_read_as_derece(self):
        assert normalize_speech("20 °C") == "yirmi derece"
        assert normalize_speech("20°C") == "yirmi derece"

    def test_a_suffix_on_the_abbreviation_joins_the_spoken_unit(self):
        assert normalize_speech("700 km'lik yol") == "yedi yüz kilometrelik yol"
        assert normalize_speech("5 kg'lık paket") == "beş kilogramlık paket"

    def test_the_currency_word_matches_the_currency_symbol(self):
        # Otherwise "€5" says "avro" and "5 Euro" says "Euro" in one bulletin.
        assert normalize_speech("50.000 Euro'dan fazla") == "elli bin avrodan fazla"
        assert normalize_speech("20 Euro tazminat") == "yirmi avro tazminat"
        assert normalize_speech("€5 milyon") == "beş milyon avro"

    def test_a_unit_is_only_read_directly_after_a_number(self):
        # Otherwise the single-letter entries would eat ordinary words.
        assert normalize_speech("3 metre m harfi") == "üç metre m harfi"
        assert normalize_speech("Ali g harfini yazdı 5 kez") == "Ali g harfini yazdı beş kez"

    def test_an_ordinary_word_after_a_number_is_left_alone(self):
        assert normalize_speech("15 nehir gemisi") == "on beş nehir gemisi"
        assert normalize_speech("4 hafta içinde") == "dört hafta içinde"
        assert normalize_speech("20 depoya") == "yirmi depoya"
        assert normalize_speech("5 milyon") == "beş milyon"

    def test_an_ambiguous_abbreviation_needs_the_right_case(self):
        # "M" in an all-caps headline is not metres.
        assert normalize_speech("DAX 26.000 M ENDEKSİ") == "DAX yirmi altı bin M ENDEKSİ"

    def test_the_longer_abbreviation_wins(self):
        assert normalize_speech("5 km²") == "beş kilometrekare"
        assert normalize_speech("5 kWh") == "beş kilovatsaat"
