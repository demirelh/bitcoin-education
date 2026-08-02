"""Deterministic weather data extraction from approved Turkish narration.

Extracts ONLY claims grounded in the source text. Never invents data.
Uses regex-based parsing for temperatures, regions, conditions, and time refs.

Key design: clause-level/nearest association — conditions are associated only
with regions mentioned in the same clause. When multiple regions share a clause
(e.g. "Kuzey ve doğu kesimlerinde sağanak"), all share the condition. When a
sentence has multiple comma-separated clauses with distinct regions and
conditions, each clause's conditions bind only to that clause's regions.
"""

from __future__ import annotations

import hashlib
import logging
import re

from btcedu.core.weather.lexicon import CONDITION_LEXICON_TR
from btcedu.core.weather.models import (
    ForecastReference,
    RegionId,
    SourceSpan,
    WeatherCondition,
    WeatherData,
    WeatherOverview,
    WeatherRegion,
    WeatherWarning,
)

logger = logging.getLogger(__name__)

# Region lexicon: Turkish label → RegionId
_REGION_LEXICON_TR: dict[str, RegionId] = {
    "kuzey": RegionId.NORTH,
    "kuzeyde": RegionId.NORTH,
    "kuzeydoğu": RegionId.NORTHEAST,
    "kuzeybatı": RegionId.NORTHWEST,
    "güney": RegionId.SOUTH,
    "güneyde": RegionId.SOUTH,
    "güneydoğu": RegionId.SOUTHEAST,
    "güneybatı": RegionId.SOUTHWEST,
    "güneybatıda": RegionId.SOUTHWEST,
    "doğu": RegionId.EAST,
    "doğuda": RegionId.EAST,
    "batı": RegionId.WEST,
    "batıda": RegionId.WEST,
    "orta": RegionId.CENTRAL,
    "orta kesim": RegionId.CENTRAL,
    "kıyı": RegionId.COAST,
    "kıyılarda": RegionId.COAST,
    "alpler": RegionId.ALPS,
    # Suffixed locative forms
    "kuzey kesimler": RegionId.NORTH,
    "güney kesimler": RegionId.SOUTH,
    "doğu kesimler": RegionId.EAST,
    "batı kesimler": RegionId.WEST,
}

# Region label map for display
_REGION_LABELS_TR: dict[RegionId, str] = {
    RegionId.GERMANY: "Almanya",
    RegionId.NORTH: "Kuzey",
    RegionId.NORTHEAST: "Kuzeydoğu",
    RegionId.NORTHWEST: "Kuzeybatı",
    RegionId.SOUTH: "Güney",
    RegionId.SOUTHEAST: "Güneydoğu",
    RegionId.SOUTHWEST: "Güneybatı",
    RegionId.EAST: "Doğu",
    RegionId.WEST: "Batı",
    RegionId.CENTRAL: "Orta",
    RegionId.COAST: "Kıyı",
    RegionId.ALPS: "Alpler",
}

# Temperature regex: supports signed values like "-5 ile 2 derece", "20 ile 29 derece"
# Also supports Unicode minus (−) and em/en dashes as separators.
_TEMP_RANGE_RE = re.compile(
    r"([−\-]?\d{1,2})\s*(?:ile|bis|[-–—]|ve)\s*([−\-]?\d{1,2})\s*(?:derece|grad|°\s*[cC]?)",
    re.IGNORECASE,
)

# Single temperature: "29 derece" or "-5°C" (signed)
_TEMP_SINGLE_RE = re.compile(r"([−\-]?\d{1,2})\s*(?:derece|grad|°\s*[cC]?)", re.IGNORECASE)

# Day reference patterns
_DAY_REF_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\b(?:ertesi|sonraki)\s+gün\b", re.IGNORECASE), "following_day"),
    (re.compile(r"\byarın\b", re.IGNORECASE), "tomorrow"),
    (re.compile(r"\bbugün\b", re.IGNORECASE), "today"),
    (re.compile(r"\bhafta\s*sonu\b", re.IGNORECASE), "weekend"),
    (re.compile(r"\bpazartesi\b", re.IGNORECASE), "monday"),
    (re.compile(r"\bsalı\b", re.IGNORECASE), "tuesday"),
    (re.compile(r"\bçarşamba\b", re.IGNORECASE), "wednesday"),
    (re.compile(r"\bperşembe\b", re.IGNORECASE), "thursday"),
    (re.compile(r"\bcuma\b", re.IGNORECASE), "friday"),
    (re.compile(r"\bcumartesi\b", re.IGNORECASE), "saturday"),
    (re.compile(r"\bpazar\b", re.IGNORECASE), "sunday"),
]

