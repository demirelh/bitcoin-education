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
_WARNING_KEYWORDS = {
    "uyarı": "warning",
    "dikkat": "caution",
    "fırtına uyarısı": "storm",
    "sel": "flood",
    "don": "frost",
    "sıcak hava dalgası": "heat",
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
    regions: list[WeatherRegion] = []
    unresolved: list[str] = []

    for sentence, sent_start, sent_end in sentences:
        clauses = _extract_clauses(sentence)

        if len(clauses) <= 1:
            # Single clause or unsplittable — use whole sentence
            _process_clause(sentence, sent_start, sent_end, regions, unresolved, sentence)
        else:
            # Multiple clauses — clause-level nearest association
            clause_had_match = False
            for clause in clauses:
                # Find clause position within original text
                clause_offset = sentence.find(clause)
                clause_start = sent_start + clause_offset if clause_offset >= 0 else sent_start
                clause_end = clause_start + len(clause)
                matched = _process_clause(
                    clause, clause_start, clause_end, regions, unresolved, sentence
                )
                if matched:
                    clause_had_match = True

            if not clause_had_match:
                # Try whole sentence as fallback (regions in one clause,
                # conditions in another within the same sentence)
                _process_clause(sentence, sent_start, sent_end, regions, unresolved, sentence)

    # Extract warnings
    warnings: list[WeatherWarning] = []
    outlook = [sentence for sentence, _, _ in sentences if _OUTLOOK_RE.search(sentence)]
    narr_lower = narration_text.lower()
    for keyword, warning_type in _WARNING_KEYWORDS.items():
        if keyword in narr_lower:
            idx = narr_lower.index(keyword)
            # Find surrounding sentence
            for sentence, start, end in sentences:
                if start <= idx < end:
                    warnings.append(
                        WeatherWarning(
                            type=warning_type,
                            text=sentence,
                            regions=_find_regions_in_text(sentence),
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
