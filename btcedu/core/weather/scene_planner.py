"""Weather scene planner: deterministic scene layout based on TTS duration."""

from __future__ import annotations

import logging

from btcedu.core.weather.models import (
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

    # Scene 1: Title card (always present)
    title_dur = min(TITLE_DURATION, remaining * 0.15)
    title_dur = max(title_dur, MIN_SCENE_DURATION)
    scenes.append(
        WeatherScene(
            type=WeatherSceneType.TITLE,
            start=current_time,
            end=current_time + title_dur,
            headline="Hava Durumu",
        )
    )
    current_time += title_dur
    remaining -= title_dur

    # Collect content segments to distribute remaining time
    segments: list[dict] = []

    # Region scenes — group adjacent regions with same conditions
    if weather_data.regions:
        # Group regions into batches of max 3 for readability
        region_batches: list[list[RegionId]] = []
        batch: list[RegionId] = []
        for region in weather_data.regions:
            batch.append(region.region_id)
            if len(batch) >= 3:
                region_batches.append(batch)
                batch = []
        if batch:
            region_batches.append(batch)

        for rb in region_batches:
            segments.append({"type": "regions", "regions": rb})

    # Temperature scene (if available)
    if (
        weather_data.overview.temperature_min_c is not None
        and weather_data.overview.temperature_max_c is not None
    ):
        segments.append({"type": "temperature"})

    # Outlook / warnings
    if weather_data.warnings:
        segments.append({"type": "warning"})
    elif weather_data.outlook:
        segments.append({"type": "outlook"})

    # Distribute remaining time proportionally
    if not segments:
        # No content — single generic scene
        scenes.append(
            WeatherScene(
                type=WeatherSceneType.REGIONS,
                start=current_time,
                end=duration_seconds,
                regions=[RegionId.GERMANY],
            )
        )
    else:
        per_segment = remaining / len(segments)
        per_segment = max(per_segment, MIN_SCENE_DURATION)

        for seg in segments:
            end_time = min(current_time + per_segment, duration_seconds)
            if end_time - current_time < MIN_SCENE_DURATION:
                break

            if seg["type"] == "regions":
                scenes.append(
                    WeatherScene(
                        type=WeatherSceneType.REGIONS,
                        start=current_time,
                        end=end_time,
                        regions=seg["regions"],
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
                    )
                )
            elif seg["type"] == "warning":
                scenes.append(
                    WeatherScene(
                        type=WeatherSceneType.WARNING,
                        start=current_time,
                        end=end_time,
                        text=weather_data.warnings[0].text if weather_data.warnings else None,
                    )
                )
            elif seg["type"] == "outlook":
                scenes.append(
                    WeatherScene(
                        type=WeatherSceneType.OUTLOOK,
                        start=current_time,
                        end=end_time,
                        text=weather_data.outlook[0] if weather_data.outlook else None,
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