# Additional temporal markers used only for per-scene day context (not for the
# global forecast reference, which stays backwards compatible).
_CONTEXT_DAY_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bbu\s+gece\b", re.IGNORECASE), "tonight"),
    (re.compile(r"\bgece\s+başlangıcında\b", re.IGNORECASE), "tonight"),
    (re.compile(r"\bönümüzdeki\s+günlerde\b", re.IGNORECASE), "coming_days"),
    (re.compile(r"\btakip\s+eden\s+günlerde\b", re.IGNORECASE), "coming_days"),
    *_DAY_REF_PATTERNS,
]

# Turkish display labels for day references.
_DAY_LABELS_TR: dict[str, str] = {
    "today": "Bugün",
    "tonight": "Bu gece",
    "tomorrow": "Yarın",
    "following_day": "Ertesi gün",
    "weekend": "Hafta sonu",
    "coming_days": "Önümüzdeki günler",
    "monday": "Pazartesi",
    "tuesday": "Salı",
    "wednesday": "Çarşamba",
    "thursday": "Perşembe",
    "friday": "Cuma",
    "saturday": "Cumartesi",
    "sunday": "Pazar",
}

# Weekday name (lowercase, as it appears inside a date text) → day reference.
_WEEKDAY_TO_REF: dict[str, str] = {
    "pazartesi": "monday",
    "salı": "tuesday",
    "çarşamba": "wednesday",
    "perşembe": "thursday",
    "cuma": "friday",
    "cumartesi": "saturday",
    "pazar": "sunday",
}


def day_label_for(day_reference: str | None) -> str | None:
    """Return the Turkish display label for a day reference."""
    if not day_reference:
        return None
    return _DAY_LABELS_TR.get(day_reference)


def day_reference_in_text(text: str) -> str | None:
    """Return the earliest explicit day reference mentioned in ``text``."""
    best: tuple[int, str] | None = None
    for pattern, ref_name in _CONTEXT_DAY_PATTERNS:
        match = pattern.search(text)
        if match and (best is None or match.start() < best[0]):
            best = (match.start(), ref_name)
    return best[1] if best else None


def _anchor_refs(forecast_ref: ForecastReference) -> set[str]:
    """Day references that provably denote the forecast's anchor date.

    When the narration contains an explicit date ("2 Ağustos Pazar"), only that
    weekday is the anchor. Relative markers such as "yarın" are not equated with
    the date, because that mapping is not grounded in the text.
    """
    if forecast_ref.date_text:
        lowered = forecast_ref.date_text.casefold()
        weekdays = {ref for weekday, ref in _WEEKDAY_TO_REF.items() if weekday in lowered}
        if weekdays:
            return weekdays
    return {forecast_ref.day_reference} if forecast_ref.day_reference else set()


def _compose_day_label(
    day_reference: str | None,
    forecast_ref: ForecastReference,
    anchor_refs: set[str],
) -> str | None:
    """Build the display label for a day reference, grounded in the narration."""
    if not day_reference:
        return None
    base = _DAY_LABELS_TR.get(day_reference)
    date_text = forecast_ref.date_text
    if date_text and day_reference in anchor_refs:
        if base and base.casefold() not in date_text.casefold():
            return f"{base} · {date_text}"
        return date_text
    return base


