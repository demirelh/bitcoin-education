"""Versioned Pydantic data models for weather extraction and rendering."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

# Renderer version — bump when template/SVG/logic changes to invalidate cache.
RENDERER_VERSION = "1.0.0"


class WeatherCondition(str, Enum):
    """Supported weather condition identifiers."""

    SUNNY = "sunny"
    MOSTLY_SUNNY = "mostly_sunny"
    PARTLY_CLOUDY = "partly_cloudy"
    CLOUDY = "cloudy"
    OVERCAST = "overcast"
    RAIN = "rain"
    SHOWERS = "showers"
    HEAVY_RAIN = "heavy_rain"
    THUNDERSTORMS = "thunderstorms"
    SNOW = "snow"
    FOG = "fog"
    WINDY = "windy"
    STORM = "storm"
    HOT = "hot"
    COLD = "cold"
    MIXED = "mixed"


class RegionId(str, Enum):
    """Supported geographical region identifiers."""

    GERMANY = "germany"
    NORTH = "north"
    NORTHEAST = "northeast"
    NORTHWEST = "northwest"
    SOUTH = "south"
    SOUTHEAST = "southeast"
    SOUTHWEST = "southwest"
    EAST = "east"
    WEST = "west"
    CENTRAL = "central"
    COAST = "coast"
    ALPS = "alps"


class FindingSeverity(str, Enum):
    """Severity levels for validation findings."""

    INFO = "INFO"
    WARNING = "WARNING"
    MAJOR = "MAJOR"
    CRITICAL = "CRITICAL"


class FindingType(str, Enum):
    """Types of validation findings."""

    UNSUPPORTED_WEATHER_CLAIM = "unsupported_weather_claim"
    UNSUPPORTED_WEATHER_VISUAL_CLAIM = "unsupported_weather_visual_claim"
    UNSUPPORTED_TEMPERATURE = "unsupported_temperature"
    UNSUPPORTED_REGION = "unsupported_region"
    AMBIGUOUS_REGION_CONDITION = "ambiguous_region_condition"
    MISSING_SOURCE_SPAN = "missing_source_span"
    INVALID_TEMPERATURE_RANGE = "invalid_temperature_range"
    UNRESOLVED_WEATHER_CLAIM = "unresolved_weather_claim"
    WEATHER_EXTRACTION_LOW_CONFIDENCE = "weather_extraction_low_confidence"
    WEATHER_VISUAL_MISSING = "weather_visual_missing"
    BLANK_VISUAL_DURING_NARRATION = "blank_visual_during_narration"
    TRANSPARENT_WEATHER_FRAME = "transparent_weather_frame"
    WEATHER_RENDER_FAILED = "weather_render_failed"
    WEATHER_DURATION_MISMATCH = "weather_duration_mismatch"
    WEATHER_VISUAL_STALE = "weather_visual_stale"
    WEATHER_SCENE_EMPTY = "weather_scene_empty"


class SourceSpan(BaseModel):
    """Reference to the exact source text that grounds a claim."""

    text: str
    start: int = Field(ge=0)
    end: int = Field(ge=0)


class ForecastReference(BaseModel):
    """Temporal reference for the forecast."""

    date_text: str | None = None
    day_reference: str | None = None  # "today", "tomorrow", day name


class WeatherRegion(BaseModel):
    """A regional weather forecast entry."""

    region_id: RegionId
    label_tr: str
    conditions: list[WeatherCondition]
    temperature_min_c: int | None = None
    temperature_max_c: int | None = None
    wind: str | None = None
    source_span: SourceSpan | None = None
    confidence: float = Field(default=0.9, ge=0.0, le=1.0)


class WeatherOverview(BaseModel):
    """Overview/headline data for the entire forecast."""

    headline: str | None = None
    temperature_min_c: int | None = None
    temperature_max_c: int | None = None
    source_span: SourceSpan | None = None


class WeatherWarning(BaseModel):
    """Weather warning entry."""

    type: str  # e.g. "storm", "heavy_rain", "heat"
    text: str
    regions: list[RegionId] = Field(default_factory=list)
    source_span: SourceSpan | None = None


class WeatherData(BaseModel):
    """Complete structured weather data extracted from narration."""

    schema_version: str = SCHEMA_VERSION
    story_id: str | None = None
    language: str = "tr"
    source_text_hash: str = ""
    source_language_text_hash: str | None = None
    forecast_reference: ForecastReference = Field(default_factory=ForecastReference)
    overview: WeatherOverview = Field(default_factory=WeatherOverview)
    regions: list[WeatherRegion] = Field(default_factory=list)
    outlook: list[str] = Field(default_factory=list)
    warnings: list[WeatherWarning] = Field(default_factory=list)
    unresolved_claims: list[str] = Field(default_factory=list)


class ValidationFinding(BaseModel):
    """A single validation finding."""

    type: FindingType
    severity: FindingSeverity
    message: str
    source_span: SourceSpan | None = None
    publish_blocked: bool = False


class ValidationResult(BaseModel):
    """Result of weather data validation."""

    valid: bool = True
    findings: list[ValidationFinding] = Field(default_factory=list)
    publish_blocked: bool = False


class WeatherDetectionResult(BaseModel):
    """Result of weather story detection."""

    is_weather_story: bool
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = Field(default_factory=list)


class WeatherSceneType(str, Enum):
    """Types of weather scenes for rendering."""

    TITLE = "weather_title"
    REGIONS = "weather_regions"
    TEMPERATURE = "weather_temperature"
    OUTLOOK = "weather_outlook"
    WARNING = "weather_warning"


class WeatherScene(BaseModel):
    """A single scene in the weather scene plan."""

    type: WeatherSceneType
    start: float = Field(ge=0.0)
    end: float = Field(ge=0.0)
    headline: str | None = None
    regions: list[RegionId] = Field(default_factory=list)
    temperature_min_c: int | None = None
    temperature_max_c: int | None = None
    text: str | None = None


class WeatherScenePlan(BaseModel):
    """Complete scene plan for weather rendering."""

    story_id: str | None = None
    duration_seconds: float = 0.0
    scenes: list[WeatherScene] = Field(default_factory=list)


class WeatherRenderResult(BaseModel):
    """Result of weather rendering."""

    success: bool
    output_path: str | None = None
    fallback_level: Literal["full", "reduced", "generic", "pillow_fallback"] = "full"
    renderer_version: str = RENDERER_VERSION
    cache_key: str = ""
    findings: list[ValidationFinding] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
