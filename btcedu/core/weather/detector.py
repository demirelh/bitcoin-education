"""Weather story detection using title, type, text, and metadata signals."""

from __future__ import annotations

import logging
import re

from btcedu.core.weather.models import WeatherDetectionResult

logger = logging.getLogger(__name__)

# Turkish weather vocabulary
_TR_WEATHER_KEYWORDS = [
    "hava durumu",
    "hava tahmini",
    "sıcaklıklar",
    "yağmur",
    "sağanak",
    "güneşli",
    "bulutlu",
    "rüzgâr",
    "rüzgar",
    "fırtına",
    "kar yağışı",
    "sıcak",
    "serin",
    "derece",
    "kuzey",
    "güney",
    "doğu",
    "batı",
    "parçalı bulutlu",
    "yağış",
    "şiddetli yağış",
    "gök gürültülü",
]

# German weather vocabulary
_DE_WEATHER_KEYWORDS = [
    "wetter",
    "wetterbericht",
    "vorhersage",
    "temperaturen",
    "regen",
    "schauer",
    "sonne",
    "sonnig",
    "wind",
    "sturm",
    "schnee",
    "bewölkt",
    "grad",
    "norden",
    "süden",
    "osten",
    "westen",
]

# High-confidence title patterns
_TITLE_PATTERNS = [
    re.compile(r"hava\s+durumu", re.IGNORECASE),
    re.compile(r"hava\s+tahmini", re.IGNORECASE),
    re.compile(r"wetter(bericht)?$", re.IGNORECASE),
    re.compile(r"weather\s*(forecast)?$", re.IGNORECASE),
]

# Temperature pattern (Turkish/German)
_TEMP_PATTERN = re.compile(r"\d+\s*(ile|bis|[-–])\s*\d+\s*(derece|grad|°)", re.IGNORECASE)

# Regional forecast pattern
_REGIONAL_PATTERN = re.compile(
    r"(kuzey|güney|doğu|batı|norden|süden|osten|westen)"
    r".{1,40}"
    r"(yağ[ıi]ş|güneş|regen|sonn|bulut|sağanak|schauer)",
    re.IGNORECASE,
)


def detect_weather_story(
    *,
    title: str = "",
    story_type: str | None = None,
    narration_text: str = "",
    source_text: str = "",
    metadata: dict | None = None,
    min_confidence: float = 0.75,
) -> WeatherDetectionResult:
    """Detect whether a chapter/story is a weather forecast.

    Uses multiple signals: title keywords, story type, narration content,
    source text, and metadata. Returns confidence score and evidence list.
    """
    evidence: list[str] = []
    score = 0.0

    title_lower = title.lower().strip()
    narration_lower = narration_text.lower()
    source_lower = source_text.lower()
    combined_text = f"{narration_lower} {source_lower}"

    # Signal 1: Title matches known weather patterns (strongest signal)
    for pattern in _TITLE_PATTERNS:
        if pattern.search(title_lower):
            score += 0.80
            evidence.append(f"chapter title matches weather pattern: '{title}'")
            break

    # Signal 2: Story type metadata
    if story_type and story_type.lower() in ("weather", "hava_durumu", "wetter"):
        score += 0.80
        evidence.append(f"story type is '{story_type}'")

    # Signal 3: Temperature range detected in narration
    if _TEMP_PATTERN.search(combined_text):
        score += 0.25
        evidence.append("temperature range detected in text")

    # Signal 4: Regional forecast language
    if _REGIONAL_PATTERN.search(combined_text):
        score += 0.20
        evidence.append("regional forecast language detected")

    # Signal 5: Keyword density in narration
    tr_hits = sum(1 for kw in _TR_WEATHER_KEYWORDS if kw in narration_lower)
    de_hits = sum(1 for kw in _DE_WEATHER_KEYWORDS if kw in source_lower)
    keyword_hits = tr_hits + de_hits

    if keyword_hits >= 5:
        score += 0.30
        evidence.append(f"high weather keyword density ({keyword_hits} matches)")
    elif keyword_hits >= 3:
        score += 0.15
        evidence.append(f"moderate weather keyword density ({keyword_hits} matches)")

    # Signal 6: Metadata hints
    if metadata:
        if metadata.get("is_weather") or metadata.get("weather_chapter"):
            score += 0.30
            evidence.append("metadata indicates weather chapter")

    # Cap confidence at 1.0
    confidence = min(score, 1.0)
    is_weather = confidence >= min_confidence

    if is_weather:
        logger.info(
            "Weather story detected (confidence=%.2f): %s",
            confidence,
            "; ".join(evidence),
        )
    else:
        logger.debug(
            "Not a weather story (confidence=%.2f): %s",
            confidence,
            "; ".join(evidence) or "no evidence",
        )

    return WeatherDetectionResult(
        is_weather_story=is_weather,
        confidence=confidence,
        evidence=evidence,
    )