def _build_day_contexts(
    sentences: list[tuple[str, int, int]],
    forecast_ref: ForecastReference,
) -> list[tuple[int, int, str | None, str | None]]:
    """Resolve which forecast day each sentence describes.

    The day reference is sticky: a sentence without its own temporal marker
    inherits the day of the preceding sentence. Only markers present in the
    narration are used — nothing is inferred from the calendar.
    """
    anchors = _anchor_refs(forecast_ref)
    contexts: list[tuple[int, int, str | None, str | None]] = []
    current_ref: str | None = None
    current_label: str | None = None

    for sentence, start, end in sentences:
        date_match = _DATE_TEXT_RE.search(sentence)
        sentence_ref: str | None = None
        sentence_label: str | None = None

        if date_match:
            matched_date = date_match.group(0)
            lowered = matched_date.casefold()
            sentence_ref = next(
                (ref for weekday, ref in _WEEKDAY_TO_REF.items() if weekday in lowered),
                "date",
            )
            sentence_label = matched_date
        else:
            best: tuple[int, str] | None = None
            for pattern, ref_name in _CONTEXT_DAY_PATTERNS:
                match = pattern.search(sentence)
                if match and (best is None or match.start() < best[0]):
                    best = (match.start(), ref_name)
            if best:
                sentence_ref = best[1]
                sentence_label = _compose_day_label(sentence_ref, forecast_ref, anchors)

        if sentence_ref:
            current_ref = sentence_ref
            current_label = sentence_label
        contexts.append((start, end, current_ref, current_label))

    return contexts


def _day_context_at(
    contexts: list[tuple[int, int, str | None, str | None]],
    offset: int,
) -> tuple[str | None, str | None]:
    """Return (day_reference, day_label) for a character offset."""
    resolved: tuple[str | None, str | None] = (None, None)
    for start, end, ref, label in contexts:
        if start <= offset < end:
            return ref, label
        if start <= offset:
            resolved = (ref, label)
    return resolved

_DATE_TEXT_RE = re.compile(
    r"\b\d{1,2}\s+"
    r"(?:ocak|şubat|mart|nisan|mayıs|haziran|temmuz|ağustos|eylül|ekim|kasım|aralık)"
    r"(?:\s+(?:pazartesi|salı|çarşamba|perşembe|cuma|cumartesi|pazar))?\b",
    re.IGNORECASE,
)

_OUTLOOK_RE = re.compile(
    r"\b(?:önümüzdeki\s+günlerde|ertesi\s+gün|sonraki\s+gün|hafta\s+sonunda)\b",
    re.IGNORECASE,
)

# Warning keywords
_WARNING_PATTERNS = {
    re.compile(r"(?<!\w)fırtına uyarısı(?!\w)", re.IGNORECASE): "storm",
    re.compile(r"(?<!\w)kasırga şiddetinde rüzg[aâ]r(?!\w)", re.IGNORECASE): "wind",
    re.compile(r"(?<!\w)uyarı(?!\w)", re.IGNORECASE): "warning",
    re.compile(r"(?<!\w)dikkat(?!\w)", re.IGNORECASE): "caution",
    re.compile(r"(?<!\w)sel(?!\w)", re.IGNORECASE): "flood",
    re.compile(r"(?<!\w)don(?!\w)", re.IGNORECASE): "frost",
    re.compile(r"(?<!\w)sıcak hava dalgası(?!\w)", re.IGNORECASE): "heat",
}

# Sentence splitter
_SENTENCE_RE = re.compile(r"[.!?]+\s*|\n+")

# Commas and semicolons separate claims. Turkish "ise" stays inside the claim
# because it commonly connects a region to its condition ("Güneyde ise güneşli").
_CLAUSE_SPLIT_RE = re.compile(r",\s*|;\s*", re.IGNORECASE)

# Shared-region connector: "X ve Y kesimlerinde" or "X ve Y'de"
_SHARED_REGION_RE = re.compile(
    r"((?:kuzey|güney|doğu|batı|kuzeydoğu|kuzeybatı|güneydoğu|güneybatı|orta)"
    r"(?:\s+ve\s+"
    r"(?:kuzey|güney|doğu|batı|kuzeydoğu|kuzeybatı|güneydoğu|güneybatı|orta))+)",
    re.IGNORECASE,
)


def _split_sentences(text: str) -> list[tuple[str, int, int]]:
    """Split text into sentences with start/end offsets."""
    sentences = []
    pos = 0
    for m in _SENTENCE_RE.finditer(text):
        sentence = text[pos : m.start()].strip()
        if sentence:
            sentences.append((sentence, pos, m.start()))
        pos = m.end()
    # Remaining text
    remaining = text[pos:].strip()
    if remaining:
        sentences.append((remaining, pos, len(text)))
    return sentences


