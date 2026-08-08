"""Turkish speech normalization for the text handed to the TTS engine.

ElevenLabs reads digits by guessing a language and a reading. "13.00" can come
out as a decimal, "%70" as "yüzde yetmiş" or as "yetmiş yüzde", and "2026" as
"iki bin yirmi altı" on one take and "yirmi yirmi altı" on the next. The engine
is not wrong so much as unguided, and a bulletin that says a number differently
each evening sounds unrehearsed.

So the numbers are spelled out here, deterministically, before the text leaves
for the API. The same sentence then always produces the same reading.

This runs on the **synthesis text only**. The approved narration, the chapter
titles, the topic cards, the lower thirds and everything written to disk keep
their digits — those are read with the eyes, where "%70" is clearer than
"yüzde yetmiş". :func:`normalize_speech` is deliberately a pure string function
with no knowledge of chapters or files, so it cannot reach that far.

The rules are ordered from most specific to most general: a date pattern must
be claimed before the thousands separator sees "07.08.2026", and a clock time
before "13.00" is read as a decimal. Every rule is anchored on digits, so text
without digits is returned untouched.
"""

from __future__ import annotations

import re

__all__ = ["normalize_speech", "number_to_turkish_words"]

_ONES = ("", "bir", "iki", "üç", "dört", "beş", "altı", "yedi", "sekiz", "dokuz")
_TENS = ("", "on", "yirmi", "otuz", "kırk", "elli", "altmış", "yetmiş", "seksen", "doksan")
#: Index is the power of a thousand. Beyond "katrilyon" a spoken bulletin has
#: left the realm of quantities a listener can picture, and the reading is not
#: worth guessing at.
_SCALES = ("", "bin", "milyon", "milyar", "trilyon", "katrilyon")

_MONTHS = {
    1: "Ocak",
    2: "Şubat",
    3: "Mart",
    4: "Nisan",
    5: "Mayıs",
    6: "Haziran",
    7: "Temmuz",
    8: "Ağustos",
    9: "Eylül",
    10: "Ekim",
    11: "Kasım",
    12: "Aralık",
}
_MONTH_NAMES = "|".join(_MONTHS.values())

#: Scale words that may follow a decimal, e.g. "2,75 milyon".
_SCALE_WORDS = {
    "bin": 1_000,
    "milyon": 1_000_000,
    "milyar": 1_000_000_000,
    "trilyon": 1_000_000_000_000,
}

_CURRENCY = {"€": "avro", "$": "dolar", "£": "sterlin", "₺": "lira"}

#: Abbreviated units, spelled out because the engine spells them letter by
#: letter: "yedi yüz ke em" for "700 km". Only applied directly after a number,
#: which is what keeps the single-letter entries from eating ordinary words.
#: Longest first at match time, so "km²" wins over "km" and "kWh" over "kW".
_UNITS = {
    "km²": "kilometrekare",
    "m²": "metrekare",
    "cm²": "santimetrekare",
    "km": "kilometre",
    "cm": "santimetre",
    "mm": "milimetre",
    "m": "metre",
    "kg": "kilogram",
    "mg": "miligram",
    "g": "gram",
    "ml": "mililitre",
    "lt": "litre",
    "ha": "hektar",
    "kWh": "kilovatsaat",
    "MWh": "megavatsaat",
    "GW": "gigavat",
    "MW": "megavat",
    "kW": "kilovat",
    "°C": "derece",
    "°": "derece",
    "sn": "saniye",
    "dk": "dakika",
    # Currency words the bulletin inherits from the German source. Without
    # these "€5" reads "beş avro" while "5 Euro" reads "5 Euro" — the same
    # amount said two ways in one broadcast.
    "Euro": "avro",
    "EUR": "avro",
    "USD": "dolar",
    "TL": "lira",
}

#: Ambiguous on their own, so they are only read as units when the case matches
#: exactly. "M" in an all-caps headline is not metres.
_CASE_SENSITIVE_UNITS = frozenset({"m", "g", "lt", "ml", "TL", "MW", "GW", "kW", "kWh", "MWh"})

