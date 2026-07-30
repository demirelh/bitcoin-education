"""Deterministic weather renderer for news broadcast weather chapters.

Detects weather stories, extracts structured data from approved narration,
validates claims against source text, plans scenes, and renders branded
HTML/SVG weather visuals without generative AI.
"""

from btcedu.core.weather.detector import detect_weather_story
from btcedu.core.weather.extractor import extract_weather_data
from btcedu.core.weather.models import WeatherData, WeatherDetectionResult
from btcedu.core.weather.renderer import render_weather_visual
from btcedu.core.weather.validator import validate_weather_data

__all__ = [
    "WeatherData",
    "WeatherDetectionResult",
    "detect_weather_story",
    "extract_weather_data",
    "render_weather_visual",
    "validate_weather_data",
]