def _find_regions_in_text(text: str) -> list[RegionId]:
    """Find all region mentions in a text fragment."""
    regions: list[RegionId] = []
    text_lower = text.lower()
    # Check multi-word entries first (longest match)
    for key in sorted(_REGION_LEXICON_TR.keys(), key=len, reverse=True):
        if key in text_lower:
            rid = _REGION_LEXICON_TR[key]
            if rid not in regions:
                regions.append(rid)
            # Remove matched text to avoid sub-matches
            text_lower = text_lower.replace(key, " " * len(key), 1)
    return regions


def _find_conditions_in_text(text: str) -> list[WeatherCondition]:
    """Find all weather conditions in a text fragment."""
    conditions = []
    remaining = text.lower()
    # Check longer phrases first
    for phrase in sorted(CONDITION_LEXICON_TR, key=len, reverse=True):
        pattern = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.IGNORECASE)
        match = pattern.search(remaining)
        if match:
            cond = CONDITION_LEXICON_TR[phrase]
            if cond not in conditions:
                conditions.append(cond)
                remaining = (
                    remaining[: match.start()]
                    + (" " * (match.end() - match.start()))
                    + remaining[match.end() :]
                )
    return conditions


def _parse_signed_temp(raw: str) -> int:
    """Parse a temperature string that may have a Unicode minus (−) or ASCII minus."""
    normalized = raw.replace("\u2212", "-").strip()
    return int(normalized)


def _extract_clauses(sentence: str) -> list[str]:
    """Split a sentence into clauses for nearest-region association."""
    initial_parts = [p.strip() for p in _CLAUSE_SPLIT_RE.split(sentence) if p.strip()]
    clauses: list[str] = []
    region_after_and = re.compile(
        r"\s+ve\s+(?=(?:kuzey|güney|doğu|batı|orta)\w*\b)",
        re.IGNORECASE,
    )
    for part in initial_parts:
        match = region_after_and.search(part)
        if (
            match
            and _find_regions_in_text(part[: match.start()])
            and _find_conditions_in_text(part[: match.start()])
        ):
            clauses.extend([part[: match.start()].strip(), part[match.end() :].strip()])
        else:
            clauses.append(part)
    return clauses