#: Ordinal endings attach to the last word of the cardinal, and the vowel
#: harmony is irregular enough to be worth a table. "dört" also softens its
#: final consonant: "dördüncü", never "dörtüncü".
_ORDINALS = {
    "bir": "birinci",
    "iki": "ikinci",
    "üç": "üçüncü",
    "dört": "dördüncü",
    "beş": "beşinci",
    "altı": "altıncı",
    "yedi": "yedinci",
    "sekiz": "sekizinci",
    "dokuz": "dokuzuncu",
    "on": "onuncu",
    "yirmi": "yirminci",
    "otuz": "otuzuncu",
    "kırk": "kırkıncı",
    "elli": "ellinci",
    "altmış": "altmışıncı",
    "yetmiş": "yetmişinci",
    "seksen": "sekseninci",
    "doksan": "doksanıncı",
    "yüz": "yüzüncü",
    "bin": "bininci",
    "milyon": "milyonuncu",
    "milyar": "milyarıncı",
}

#: Above this a written number is an identifier, not a quantity — an account
#: number or a code. Reading it as one enormous word would be worse than
#: leaving the digits for the engine.
_MAX_SPOKEN = 10**15

#: A digit welded to letters is part of a name or a code -- "ALMANYA24", "G7",
#: "B12" -- and spelling it out destroys the word: "ALMANYAyirmi dört". The
#: show name alone appears in every opening, so this guard comes before every
#: numeric rule. Names that still need help belong in the pronunciation lexicon.
_GLUED_TO_LETTER = r"(?<![A-Za-zÀ-ÖØ-öø-ÿÇĞİıÖŞÜçğöşü])"


def _group_to_words(group: int, scale_index: int) -> str:
    """Spell one three-digit group, with the article Turkish leaves out.

    "bir yüz" and "bir bin" are not said; the bare "yüz" and "bin" carry the
    one. Millions keep theirs, so "bir milyon" is right where "bir bin" is not.
    """
    parts: list[str] = []
    hundreds, rest = divmod(group, 100)
    tens, ones = divmod(rest, 10)

    if hundreds:
        parts.append("yüz" if hundreds == 1 else f"{_ONES[hundreds]} yüz")
    if tens:
        parts.append(_TENS[tens])
    if ones:
        # A lone "bir" before "bin" is dropped: 1000 is "bin", not "bir bin".
        if not (ones == 1 and scale_index == 1 and not hundreds and not tens):
            parts.append(_ONES[ones])
    return " ".join(parts)


def number_to_turkish_words(value: int) -> str:
    """Spell a non-negative integer in Turkish.

    Handles the two places Turkish drops the "one" (yüz, bin) and keeps it
    everywhere else, so 1_000_000 reads "bir milyon" while 1_000 reads "bin".
    """
    if value < 0:
        return f"eksi {number_to_turkish_words(-value)}"
    if value == 0:
        return "sıfır"
    if value >= _MAX_SPOKEN:
        return str(value)

    groups: list[int] = []
    remaining = value
    while remaining:
        remaining, group = divmod(remaining, 1000)
        groups.append(group)

    parts: list[str] = []
    for scale_index in range(len(groups) - 1, -1, -1):
        group = groups[scale_index]
        if not group:
            continue
        words = _group_to_words(group, scale_index)
        scale = _SCALES[scale_index] if scale_index < len(_SCALES) else ""
        parts.append(f"{words} {scale}".strip() if scale else words)
    return " ".join(p for p in parts if p)


def _decimal_to_words(whole: int, fraction: str) -> str:
    """Spell a decimal the way it is actually said.

    Halves get "buçuk" because that is what a presenter says; "iki virgül beş"
    is what a spreadsheet says. Everything else falls back to "virgül" with the
    fractional digits read as a number, except leading zeros, which have to stay
    audible: 2,05 is "iki virgül sıfır beş", never "iki virgül beş".
    """
    if not fraction.strip("0"):
        # 2,0 and 2,00 are just two.
        return number_to_turkish_words(whole)
    if fraction.rstrip("0") == "5":
        # 0,5 is "yarım" standing alone — "sıfır buçuk" is not Turkish.
        return "yarım" if whole == 0 else f"{number_to_turkish_words(whole)} buçuk"

    # Trailing zeros are read, not dropped: 2,70 is "iki virgül yetmiş", and
    # "iki virgül yedi" is a different number written a different way.
    leading_zeros = len(fraction) - len(fraction.lstrip("0"))
    spoken_fraction = " ".join(["sıfır"] * leading_zeros)
    remainder = fraction.lstrip("0")
    if remainder:
        tail = number_to_turkish_words(int(remainder))
        spoken_fraction = f"{spoken_fraction} {tail}".strip()
    return f"{number_to_turkish_words(whole)} virgül {spoken_fraction}".strip()


