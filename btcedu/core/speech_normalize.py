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

#: Above this a written number is an identifier, not a quantity — an account
#: number or a code. Reading it as one enormous word would be worse than
#: leaving the digits for the engine.
_MAX_SPOKEN = 10**15


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
    fraction = fraction.rstrip("0")
    if not fraction:
        return number_to_turkish_words(whole)
    if fraction == "5":
        # 0,5 is "yarım" standing alone — "sıfır buçuk" is not Turkish.
        return "yarım" if whole == 0 else f"{number_to_turkish_words(whole)} buçuk"

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

    text = re.sub(r"(?<!\d)(\d+),(\d+)(?!\d)", decimal, text)

    def integer(match: re.Match[str]) -> str:
        return number_to_turkish_words(int(match.group(1)))

    return re.sub(r"(?<!\d)(\d+)(?!\d)", integer, text)


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


def normalize_speech(text: str) -> str:
    """Spell out every number in ``text`` as Turkish words.

    Intended for the synthesis text only — see the module docstring. Returns
    ``text`` unchanged when it holds no digits, which is the common case for
    short segments and keeps the work off the hot path.
    """
    if not text or not any(character.isdigit() for character in text):
        return text

    result = text
    # Order matters: each rule consumes digits the next one would misread.
    result = _sub_dates(result)
    result = _sub_times(result)
    result = _sub_currency_symbols(result)
    result = _sub_percent(result)
    result = _sub_scaled_decimals(result)
    result = _strip_thousands(result)
    result = _sub_ranges(result)
    result = _sub_negatives(result)
    result = _sub_plain_numbers(result)

    # Spelling out numbers leaves double spaces where digits used to be glued
    # to punctuation; the engine reads those as hesitations.
    return re.sub(r"[ \t]{2,}", " ", result).strip()