def extract_weather_data(
    narration_text: str,
    *,
    story_id: str | None = None,
    source_text: str = "",
    language: str = "tr",
) -> WeatherData:
    """Extract structured weather data from approved narration text.

    Only extracts claims explicitly present in the text. Never invents data.
    Uses deterministic regex/lexicon parsing with clause-level association.
    """
    text_hash = hashlib.sha256(narration_text.encode()).hexdigest()[:16]

    # Extract forecast time reference
    forecast_ref = ForecastReference()
    date_match = _DATE_TEXT_RE.search(narration_text)
    if date_match:
        forecast_ref.date_text = date_match.group(0)
    for pattern, ref_name in _DAY_REF_PATTERNS:
        if pattern.search(narration_text):
            forecast_ref.day_reference = ref_name
            break

    # Extract temperature range (global)
    overview = WeatherOverview()
    temp_match = _TEMP_RANGE_RE.search(narration_text)
    if temp_match:
        t1 = _parse_signed_temp(temp_match.group(1))
        t2 = _parse_signed_temp(temp_match.group(2))
        overview.temperature_min_c = min(t1, t2)
        overview.temperature_max_c = max(t1, t2)
        overview.source_span = SourceSpan(
            text=temp_match.group(0),
            start=temp_match.start(),
            end=temp_match.end(),
        )

    # Parse sentences for regional forecasts using clause-level association
    sentences = _split_sentences(narration_text)
    day_contexts = _build_day_contexts(sentences, forecast_ref)
    regions: list[WeatherRegion] = []
    unresolved: list[str] = []

    if temp_match:
        overview.day_reference, overview.day_label_tr = _day_context_at(
            day_contexts, temp_match.start()
        )

    for sentence, sent_start, sent_end in sentences:
        clauses = _extract_clauses(sentence)
        day_ref, day_label = _day_context_at(day_contexts, sent_start)

        if len(clauses) <= 1:
            # Single clause or unsplittable — use whole sentence
            _process_clause(
                sentence,
                sent_start,
                sent_end,
                regions,
                unresolved,
                sentence,
                day_ref,
                day_label,
            )
        else:
            # Multiple clauses — clause-level nearest association
            clause_had_match = False
            for clause in clauses:
                # Find clause position within original text
                clause_offset = sentence.find(clause)
                clause_start = sent_start + clause_offset if clause_offset >= 0 else sent_start
                clause_end = clause_start + len(clause)
                matched = _process_clause(
                    clause,
                    clause_start,
                    clause_end,
                    regions,
                    unresolved,
                    sentence,
                    day_ref,
                    day_label,
                )
                if matched:
                    clause_had_match = True

            if not clause_had_match:
                # Try whole sentence as fallback (regions in one clause,
                # conditions in another within the same sentence)
                _process_clause(
                    sentence,
                    sent_start,
                    sent_end,
                    regions,
                    unresolved,
                    sentence,
                    day_ref,
                    day_label,
                )

    # Extract warnings
    warnings: list[WeatherWarning] = []
    outlook = [sentence for sentence, _, _ in sentences if _OUTLOOK_RE.search(sentence)]
    for pattern, warning_type in _WARNING_PATTERNS.items():
        if match := pattern.search(narration_text):
            idx = match.start()
            # Find surrounding sentence
            for sentence, start, end in sentences:
                if start <= idx < end:
                    warning_day_ref, warning_day_label = _day_context_at(day_contexts, start)
                    warnings.append(
                        WeatherWarning(
                            type=warning_type,
                            text=sentence,
                            regions=_find_regions_in_text(sentence),
                            day_reference=warning_day_ref,
                            day_label_tr=warning_day_label,
                            source_span=SourceSpan(text=sentence, start=start, end=end),
                        )
                    )
                    break

    return WeatherData(
        story_id=story_id,
        language=language,
        source_text_hash=text_hash,
        source_language_text_hash=(
            hashlib.sha256(source_text.encode()).hexdigest()[:16] if source_text else None
        ),
        forecast_reference=forecast_ref,
        overview=overview,
        regions=regions,
        outlook=outlook,
        warnings=warnings,
        unresolved_claims=unresolved,
    )


def _process_clause(
    clause: str,
    start: int,
    end: int,
    regions: list[WeatherRegion],
    unresolved: list[str],
    full_sentence: str,
    day_reference: str | None = None,
    day_label: str | None = None,
) -> bool:
    """Process a single clause for region-condition associations.

    Returns True if a valid region+condition match was found.
    """
    found_regions = _find_regions_in_text(clause)
    found_conditions = _find_conditions_in_text(clause)

    if found_regions and found_conditions:
        # Clause-level association: each region in this clause gets
        # exactly the conditions found in THIS clause (not others).
        for rid in found_regions:
            # Check for temperature in this clause
            temp_min = None
            temp_max = None
            sent_temp = _TEMP_RANGE_RE.search(clause)
            if sent_temp:
                t1 = _parse_signed_temp(sent_temp.group(1))
                t2 = _parse_signed_temp(sent_temp.group(2))
                temp_min = min(t1, t2)
                temp_max = max(t1, t2)

            region = WeatherRegion(
                region_id=rid,
                label_tr=_REGION_LABELS_TR.get(rid, rid.value),
                conditions=found_conditions,
                temperature_min_c=temp_min,
                temperature_max_c=temp_max,
                wind=(
                    next(
                        (
                            match.group(0)
                            for keyword in ("rüzgârlı", "rüzgarlı", "rüzgâr", "rüzgar")
                            if (
                                match := re.search(
                                    rf"(?<!\w){re.escape(keyword)}(?!\w)",
                                    clause,
                                    re.IGNORECASE,
                                )
                            )
                        ),
                        None,
                    )
                    if WeatherCondition.WINDY in found_conditions
                    else None
                ),
                day_reference=day_reference,
                day_label_tr=day_label,
                source_span=SourceSpan(text=clause, start=start, end=end),
                confidence=0.95 if len(found_regions) == 1 else 0.85,
            )
            regions.append(region)
        return True
    elif found_conditions and not found_regions:
        # Conditions without a clear region — record as unresolved
        unresolved.append(clause)
        return False
    return False