def _clock_to_words(hour: int, minute: int) -> str:
    """Say a time of day the way a bulletin says it.

    Turkish news uses the twelve-hour reading in speech even when the schedule
    is written in twenty-four, so 20.00 is "saat sekiz". Minutes other than the
    half hour are read plainly rather than with "geçe"/"kala", which would need
    the hour in an oblique case and is easy to get wrong.
    """
    spoken_hour = hour % 12 or 12
    if minute == 0:
        return f"saat {number_to_turkish_words(spoken_hour)}"
    if minute == 30:
        return f"saat {number_to_turkish_words(spoken_hour)} buçuk"
    return f"saat {number_to_turkish_words(spoken_hour)} {number_to_turkish_words(minute)}"


def _strip_thousands(text: str) -> str:
    """Turn "98.000" into "98000" so it can be read as one number.

    Only a dot followed by exactly three digits counts. A clock time has two
    ("13.00") and a sentence end has none, so neither is touched here.
    """
    pattern = re.compile(r"(?<!\d)(\d{1,3})((?:\.\d{3})+)(?!\d)")
    return pattern.sub(lambda m: m.group(1) + m.group(2).replace(".", ""), text)


def _sub_dates(text: str) -> str:
    """Numeric dates, before the thousands separator can claim them."""

    def full_date(match: re.Match[str]) -> str:
        day, month, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if not (1 <= day <= 31 and 1 <= month <= 12):
            return match.group(0)
        return (
            f"{number_to_turkish_words(day)} {_MONTHS[month]} "
            f"{number_to_turkish_words(year)}"
        )

    text = re.sub(r"(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)", full_date, text)

    def day_span(match: re.Match[str]) -> str:
        """"2-3 Ağustos" is the night from the 2nd to the 3rd, not "2 to 3"."""
        first, second = int(match.group(1)), int(match.group(2))
        if not (1 <= first <= 31 and 1 <= second <= 31):
            return match.group(0)
        return (
            f"{number_to_turkish_words(first)} {number_to_turkish_words(second)} "
            f"{match.group(3)}"
        )

    text = re.sub(
        rf"(?<!\d)(\d{{1,2}})\s*-\s*(\d{{1,2}})\s+({_MONTH_NAMES})\b", day_span, text
    )

    def day_month(match: re.Match[str]) -> str:
        day = int(match.group(1))
        if not 1 <= day <= 31:
            return match.group(0)
        return f"{number_to_turkish_words(day)} {match.group(2)}"

    return re.sub(rf"(?<!\d)(\d{{1,2}})\s+({_MONTH_NAMES})\b", day_month, text)


def _sub_times(text: str) -> str:
    """Clock times written with a colon or a dot."""

    def repl(match: re.Match[str]) -> str:
        hour, minute = int(match.group(2)), int(match.group(3))
        if hour > 23 or minute > 59:
            return match.group(0)
        spoken = _clock_to_words(hour, minute)
        # The reading supplies its own "saat", so a written one is absorbed
        # rather than repeated. Its capitalisation is kept: the word may open
        # the sentence.
        written = match.group(1)
        if written and written[0].isupper():
            return spoken.capitalize()
        return spoken

    text = re.sub(r"(?i)(saat\s+)?(?<!\d)([01]?\d|2[0-3]):([0-5]\d)(?!\d)", repl, text)
    # The dotted form is ambiguous with a thousands separator, so it is only
    # read as a time when exactly two digits follow ("13.00", never "13.000").
    return re.sub(r"(?i)(saat\s+)?(?<!\d)([01]?\d|2[0-3])\.([0-5]\d)(?!\d)", repl, text)


