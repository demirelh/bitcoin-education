"""Weather scene planner: deterministic scene layout based on TTS duration."""

from __future__ import annotations

import logging
from datetime import date

from btcedu.core.weather.dates import ForecastCalendar, format_date
from btcedu.core.weather.models import (
    ForecastReference,
    RegionId,
    WeatherData,
    WeatherScene,
    WeatherScenePlan,
    WeatherSceneType,
)

logger = logging.getLogger(__name__)

# Minimum scene duration in seconds
MIN_SCENE_DURATION = 2.0

# Title card duration
TITLE_DURATION = 3.5


def _calendar(weather_data: WeatherData) -> ForecastCalendar | None:
    """Rebuild the forecast calendar from the persisted weather data."""
    reference: ForecastReference = weather_data.forecast_reference
    if not reference.broadcast_date or not reference.anchor_date:
        return None
    try:
        return ForecastCalendar(
            broadcast_date=date.fromisoformat(reference.broadcast_date),
            anchor_date=date.fromisoformat(reference.anchor_date),
        )
    except ValueError:
        return None


def _anchor_day(weather_data: WeatherData) -> tuple[str | None, str | None, str | None]:
    """Return (day_reference, day_label, day_date) of the forecast anchor date."""
    from btcedu.core.weather.extractor import day_label_for

    reference: ForecastReference = weather_data.forecast_reference
    calendar = _calendar(weather_data)
    if calendar is not None:
        return (
            reference.day_reference,
            format_date(calendar.anchor_date),
            calendar.anchor_date.isoformat(),
        )
    if reference.date_text:
        return reference.day_reference, reference.date_text, None
    return reference.day_reference, day_label_for(reference.day_reference), None


def _outlook_day(
    text: str, calendar: ForecastCalendar | None
) -> tuple[str | None, str | None, str | None, str | None]:
    """Resolve a day badge for free-form outlook text."""
    from btcedu.core.weather.extractor import day_label_for, day_reference_in_text

    reference = day_reference_in_text(text)
    if not reference:
        return None, None, None, None
    if calendar is not None:
        span = calendar.resolve(reference)
        if span:
            start, end = span
            return (
                reference,
                calendar.label(reference),
                start.isoformat(),
                end.isoformat() if end != start else None,
            )
    return reference, day_label_for(reference), None, None


def _caption(calendar: ForecastCalendar | None, day_reference: str | None) -> str | None:
    """Small caption rendered above the date badge."""
    if calendar is None:
        return None
    return ForecastCalendar.caption(day_reference)


