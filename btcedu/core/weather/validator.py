"""Deterministic validation of extracted weather data against source text.

Ensures every rendered claim is grounded in the approved narration.
"""

from __future__ import annotations

import logging
import re

from btcedu.core.weather.models import (
    FindingSeverity,
    FindingType,
    ValidationFinding,
    ValidationResult,
    WeatherData,
)

logger = logging.getLogger(__name__)


def validate_weather_data(
    weather_data: WeatherData,
    narration_text: str,
) -> ValidationResult:
    """Validate extracted weather data against the source narration.

    Every field must be traceable to the narration text. Unsupported
    or invented data produces findings that may block publishing.
    """
    findings: list[ValidationFinding] = []
    narr_lower = narration_text.lower()

    # Validate temperature range
    if weather_data.overview.temperature_min_c is not None:
        if not _temp_in_narration(weather_data.overview.temperature_min_c, narration_text):
            findings.append(
                ValidationFinding(
                    type=FindingType.UNSUPPORTED_TEMPERATURE,
                    severity=FindingSeverity.MAJOR,
                    message=(
                        f"Temperature min {weather_data.overview.temperature_min_c}°C "
                        f"not found in narration"
                    ),
                    publish_blocked=True,
                )
            )

    if weather_data.overview.temperature_max_c is not None:
        if not _temp_in_narration(weather_data.overview.temperature_max_c, narration_text):
            findings.append(
                ValidationFinding(
                    type=FindingType.UNSUPPORTED_TEMPERATURE,
                    severity=FindingSeverity.MAJOR,
                    message=(
                        f"Temperature max {weather_data.overview.temperature_max_c}°C "
                        f"not found in narration"
                    ),
                    publish_blocked=True,
                )
            )

    # Validate temperature range is plausible
    if (
        weather_data.overview.temperature_min_c is not None
        and weather_data.overview.temperature_max_c is not None
    ):
        if weather_data.overview.temperature_min_c > weather_data.overview.temperature_max_c:
            findings.append(
                ValidationFinding(
                    type=FindingType.INVALID_TEMPERATURE_RANGE,
                    severity=FindingSeverity.CRITICAL,
                    message="Temperature min exceeds max",
                    publish_blocked=True,
                )
            )
        diff = weather_data.overview.temperature_max_c - weather_data.overview.temperature_min_c
        if diff > 30:
            findings.append(
                ValidationFinding(
                    type=FindingType.INVALID_TEMPERATURE_RANGE,
                    severity=FindingSeverity.WARNING,
                    message=f"Temperature range span {diff}°C seems unusually large",
                )
            )

    # Validate each region
    for region in weather_data.regions:
        # Must have source span
        if region.source_span is None:
            findings.append(
                ValidationFinding(
                    type=FindingType.MISSING_SOURCE_SPAN,
                    severity=FindingSeverity.MAJOR,
                    message=f"Region {region.region_id.value} has no source span",
                    publish_blocked=True,
                )
            )
            continue

        # Source span text must exist in narration
        if region.source_span.text.lower() not in narr_lower:
            findings.append(
                ValidationFinding(
                    type=FindingType.UNSUPPORTED_REGION,
                    severity=FindingSeverity.MAJOR,
                    message=(f"Region {region.region_id.value} source span not found in narration"),
                    source_span=region.source_span,
                    publish_blocked=True,
                )
            )

        # Conditions must be traceable to the source span
        for condition in region.conditions:
            # Check condition keyword is in the source span
            cond_grounded = _condition_in_text(condition.value, region.source_span.text)
            if not cond_grounded:
                findings.append(
                    ValidationFinding(
                        type=FindingType.UNSUPPORTED_WEATHER_CLAIM,
                        severity=FindingSeverity.MAJOR,
                        message=(
                            f"Condition '{condition.value}' for region "
                            f"{region.region_id.value} not grounded in source span"
                        ),
                        source_span=region.source_span,
                        publish_blocked=True,
                    )
                )

        # Low confidence warning
        if region.confidence < 0.8:
            findings.append(
                ValidationFinding(
                    type=FindingType.WEATHER_EXTRACTION_LOW_CONFIDENCE,
                    severity=FindingSeverity.WARNING,
                    message=(
                        f"Region {region.region_id.value} extraction confidence "
                        f"{region.confidence:.2f} below threshold"
                    ),
                )
            )

    # Warn about unresolved claims
    for claim in weather_data.unresolved_claims:
        findings.append(
            ValidationFinding(
                type=FindingType.UNRESOLVED_WEATHER_CLAIM,
                severity=FindingSeverity.INFO,
                message=f"Unresolved claim: {claim[:80]}",
            )
        )

    publish_blocked = any(f.publish_blocked for f in findings)
    valid = not publish_blocked

    if findings:
        logger.info(
            "Weather validation: %d findings, publish_blocked=%s",
            len(findings),
            publish_blocked,
        )

    return ValidationResult(
        valid=valid,
        findings=findings,
        publish_blocked=publish_blocked,
    )


def _temp_in_narration(temp_value: int, narration_text: str) -> bool:
    """Check if a temperature value is grounded in the narration text.

    Handles both ASCII minus (-5) and Unicode minus (−5, U+2212).
    """
    temp_str = str(temp_value)
    if temp_str in narration_text:
        return True
    # Also check Unicode minus representation for negative values
    if temp_value < 0:
        unicode_repr = "\u2212" + str(abs(temp_value))
        if unicode_repr in narration_text:
            return True
    return False


# Reverse map: condition value → Turkish keywords that indicate it
_CONDITION_KEYWORDS: dict[str, list[str]] = {
    "sunny": ["güneş", "güneşli", "açık"],
    "mostly_sunny": ["güneşli", "çoğunlukla"],
    "partly_cloudy": ["parçalı bulutlu"],
    "cloudy": ["bulutlu"],
    "overcast": ["kapalı"],
    "rain": ["yağmur", "yağış"],
    "showers": ["sağanak"],
    "heavy_rain": ["şiddetli yağış", "şiddetli yağmur"],
    "thunderstorms": ["gök gürültü"],
    "snow": ["kar"],
    "fog": ["sis"],
    "windy": ["rüzgâr", "rüzgar"],
    "storm": ["fırtına"],
    "hot": ["sıcak"],
    "cold": ["serin", "soğuk"],
    "mixed": ["değişken", "karışık"],
}


def _condition_in_text(condition_value: str, text: str) -> bool:
    """Check if a condition is evidenced by keywords in the given text."""
    keywords = _CONDITION_KEYWORDS.get(condition_value, [])
    return any(
        re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", text, re.IGNORECASE)
        for keyword in keywords
    )