def _sub_percent(text: str) -> str:
    """Percentages, written before the number in Turkish and after it elsewhere."""

    def spoken(number: str) -> str:
        cleaned = _strip_thousands(number)
        if "," in cleaned:
            whole, _, fraction = cleaned.partition(",")
            return _decimal_to_words(int(whole or 0), fraction)
        return number_to_turkish_words(int(cleaned))

    text = re.sub(
        r"%\s*(\d[\d.]*(?:,\d+)?)",
        lambda m: f"yüzde {spoken(m.group(1))}",
        text,
    )
    return re.sub(
        r"(?<![\w])(\d[\d.]*(?:,\d+)?)\s*%",
        lambda m: f"yüzde {spoken(m.group(1))}",
        text,
    )


def _sub_currency_symbols(text: str) -> str:
    """Move a leading currency symbol behind the amount, as Turkish says it."""
    symbols = "".join(re.escape(s) for s in _CURRENCY)
    scale_pattern = "|".join(_SCALE_WORDS)
    # The scale word is part of the amount: "€5 milyon" is five million euros,
    # not five euros of a million.
    return re.sub(
        rf"([{symbols}])\s*(\d[\d.,]*)(\s+(?:{scale_pattern})\b)?",
        lambda m: f"{m.group(2)}{m.group(3) or ''} {_CURRENCY[m.group(1)]}",
        text,
    )


def _sub_units(text: str) -> str:
    """Spell out an abbreviated unit standing right after a number.

    Runs before the digits are spelled out, because "700 km" is recognisable as
    a measurement and "yedi yüz km" is just a word followed by two letters.

    A suffix attached to the abbreviation ("km'lik") is glued on the same way a
    suffix on a numeral is: it was chosen to harmonise with the spoken unit,
    which is the form it ends up on.
    """

    def repl(match: re.Match[str]) -> str:
        written = match.group("unit")
        spoken = _UNITS.get(written)
        if spoken is None:
            # Only a case difference away from a unit; accept it unless the
            # abbreviation is one of the ambiguous ones.
            for key, value in _UNITS.items():
                if key.lower() == written.lower() and key not in _CASE_SENSITIVE_UNITS:
                    spoken = value
                    break
        if spoken is None:
            return match.group(0)
        suffix = _SUFFIX_MARK if match.group("apos") else ""
        return f"{match.group('number')} {spoken}{suffix}"

    # Longest abbreviation first: "km²" must be tried before "km".
    alternatives = "|".join(re.escape(u) for u in sorted(_UNITS, key=len, reverse=True))
    pattern = re.compile(
        rf"(?P<number>\d)\s*(?P<unit>{alternatives})"
        # Either a suffix follows, or nothing word-like does. Written as one
        # alternation because a trailing "not a word character" check would
        # reject the suffix it is meant to allow.
        rf"(?:(?P<apos>['’])(?=[a-zçğıöşü])|(?![\w²³]))"
    )
    return pattern.sub(repl, text)


def _sub_scaled_decimals(text: str) -> str:
    """Spell "2,75 milyon" as the amount it is: "iki milyon yedi yüz elli bin".

    A money figure is heard as a quantity, not as a digit string, and
    "iki virgül yetmiş beş milyon" makes the listener do the arithmetic. Halves
    keep "buçuk", which is both shorter and what a presenter would say.
    """
    scale_pattern = "|".join(_SCALE_WORDS)

    def repl(match: re.Match[str]) -> str:
        whole = int(match.group(1))
        fraction = match.group(2)
        scale_word = match.group(3)
        if fraction.rstrip("0") == "5":
            half = "yarım" if whole == 0 else f"{number_to_turkish_words(whole)} buçuk"
            return f"{half} {scale_word}"
        multiplier = _SCALE_WORDS[scale_word]
        scaled = (whole * multiplier) + int(
            round(float(f"0.{fraction}") * multiplier)
        )
        return number_to_turkish_words(scaled)

    return re.sub(
        rf"(?<!\d)(\d+),(\d+)\s+({scale_pattern})\b",
        repl,
        text,
    )


def _sub_plain_numbers(text: str) -> str:
    """Whatever digits are left: decimals, then integers.

    Runs last so the specific readings above have already claimed their text.
    """

    def decimal(match: re.Match[str]) -> str:
        return _decimal_to_words(int(match.group(1)), match.group(2))

    text = re.sub(_GLUED_TO_LETTER + r"(?<!\d)(\d+),(\d+)(?!\d)", decimal, text)

    def integer(match: re.Match[str]) -> str:
        return number_to_turkish_words(int(match.group(1)))

    return re.sub(_GLUED_TO_LETTER + r"(?<!\d)(\d+)(?!\d)", integer, text)


