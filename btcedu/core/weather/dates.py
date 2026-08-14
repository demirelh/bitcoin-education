"""Deterministic Turkish calendar helpers for weather visuals.

The weather card must always show an absolute date instead of relative wording
("Yarın", "Ertesi gün"). Relative markers found in the narration are resolved
against the episode broadcast date, so the badge format is identical every day.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta

# Turkish month names (nominative) used by the adaptation stage.
TR_MONTH_NAMES: dict[int, str] = {
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

TR_MONTHS: dict[str, int] = {name.casefold(): number for number, name in TR_MONTH_NAMES.items()}

# Monday == 0, matching :meth:`datetime.date.weekday`.
TR_WEEKDAY_NAMES: dict[int, str] = {
    0: "Pazartesi",
    1: "Salı",
    2: "Çarşamba",
    3: "Perşembe",
    4: "Cuma",
    5: "Cumartesi",
    6: "Pazar",
}

# Day reference identifiers produced by the extractor → weekday index.
WEEKDAY_REFERENCES: dict[str, int] = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}

# Captions shown above the date on the weather card.
_CAPTION_DEFAULT = "Tahmin"
_CAPTIONS: dict[str, str] = {
    "today": "Bugün",
    "tonight": "Bu gece",
    "coming_days": "Önümüzdeki günler",
    "weekend": "Hafta sonu",
}

_DATE_TEXT_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(TR_MONTHS) + r")\b",
    re.IGNORECASE,
)


def format_date(value: date) -> str:
    """Return the canonical badge format, e.g. ``3 Ağustos Pazartesi``."""
    return f"{value.day} {TR_MONTH_NAMES[value.month]} {TR_WEEKDAY_NAMES[value.weekday()]}"


def format_short_date(value: date) -> str:
    """Return a compact date without the weekday, e.g. ``3 Ağustos``."""
    return f"{value.day} {TR_MONTH_NAMES[value.month]}"


def format_range(start: date, end: date) -> str:
    """Return a date range label, e.g. ``4 – 6 Ağustos``."""
    if start == end:
        return format_date(start)
    if start.month == end.month and start.year == end.year:
        return f"{start.day} – {end.day} {TR_MONTH_NAMES[end.month]}"
    return f"{format_short_date(start)} – {format_short_date(end)}"


def parse_date_text(text: str | None, reference: date) -> date | None:
    """Parse ``"3 Ağustos Pazartesi"`` into a date near ``reference``.

    The narration never carries a year, so the year closest to the reference
    date is chosen (handles the December/January rollover).
    """
    if not text:
        return None
    match = _DATE_TEXT_RE.search(text)
    if not match:
        return None
    day = int(match.group(1))
    month = TR_MONTHS[match.group(2).casefold()]
    candidates: list[date] = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(date(year, month, day))
        except ValueError:
            continue
    if not candidates:
        return None
    return min(candidates, key=lambda candidate: abs((candidate - reference).days))


@dataclass(frozen=True)
class ForecastCalendar:
    """Resolves narration day references to absolute dates.

    ``broadcast_date`` is the day the episode aired; ``anchor_date`` is the main
    forecast day the narration headline refers to (usually the following day for
    the 20:00 broadcast).
    """

    broadcast_date: date
    anchor_date: date

    def next_weekday(self, weekday: int) -> date:
        """First date on or after the anchor with the given weekday index."""
        delta = (weekday - self.anchor_date.weekday()) % 7
        return self.anchor_date + timedelta(days=delta)

    def resolve(self, day_reference: str | None) -> tuple[date, date] | None:
        """Return the (start, end) dates a day reference denotes."""
        if not day_reference:
            return None
        if day_reference in ("today", "tonight"):
            return self.broadcast_date, self.broadcast_date
        if day_reference == "tomorrow":
            return self.broadcast_date + timedelta(days=1), self.broadcast_date + timedelta(days=1)
        if day_reference == "date":
            return self.anchor_date, self.anchor_date
        if day_reference == "following_day":
            return self.anchor_date + timedelta(days=1), self.anchor_date + timedelta(days=1)
        if day_reference == "coming_days":
            return self.anchor_date + timedelta(days=1), self.anchor_date + timedelta(days=3)
        if day_reference == "weekend":
            saturday = self.next_weekday(5)
            return saturday, saturday + timedelta(days=1)
        weekday = WEEKDAY_REFERENCES.get(day_reference)
        if weekday is not None:
            resolved = self.next_weekday(weekday)
            return resolved, resolved
        return None

    def label(self, day_reference: str | None, explicit_date: date | None = None) -> str | None:
        """Return the absolute date badge for a day reference."""
        if explicit_date is not None:
            return format_date(explicit_date)
        resolved = self.resolve(day_reference)
        if not resolved:
            return None
        start, end = resolved
        return format_range(start, end)

    @staticmethod
    def caption(day_reference: str | None) -> str:
        """Return the small caption rendered above the date."""
        if not day_reference:
            return _CAPTION_DEFAULT
        return _CAPTIONS.get(day_reference, _CAPTION_DEFAULT)


def build_calendar(
    date_text: str | None,
    broadcast_date: date | None,
) -> ForecastCalendar | None:
    """Create a calendar from the narration date and the broadcast date.

    Falls back to ``broadcast_date + 1`` when the narration carries no explicit
    date — the tagesschau 20:00 forecast always covers the following day.
    """
    if broadcast_date is None:
        return None
    anchor = parse_date_text(date_text, broadcast_date)
    if anchor is None or not -2 <= (anchor - broadcast_date).days <= 7:
        anchor = broadcast_date + timedelta(days=1)
    return ForecastCalendar(broadcast_date=broadcast_date, anchor_date=anchor)