def plan_weather_scenes(
    weather_data: WeatherData,
    duration_seconds: float,
    *,
    story_id: str | None = None,
) -> WeatherScenePlan:
    """Create a deterministic scene plan from extracted weather data.

    Distributes scenes proportionally across the available TTS duration.
    Respects narration order and minimum scene durations.
    """
    if duration_seconds < MIN_SCENE_DURATION:
        duration_seconds = max(duration_seconds, 5.0)

    scenes: list[WeatherScene] = []
    current_time = 0.0
    remaining = duration_seconds
    calendar = _calendar(weather_data)
    anchor_ref, anchor_label, anchor_date = _anchor_day(weather_data)

    # Scene 1: Title card (always present)
    title_dur = min(TITLE_DURATION, remaining * 0.15)
    title_dur = max(title_dur, MIN_SCENE_DURATION)
    scenes.append(
        WeatherScene(
            type=WeatherSceneType.TITLE,
            start=current_time,
            end=current_time + title_dur,
            headline="Hava Durumu",
            day_reference=anchor_ref,
            day_label=anchor_label,
            day_caption=_caption(calendar, anchor_ref),
            day_date=anchor_date,
        )
    )
    current_time += title_dur
    remaining -= title_dur
    if remaining < MIN_SCENE_DURATION:
        scenes[0].end = duration_seconds
        return WeatherScenePlan(
            story_id=story_id or weather_data.story_id,
            duration_seconds=duration_seconds,
            scenes=scenes,
        )

    # Collect content segments to distribute remaining time
    segments: list[dict] = []

    # Region scenes — group regions grounded in the same source claim.
    if weather_data.regions:
        grouped_regions: dict[int, list[RegionId]] = {}
        grouped_days: dict[int, tuple[str | None, str | None, str | None]] = {}
        for region in weather_data.regions:
            position = region.source_span.start if region.source_span else 10**9
            grouped_regions.setdefault(position, []).append(region.region_id)
            grouped_days.setdefault(
                position, (region.day_reference, region.day_label_tr, region.day_date)
            )
        for position, region_ids in grouped_regions.items():
            day_reference, day_label, day_date = grouped_days.get(position, (None, None, None))
            for index in range(0, len(region_ids), 3):
                segments.append(
                    {
                        "type": "regions",
                        "regions": region_ids[index : index + 3],
                        "position": position,
                        "day_reference": day_reference,
                        "day_label": day_label,
                        "day_date": day_date,
                    }
                )

    # Temperature scene (if available)
    if (
        weather_data.overview.temperature_min_c is not None
        and weather_data.overview.temperature_max_c is not None
    ):
        segments.append(
            {
                "type": "temperature",
                "position": (
                    weather_data.overview.source_span.start
                    if weather_data.overview.source_span
                    else 10**9
                ),
                "day_reference": weather_data.overview.day_reference,
                "day_label": weather_data.overview.day_label_tr,
                "day_date": weather_data.overview.day_date,
            }
        )

    # Outlook / warnings
    if weather_data.warnings:
        segments.append(
            {
                "type": "warning",
                "position": (
                    weather_data.warnings[0].source_span.start
                    if weather_data.warnings[0].source_span
                    else 10**9
                ),
                "day_reference": weather_data.warnings[0].day_reference,
                "day_label": weather_data.warnings[0].day_label_tr,
                "day_date": weather_data.warnings[0].day_date,
            }
        )
    elif weather_data.outlook:
        outlook_ref, outlook_label, outlook_date, outlook_date_end = _outlook_day(
            weather_data.outlook[0], calendar
        )
        segments.append(
            {
                "type": "outlook",
                "position": 10**9,
                "day_reference": outlook_ref,
                "day_label": outlook_label,
                "day_date": outlook_date,
                "day_date_end": outlook_date_end,
            }
        )

    segments.sort(key=lambda segment: segment["position"])

    # Distribute remaining time proportionally
    if not segments:
        # No content — single generic scene
        scenes.append(
            WeatherScene(
                type=WeatherSceneType.REGIONS,
                start=current_time,
                end=duration_seconds,
                regions=[RegionId.GERMANY],
                day_reference=anchor_ref,
                day_label=anchor_label,
                day_caption=_caption(calendar, anchor_ref),
                day_date=anchor_date,
            )
        )
    else:
        per_segment = remaining / len(segments)
        per_segment = max(per_segment, MIN_SCENE_DURATION)

        for seg in segments:
            end_time = min(current_time + per_segment, duration_seconds)
            if end_time - current_time < MIN_SCENE_DURATION:
                break

            seg_day_ref = seg.get("day_reference") or anchor_ref
            seg_day_label = seg.get("day_label") or anchor_label
            seg_day_date = seg.get("day_date") or (
                anchor_date if not seg.get("day_label") else None
            )
            seg_caption = _caption(calendar, seg_day_ref)

            if seg["type"] == "regions":
                scenes.append(
                    WeatherScene(
                        type=WeatherSceneType.REGIONS,
                        start=current_time,
                        end=end_time,
                        regions=seg["regions"],
                        day_reference=seg_day_ref,
                        day_label=seg_day_label,
                        day_caption=seg_caption,
                        day_date=seg_day_date,
                    )
                )
            elif seg["type"] == "temperature":
                scenes.append(
                    WeatherScene(
                        type=WeatherSceneType.TEMPERATURE,
                        start=current_time,
                        end=end_time,
                        temperature_min_c=weather_data.overview.temperature_min_c,
                        temperature_max_c=weather_data.overview.temperature_max_c,
                        day_reference=seg_day_ref,
                        day_label=seg_day_label,
                        day_caption=seg_caption,
                        day_date=seg_day_date,
                    )
                )
            elif seg["type"] == "warning":
                scenes.append(
                    WeatherScene(
                        type=WeatherSceneType.WARNING,
                        start=current_time,
                        end=end_time,
                        text=weather_data.warnings[0].text if weather_data.warnings else None,
                        day_reference=seg_day_ref,
                        day_label=seg_day_label,
                        day_caption=seg_caption,
                        day_date=seg_day_date,
                    )
                )
            elif seg["type"] == "outlook":
                scenes.append(
                    WeatherScene(
                        type=WeatherSceneType.OUTLOOK,
                        start=current_time,
                        end=end_time,
                        text=weather_data.outlook[0] if weather_data.outlook else None,
                        day_reference=seg.get("day_reference"),
                        day_label=seg.get("day_label"),
                        day_caption=_caption(calendar, seg.get("day_reference")),
                        day_date=seg.get("day_date"),
                        day_date_end=seg.get("day_date_end"),
                    )
                )

            current_time = end_time

    # Extend last scene to fill remaining time
    if scenes and scenes[-1].end < duration_seconds:
        scenes[-1].end = duration_seconds

    return WeatherScenePlan(
        story_id=story_id or weather_data.story_id,
        duration_seconds=duration_seconds,
        scenes=scenes,
    )