def _ordinal_to_words(value: int) -> str:
    """Spell a Turkish ordinal: only the last word of the cardinal inflects."""
    cardinal = number_to_turkish_words(value)
    head, _, last = cardinal.rpartition(" ")
    suffixed = _ORDINALS.get(last)
    if suffixed is None:
        return cardinal
    return f"{head} {suffixed}".strip()


def _sub_ordinals(text: str) -> str:
    """A digit followed by a dot is an ordinal — unless it ends the sentence.

    Turkish writes ordinals as "4." and sentences also end in a dot, so the two
    are indistinguishable on the character alone. What separates them is what
    follows: an ordinal is followed by its noun in lower case, a full stop by a
    new sentence in upper case. Left unconverted the dot becomes an audible
    pause in the middle of a phrase.
    """

    def repl(match: re.Match[str]) -> str:
        return f"{_ordinal_to_words(int(match.group(1)))} {match.group(2)}"

    return re.sub(r"(?<!\d)(\d{1,4})\.\s+([a-zçğıöşü])", repl, text)


def _sub_ranges(text: str) -> str:
    """A hyphen between two numbers is a range: "18-24" is "18 ile 24".

    Left as a bare hyphen the engine either swallows it or reads a minus, and
    "on sekiz-yirmi dört derece" is not a temperature anyone says out loud.
    """
    return re.sub(r"(?<=\d)\s*-\s*(?=\d)", " ile ", text)


def _sub_negatives(text: str) -> str:
    """A leading minus becomes "eksi" — winter temperatures depend on it.

    A hyphen between digits is a range ("18-24 derece"), not a sign, so the
    minus is only read when nothing word-like precedes it.
    """
    return re.sub(r"(?<![\w\d])-(?=\d)", "eksi ", text)


#: Stands in for an apostrophe that only existed because its stem was written
#: as a numeral. Marking it up front is what keeps proper nouns out of it:
#: "Türkiye'nin" must keep its apostrophe even in a sentence full of figures.
_SUFFIX_MARK = "\x00"


def _mark_digit_suffixes(text: str) -> str:
    """Set aside the apostrophes that belong to numbers, before spelling them.

    Turkish attaches suffixes to digits with an apostrophe -- "150'si",
    "%70'ini" -- purely because the stem is a numeral. Once the stem is a word
    the apostrophe has no reason to exist, and the engine reads it as a break:
    "yüz elli | si".
    """
    return re.sub(
        _GLUED_TO_LETTER + r"(?<!\d)(\d[\d.,:]*)['’](?=[a-zçğıöşü])",
        lambda m: m.group(1) + _SUFFIX_MARK,
        text,
    )


def _join_suffixes(text: str) -> str:
    """Drop the marked apostrophes, gluing each suffix to its spelled-out stem.

    The suffix needs no adjustment: it was already chosen to harmonise with the
    spoken form, which is the form it is now attached to.
    """
    return text.replace(_SUFFIX_MARK, "")


def normalize_speech(text: str) -> str:
    """Spell out every number in ``text`` as Turkish words.

    Intended for the synthesis text only — see the module docstring. Returns
    ``text`` unchanged when it holds no digits, which is the common case for
    short segments and keeps the work off the hot path.
    """
    if not text or not any(character.isdigit() for character in text):
        return text

    result = _mark_digit_suffixes(text)
    # Order matters: each rule consumes digits the next one would misread.
    result = _sub_dates(result)
    result = _sub_ordinals(result)
    result = _sub_times(result)
    result = _sub_currency_symbols(result)
    result = _sub_percent(result)
    result = _sub_units(result)
    result = _sub_scaled_decimals(result)
    result = _strip_thousands(result)
    result = _sub_ranges(result)
    result = _sub_negatives(result)
    result = _sub_plain_numbers(result)
    result = _join_suffixes(result)

    # Spelling out numbers leaves double spaces where digits used to be glued
    # to punctuation; the engine reads those as hesitations.
    return re.sub(r"[ \t]{2,}", " ", result).strip()
